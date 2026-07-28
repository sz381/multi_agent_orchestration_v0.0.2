"""Shared retry utility for LLM ainvoke calls with exponential backoff.

Catches transient network/SSL errors that cause crashes under concurrent load,
and retries with exponential backoff before giving up.
"""

import asyncio
import ssl
from typing import Any

import httpx
import openai

from utils.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds

# Exceptions that are safe to retry — all transient/network-level.
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
                # Rate limits need longer backoff.
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
    raise last_exc  # type: ignore[misc]
