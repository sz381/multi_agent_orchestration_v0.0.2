"""Archive: persist a finished orchestration atomically + read back.

Single transaction per orchestration: one row in ``orchestrations``
(the result snapshot + terminal messages + T2 summary) plus one row per
event in ``events`` (the process replay, seq-ordered). Either everything
lands or nothing does — the archive is never left half-written.

The ``messages`` column is the cross-conversation memory asset: each
archived row stores the terminal (post-T2) message list, serialized via
``langchain_core.load.dumpd`` so ``loads`` can restore BaseMessage
instances for injection into the next orchestration.

Fail-open: any DB failure logs and returns False / empty; the
orchestration lifecycle is never blocked by the archive.
"""

import json
import time

from langchain_core.load import dumpd

from utils.logging import get_logger
from .session import get_pool

logger = get_logger(__name__)


def _parse_messages(raw) -> list:
    """Normalize the stored messages column to a list of dumpd dicts.

    asyncpg returns jsonb as a JSON string by default (no codec
    registered), so the raw value may be a str or a list depending on
    the code path; None (legacy rows) becomes an empty list.

    Args:
        raw: Raw value of the messages column.

    Returns:
        A list of dumpd dicts (possibly empty).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw


async def archive_orchestration(
    orchestration_id: str,
    conversation_id: str,
    user_query: str,
    status: str,
    response: str,
    error_message: str,
    created_at: float,
    events: list[dict],
    messages: list | None = None,
    summary: str = "",
    total_tokens: int = 0,
    time_elapsed: float = 0.0,
) -> bool:
    """Persist the snapshot + full event list in one transaction.

    Args:
        orchestration_id: Id of the finished run.
        conversation_id:  Multi-turn anchor.
        user_query:       The user's request text.
        status:           Terminal status (done / failed).
        response:         Final response text (end_orchestration).
        error_message:    Non-empty on failure.
        created_at:       Run creation timestamp.
        events:           Events from ``StreamChannel.snapshot()``; the
                          ``type`` key is stored in the type column, the
                          rest goes into the JSONB payload column.
        messages:         Terminal (post-T2) message list; serialized
                          with ``dumpd`` into the JSONB column. This is
                          the cross-conversation memory asset.
        summary:          T2 accumulated summary (compaction checkpoint).
        total_tokens:     Terminal token counter from the final state.
        time_elapsed:     Terminal elapsed seconds from the final state.

    Returns:
        True when persisted, False on any failure (fail-open).
    """
    pool = get_pool()
    if pool is None:
        logger.debug(
            "archive_skipped",
            reason="db not initialized",
            orchestration_id=orchestration_id,
        )
        return False
    archived_at = time.time()
    try:
        messages_json = (
            json.dumps([dumpd(m) for m in messages], ensure_ascii=False)
            if messages
            else "[]"
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO orchestrations (
                        orchestration_id, conversation_id, user_query,
                        status, response, error_message, created_at,
                        finished_at, messages, summary, total_tokens,
                        time_elapsed
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
                    """,
                    orchestration_id,
                    conversation_id,
                    user_query,
                    status,
                    response,
                    error_message,
                    created_at,
                    archived_at,
                    messages_json,
                    summary,
                    total_tokens,
                    time_elapsed,
                )
                await conn.executemany(
                    """
                    INSERT INTO events (
                        orchestration_id, seq, type, payload, created_at
                    )
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    """,
                    [
                        (
                            orchestration_id,
                            seq,
                            evt.get("type", ""),
                            json.dumps(
                                {k: v for k, v in evt.items() if k != "type"},
                                ensure_ascii=False,
                            ),
                            archived_at,
                        )
                        for seq, evt in enumerate(events)
                    ],
                )
        logger.info(
            "orchestration_archived",
            orchestration_id=orchestration_id,
            event_count=len(events),
        )
        return True
    except Exception as e:
        logger.error(
            "archive_failed",
            orchestration_id=orchestration_id,
            error=str(e)[:500],
        )
        return False


