"""
Web search and page fetch tools.
"""

import json
import atexit
import asyncio
import ipaddress
from urllib.parse import urlparse

from ddgs import DDGS
from tavily import TavilyClient
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from utils.settings import settings
from utils.logging import get_logger
from utils.model import ainvoke_with_retry
from orchestration.tools.description.web import WEB_SUMMARIZE_TEMPLATE

MAX_SEARCH_RESULTS          = 20
MAX_QUERY_LENGTH            = 500
MAX_URL_LENGTH              = 2048
MAX_PROMPT_LENGTH           = 5000
SUMMARIZE_LENGTH_THRESHOLD  = 4000
MAX_CONTENT_CHARS           = 100_000
PAGE_TIMEOUT_MS             = 20_000

_crawler: AsyncWebCrawler | None = None
_crawler_lock = asyncio.Lock()
_crawler_started = False

_SSRF_BLOCKED_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})

logger = get_logger(__name__)


async def _get_crawler() -> AsyncWebCrawler:
    """Return the singleton crawler instance, starting it if needed.

    Uses double-checked locking to avoid starting multiple crawlers.
    Registers an atexit hook for cleanup on process exit.
    Configures the browser with a realistic macOS Chrome User-Agent,
    stealth mode, and typical browser headers to reduce anti-bot detection.
    """
    
    global _crawler, _crawler_started

    if _crawler is None:
        async with _crawler_lock:
            if _crawler is None:
                browser_config = BrowserConfig(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/135.0.0.0 Safari/537.36"
                    ),
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "DNT": "1",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    viewport_width=1920,
                    viewport_height=1080,
                    enable_stealth=True,
                )
                crawler = AsyncWebCrawler(config=browser_config)

                await crawler.start()
                _crawler = crawler

                if not _crawler_started:
                    _crawler_started = True
                    atexit.register(_close_crawler_sync)

    return _crawler


async def _close_crawler():
    """
    Close and clear the singleton crawler.
    """
    
    global _crawler

    if _crawler is not None:
        await _crawler.close()
        _crawler = None


async def close_crawler():
    """
    Public shutdown: close the crawler while the event loop is alive.
    """
    
    await _close_crawler()


def _close_crawler_sync():
    """
    Synchronous wrapper for atexit — runs _close_crawler in a new event loop.
    """
    
    global _crawler

    if _crawler is not None:
        try:
            asyncio.run(_close_crawler())
        except (RuntimeError, KeyboardInterrupt):
            pass
        except Exception:
            pass


def _matches_domain(
    url: str, 
    domains: list[str]
) -> bool:
    """Check whether a URL's hostname matches any domain in the list.

    Matches exact hostname or any subdomain (e.g. ``example.com``
    matches both ``example.com`` and ``sub.example.com``).
    """
    
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False

    return any(host == d or host.endswith("." + d) for d in domains)


def _is_private_url(
    url: str
) -> bool:
    """SSRF protection: block private, loopback, and special IPs.

    Blocks localhost, private ranges (10.x, 172.16.x, 192.168.x),
    loopback (127.x), and unspecified addresses. Domain names pass
    through — DNS resolution is left to the crawler.
    """
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return True

    if host.lower() in _SSRF_BLOCKED_HOSTS:
        return True

    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_unspecified
    except ValueError:
        pass

    return False


def _format_web_search_results(
    raw_results: list[dict]
) -> str:
    """Format raw search results into a standardized JSON response.

    Args:
        raw_results: List of raw result dicts from DDG or Tavily.

    Returns:
        JSON with status, total_results, and indexed results list.
    """
    
    if not raw_results:
        return json.dumps({
            "status": "ok",
            "total_results": 0,
            "results": [],
        }, ensure_ascii=False)

    results = []
    for i, r in enumerate(raw_results, 1):
        results.append({
            "index": i,
            "title": r.get("title", "").strip(),
            "url": r.get("url") or r.get("href", "").strip(),
            "snippet": r.get("content") or r.get("body", "").strip(),
        })

    return json.dumps({
        "status": "ok",
        "total_results": len(results),
        "results": results,
    }, ensure_ascii=False)


