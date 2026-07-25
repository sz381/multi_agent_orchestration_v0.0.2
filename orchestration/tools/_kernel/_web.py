import json
import atexit
import asyncio
import ipaddress
from urllib.parse import urlparse

from ddgs import DDGS
from tavily import TavilyClient
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from utils.settings import settings
from utils.logging import get_logger
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
    global _crawler, _crawler_started
    
    if _crawler is None:
        async with _crawler_lock:
            if _crawler is None:
                crawler = AsyncWebCrawler()
                
                await crawler.start()       #  start
                _crawler = crawler          #  再赋值（消除 TOCTOU）
                
                if not _crawler_started:
                    _crawler_started = True
                    atexit.register(_close_crawler_sync)
                    
    return _crawler


async def _close_crawler():
    global _crawler
    
    if _crawler is not None:
        await _crawler.close()
        _crawler = None


async def close_crawler():
    """Public shutdown: close the crawler while event loop is still alive."""
    await _close_crawler()


def _close_crawler_sync():
    global _crawler
    
    if _crawler is not None:
        try:
            asyncio.run(_close_crawler())
        except (RuntimeError, KeyboardInterrupt):
            pass
        except Exception:
            pass


def _matches_domain(url: str, domains: list[str]) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_private_url(url: str) -> bool:
    """SSRF 防护：阻止访问内网 / localhost / 特殊 IP"""
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
        pass   # 域名，不是 IP — 抓取时由 DNS 解析决定

    return False



def _format_web_search_results(raw_results: list[dict]) -> str:
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


async def _summarize_with_llm(content: str, prompt: str) -> str:
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
        response = await summarizer.ainvoke([HumanMessage(content=text)])
        result = response.content
        
        logger.debug("web_summarize_done", before=len(content), after=len(result))
        
        return result
    except Exception as e:
        logger.warning("web_summarize_failed", error=str(e), content_len=len(content))
        return content


def web_search(
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


async def _fallback_fetch(url: str, prompt: str, crawl_error: Exception) -> str:
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
