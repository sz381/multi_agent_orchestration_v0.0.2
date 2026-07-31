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
    model_name: str = settings.xiaomi_mimo_model_name,
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
    return ChatOpenAI(
        model=model_name,
        api_key=settings.xiaomi_mimo_api_key,
        base_url=settings.xiaomi_mimo_base_url,
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