async def _summarize_with_llm(
    content: str, 
    prompt: str
) -> str:
    """Summarize page content with a secondary LLM call.

    Short content is returned as-is. Long content is truncated and
    summarized via Secondary LLM. Falls back to raw content on failure.

    Args:
        content: Raw page content to summarize.
        prompt: What the user wants to extract from the page.

    Returns:
        Summarized content, or the original if short/summarization fails.
    """
    
    if len(content) <= SUMMARIZE_LENGTH_THRESHOLD:
        return content
    
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]
        logger.debug("web_summarize_truncated", max_chars=MAX_CONTENT_CHARS)

    api_key = settings.deepseek_api_key
    base_url = settings.deepseek_base_url
    model = settings.deepseek_model_name

    logger.debug("web_summarize_start", content_len=len(content), model=model)

    try:
        summarizer = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model,
            streaming=False,
            temperature=0.0,
            max_tokens=5000,
        )
        
        text = WEB_SUMMARIZE_TEMPLATE.format(prompt=prompt, content=content)
        response = await ainvoke_with_retry(summarizer, [HumanMessage(content=text)])
        result = response.content
        
        logger.debug("web_summarize_done", before=len(content), after=len(result))
        
        return result
    except Exception as e:
        logger.warning("web_summarize_failed", error=str(e), content_len=len(content))
        return content


