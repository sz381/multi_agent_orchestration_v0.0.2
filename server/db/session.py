"""Async postgres session: connection pool + schema bootstrap.

The database is a write-once ARCHIVE for finished orchestrations. It is
not read by the live API in this phase; the in-memory StreamChannel
remains the source of truth for queries and SSE replay.

Fail-open contract
------------------
- No ``DATABASE_URL`` configured  -> init_pool() returns False, the pool
  stays None, the service runs with zero DB dependency.
- Connection / DDL failure       -> logged, pool reset to None, same
  fail-open behavior.
"""

import asyncpg

from utils.logging import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS orchestrations (
    orchestration_id TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    user_query       TEXT NOT NULL,
    status           TEXT NOT NULL,
    response         TEXT NOT NULL DEFAULT '',
    error_message    TEXT NOT NULL DEFAULT '',
    created_at       DOUBLE PRECISION NOT NULL,
    finished_at      DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id               BIGSERIAL PRIMARY KEY,
    orchestration_id TEXT NOT NULL REFERENCES orchestrations(orchestration_id),
    seq              BIGINT NOT NULL,
    type             TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       DOUBLE PRECISION NOT NULL,
    UNIQUE (orchestration_id, seq)
);
"""


async def init_pool(database_url: str) -> bool:
    """Create the connection pool and bootstrap the archive tables.

    Args:
        database_url: Postgres URL, e.g.
            postgresql://orch:orch@localhost:5432/orch. Empty string
            disables persistence entirely (fail-open).

    Returns:
        True when the pool is ready, False otherwise (service keeps
        running without persistence).
    """
    global _pool
    if not database_url:
        logger.warning("db_disabled", reason="DATABASE_URL not set")
        return False
    try:
        _pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=5
        )
        async with _pool.acquire() as conn:
            await conn.execute(DDL)
        logger.info("db_ready")
        return True
    except Exception as e:
        logger.error("db_init_failed", error=str(e)[:500])
        _pool = None
        return False


async def close_pool() -> None:
    """Close the pool at application shutdown (idempotent)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool | None:
    """The active pool, or None when persistence is disabled."""
    return _pool
