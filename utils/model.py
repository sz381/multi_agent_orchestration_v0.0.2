"""
LLM initialization and retry utility.
"""

import asyncio
import ssl
from functools import lru_cache
from typing import Any

import httpx
import openai
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from utils.settings import settings
from utils.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0

_RETRYABLE_EXC = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
    ssl.SSLError,
    httpx.TransportError,
    httpx.ReadError,
    httpx.ConnectError,
    ConnectionError,
    OSError,
)


@lru_cache(maxsize=8)
def init_model(
    model_name: str = settings.deepseek_model_name,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    streaming: bool = True,
) -> ChatOpenAI:
    """
    Create a configured ``ChatOpenAI`` instance.

    Results are cached via ``lru_cache`` — identical argument combinations
    return the same instance, avoiding repeated object construction across
    orchestrator turns and sub-agent fanout rounds.
    """
    if model_name.startswith("deepseek"):
        api_key = settings.deepseek_api_key
        base_url = settings.deepseek_base_url
    elif model_name.startswith("mimo"):
        api_key = settings.xiaomi_mimo_api_key
        base_url = settings.xiaomi_mimo_base_url
    else:
        raise ValueError(f"Unknown model name: {model_name}")

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
    )


async def ainvoke_with_retry(
    runnable: Any,
    *args: Any,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
    **kwargs: Any,
) -> Any:
    """Invoke ``runnable.ainvoke()`` with exponential-backoff retry.

    Retries on transient network errors (SSL, connection, timeout, rate-limit).
    Non-retryable exceptions propagate immediately.

    Args:
        runnable:  Any object with an ``ainvoke`` method (ChatOpenAI, Runnable, etc.)
        *args:     Positional args forwarded to ``ainvoke``.
        max_retries: Maximum retry attempts (default 3).
        base_delay:  Initial backoff delay in seconds (default 1.0, doubles each retry).
        **kwargs:  Keyword args forwarded to ``ainvoke``.

    Returns:
        The result of ``runnable.ainvoke(*args, **kwargs)``.

    Raises:
        The last retryable exception if all retries are exhausted,
        or the original non-retryable exception immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await runnable.ainvoke(*args, **kwargs)
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                if isinstance(exc, openai.RateLimitError):
                    delay = max(delay, 5.0)
                logger.warning(
                    "ainvoke_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay=round(delay, 1),
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)
            else:
                raise
    raise last_exc


def _is_empty_response(msg: Any) -> bool:
    """Return True when ``msg`` carries neither tool calls nor text content.

    An "empty response" means the LLM call succeeded but produced nothing
    usable: no tool calls AND no content (None / blank string / blank
    content blocks). This is a response-level anomaly — distinct from
    network errors — and typically correlates with long-context pressure
    (8_03_011: orchestrator returned an empty response and the graph ended
    via fallback without calling end_orchestration).
    """
    if getattr(msg, "tool_calls", None):
        return False
    content = getattr(msg, "content", None)
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        # Content-blocks form: [{"type": "text", "text": "..."}].
        # A non-dict block is unexpected → treat as non-empty (conservative).
        for block in content:
            if not isinstance(block, dict):
                return False
            text = block.get("text") or ""
            if str(text).strip():
                return False
        return True
    return not str(content).strip()


async def ainvoke_with_content_guard(
    runnable: Any,
    *args: Any,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
    allow_empty: bool = False,
    role: str = "agent",
    **kwargs: Any,
) -> Any:
    """Invoke with network retry, then guard against empty responses.

    Layer 1 — ``ainvoke_with_retry``: retries transient network errors
    (SSL, connection, timeout, rate-limit). Layer 2 — empty-response guard:
    if the model returns successfully but with no tool calls and no content,
    re-send the SAME request once (no backoff — the input is unchanged). A
    second empty response is returned as-is so the caller's existing
    fallback logic still runs (no exception raised).

    Args:
        runnable:  Any object with an ``ainvoke`` method (ChatOpenAI, etc.).
        *args:     Positional args forwarded to ``ainvoke``.
        max_retries: Network retry attempts (forwarded, default 3).
        base_delay:  Initial backoff delay in seconds (forwarded, default 1.0).
        allow_empty: When True, empty responses are returned without retry.
        role:       Log label for the calling role (orchestrator / sub_agent / ...).
        **kwargs:  Keyword args forwarded to ``ainvoke``.

    Returns:
        The first non-empty result, or the (empty) result when both attempts
        are empty / ``allow_empty`` is True.
    """
    response = await ainvoke_with_retry(
        runnable, *args, max_retries=max_retries, base_delay=base_delay, **kwargs
    )
    if not _is_empty_response(response) or allow_empty:
        return response
    logger.warning(
        "llm_empty_response_retry",
        role=role,
        attempt=1,
    )
    response = await ainvoke_with_retry(
        runnable, *args, max_retries=max_retries, base_delay=base_delay, **kwargs
    )
    if _is_empty_response(response):
        logger.warning(
            "llm_empty_response",
            role=role,
            attempt=2,
        )
    return response


def count_tokens(messages: list) -> dict[str, int]:
    """Aggregate token usage across all AI-generated messages.

    Reads ``usage_metadata`` that LangChain automatically attaches to each
    :class:`~langchain_core.messages.AIMessage` during LLM invocation
    both streaming and non-streaming. 

    Returns:
        dict with keys ``prompt_tokens``, ``completion_tokens``,
        ``total_tokens`` — each an integer sum across all AI messages.
    """
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        um = getattr(msg, "usage_metadata", None) or {}
        prompt_tokens += um.get("input_tokens", 0)
        completion_tokens += um.get("output_tokens", 0)
        total_tokens += um.get("total_tokens", 0)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