async def web_search(
    query: str,
    max_results: int = 5,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web via DuckDuckGo, with Tavily fallback.

    Args:
        query: Search keywords (2-500 chars).
        max_results: Max results, clamped to 1-20.
        allowed_domains: Restrict results to these domains.
        blocked_domains: Exclude these domains from results.

    Returns:
        JSON with status, total_results, and a results list.
    """
    return await asyncio.to_thread(
        _web_search_sync,
        query,
        max_results,
        allowed_domains,
        blocked_domains,
    )


def _web_search_sync(
    query: str,
    max_results: int = 5,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    if allowed_domains is not None and blocked_domains is not None:
        return json.dumps({
            "status": "error", 
            "message": "allowed_domains and blocked_domains are mutually exclusive. Use one or the other, not both."
        }, ensure_ascii=False)

    query = query.strip()
    
    if len(query) < 2:
        return json.dumps({
            "status": "error", 
            "message": "query must be at least 2 characters."
        }, ensure_ascii=False)
    
    if len(query) > MAX_QUERY_LENGTH:
        return json.dumps({
            "status": "error", 
            "message": f"query too long ({len(query)} chars). Max {MAX_QUERY_LENGTH}."
        }, ensure_ascii=False)

    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results * 3))
            
        if allowed_domains is not None:
            raw_results = [r for r in raw_results if _matches_domain(r.get("href", ""), allowed_domains)]
        elif blocked_domains is not None:
            raw_results = [r for r in raw_results if not _matches_domain(r.get("href", ""), blocked_domains)]    
        raw_results = raw_results[:max_results]
        
        return _format_web_search_results(raw_results)
    except Exception as e:
        return _fallback_search(query, max_results, allowed_domains, blocked_domains, ddg_error=str(e))


async def fetch_web(
    url: str,
    prompt: str,
) -> str:
    """Fetch and extract content from a web page.

    Uses crawl4ai (headless Chromium) with Tavily extract fallback.
    Long pages are summarized by a secondary LLM.

    Args:
        url: Full HTTPS URL to fetch.
        prompt: What to extract from the page.

    Returns:
        Extracted markdown content, or a JSON error.
    """
    
    if not url or not url.strip():
        return json.dumps({
            "status": "error", 
            "message": "url is required."
        }, ensure_ascii=False)

    url = url.strip()
    
    if not url.startswith(("http://", "https://")):
        return json.dumps({
            "status": "error", 
            "message": "url must be a fully-formed URL starting with http:// or https://."
        }, ensure_ascii=False)
    
    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)
        
    if len(url) > MAX_URL_LENGTH:
        return json.dumps({
            "status": "error", 
            "message": f"url too long ({len(url)} chars). Max {MAX_URL_LENGTH}."
        }, ensure_ascii=False)
    
    if _is_private_url(url):
        return json.dumps({
            "status": "error", 
            "message": f"Access to private/internal URLs is blocked: {url}"
        }, ensure_ascii=False)

    if not prompt or not prompt.strip():
        return json.dumps({
            "status": "error",
            "message": "prompt is required."
        }, ensure_ascii=False)

    prompt = prompt.strip()
    
    if len(prompt) > MAX_PROMPT_LENGTH:
        return json.dumps({
            "status": "error", 
            "message": f"prompt too long ({len(prompt)} chars). Max {MAX_PROMPT_LENGTH}."
        }, ensure_ascii=False)

    try:
        md_gen = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.4, threshold_type="fixed")
        )
        config = CrawlerRunConfig(
            markdown_generator=md_gen,
            cache_mode=CacheMode.ENABLED,
            page_timeout=PAGE_TIMEOUT_MS,
            magic=True,
            simulate_user=True,
            override_navigator=True,
        )
        
        crawler = await _get_crawler()
        result = await crawler.arun(url=url, config=config)
        content = result.markdown.fit_markdown or result.markdown.raw_markdown or "(empty)"
        
        if len(content) > SUMMARIZE_LENGTH_THRESHOLD:
            content = await _summarize_with_llm(content, prompt)
            
        return content.strip()
    except Exception as e:
        return await _fallback_fetch(url, prompt, e)


def _fallback_search(
    query: str,
    max_results: int = 5,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    ddg_error: str = "",
) -> str:
    """Fallback search via Tavily when DuckDuckGo fails.

    Args:
        query: Search keywords.
        max_results: Max results to return.
        allowed_domains: Restrict results to these domains.
        blocked_domains: Exclude these domains from results.
        ddg_error: The error message from the failed DDG call.

    Returns:
        JSON with status, total_results, and a results list.
    """
    
    if not settings.tavily_api_key:
        return json.dumps({
            "status": "error", 
            "message": f"DuckDuckGo search failed and no Tavily API key configured. DDG error: {ddg_error}"
        }, ensure_ascii=False)
    
    try:
        client = TavilyClient(api_key=settings.tavily_api_key)
        
        response = client.search(
            query=query,
            max_results=max_results,
            include_domains=list(allowed_domains) if allowed_domains else None,
            exclude_domains=list(blocked_domains) if blocked_domains else None,
        )
        
        results = response.get("results", [])
        return _format_web_search_results(results)
    except Exception as e:
        return json.dumps({
            "status": "error", 
            "message": f"Both DuckDuckGo and Tavily search failed. DDG: {ddg_error} | Tavily: {e}"
        }, ensure_ascii=False)


async def _fallback_fetch(
    url: str, 
    prompt: str, 
    crawl_error: Exception
) -> str:
    """Fallback page fetch via Tavily extract when crawl4ai fails.

    Summarizes long results with the same LLM path as fetch_web.
    """
    if not settings.tavily_api_key:
        return json.dumps({
            "status": "error", 
            "message": f"fetch_web failed: {crawl_error}"
        }, ensure_ascii=False)
    
    try:
        client = TavilyClient(api_key=settings.tavily_api_key)
        response = client.extract(urls=[url])
        results = response.get("results", [])
        
        if not results:
            return json.dumps({
                "status": "error", 
                "message": f"Tavily extract returned no results for {url}"
            }, ensure_ascii=False)
        
        content = results[0].get("raw_content", "")
        
        if not content:
            return json.dumps({
                "status": "error", 
                "message": f"Tavily extract for {url} returned empty raw_content."
            }, ensure_ascii=False)
        
        if len(content) > SUMMARIZE_LENGTH_THRESHOLD:
            content = await _summarize_with_llm(content, prompt)
            
        return content.strip()
    except Exception as e:
        return json.dumps({
            "status": "error", 
            "message": f"Both crawl4ai and Tavily extract failed. crawl4ai: {crawl_error} | Tavily: {e}"
        }, ensure_ascii=False)