async def load_orchestration(orchestration_id: str) -> dict | None:
    """Read one archived snapshot from the DB (None when not archived).

    Args:
        orchestration_id: Id of the archived run.

    Returns:
        The same snapshot shape as the in-memory version, or None when
        the orchestration was never archived (or the DB is disabled).
    """
    pool = get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM orchestrations WHERE orchestration_id = $1",
                orchestration_id,
            )
        if row is None:
            return None
        return {
            "orchestration_id": row["orchestration_id"],
            "conversation_id": row["conversation_id"],
            "user_query": row["user_query"],
            "status": row["status"],
            "response": row["response"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "messages": _parse_messages(row["messages"]),
            "summary": row["summary"] or "",
            "total_tokens": row["total_tokens"],
            "time_elapsed": row["time_elapsed"],
        }
    except Exception as e:
        logger.error(
            "load_orchestration_failed",
            orchestration_id=orchestration_id,
            error=str(e)[:500],
        )
        return None


async def list_orchestrations() -> list[dict]:
    """All archived snapshots, newest first.

    Returns:
        Snapshots ordered by creation time, newest first; empty list when
        nothing has been archived yet.
    """
    pool = get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orchestrations ORDER BY created_at DESC"
            )
        return [
            {
                "orchestration_id": r["orchestration_id"],
                "conversation_id": r["conversation_id"],
                "user_query": r["user_query"],
                "status": r["status"],
                "response": r["response"],
                "error_message": r["error_message"],
                "created_at": r["created_at"],
                "messages": _parse_messages(r["messages"]),
                "summary": r["summary"] or "",
                "total_tokens": r["total_tokens"],
                "time_elapsed": r["time_elapsed"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("list_orchestrations_failed", error=str(e)[:500])
        return []


async def load_events(orchestration_id: str) -> list[dict] | None:
    """Replay events of an archived run, seq-ordered.

    The stored event dict is reassembled as {"type": ..., **payload} so
    the router can feed it straight into the SSE bridge.

    Args:
        orchestration_id: Id of the archived run.

    Returns:
        The full event list (may be empty), or None when the DB is
        disabled or the read failed.
    """
    pool = get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT type, payload FROM events
                WHERE orchestration_id = $1
                ORDER BY seq
                """,
                orchestration_id,
            )
        return [
            {"type": r["type"], **json.loads(r["payload"])}
            for r in rows
        ]
    except Exception as e:
        logger.error(
            "load_events_failed",
            orchestration_id=orchestration_id,
            error=str(e)[:500],
        )
        return None


async def load_conversation_history(conversation_id: str) -> list[dict]:
    """All archived rows of one conversation, oldest first.

    Serves the cross-conversation memory injection: the caller picks a
    budget-driven window of recent rows (full messages) and falls back
    to summaries for the rest.

    Args:
        conversation_id: Multi-turn anchor to load.

    Returns:
        Rows ordered by creation time, oldest first; each carries
        ``messages`` (raw dumpd dicts), ``summary``, ``user_query``,
        ``response`` and ``status``. Empty list when nothing archived
        (or the DB is disabled / failed).
    """
    pool = get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT orchestration_id, user_query, status, response,
                       messages, summary, created_at
                FROM orchestrations
                WHERE conversation_id = $1
                ORDER BY created_at ASC
                """,
                conversation_id,
            )
        return [
            {
                "orchestration_id": r["orchestration_id"],
                "user_query": r["user_query"],
                "status": r["status"],
                "response": r["response"],
                "messages": _parse_messages(r["messages"]),
                "summary": r["summary"] or "",
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(
            "load_conversation_history_failed",
            conversation_id=conversation_id,
            error=str(e)[:500],
        )
        return []


async def delete_archive(orchestration_id: str) -> bool:
    """Permanently delete an archived orchestration (DELETE semantics).

    Args:
        orchestration_id: Id of the archived run.

    Returns:
        True when the orchestration row existed and was deleted, False
        otherwise (nothing archived / DB disabled / failure).
    """
    pool = get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM events WHERE orchestration_id = $1",
                    orchestration_id,
                )
                result = await conn.execute(
                    "DELETE FROM orchestrations WHERE orchestration_id = $1",
                    orchestration_id,
                )
        return result.startswith("DELETE 1")
    except Exception as e:
        logger.error(
            "delete_archive_failed",
            orchestration_id=orchestration_id,
            error=str(e)[:500],
        )
        return False
