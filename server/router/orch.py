"""HTTP routes for orchestration lifecycle and SSE streaming.

- POST   /api/orchestrations              → 202, start a background orchestration
- GET    /api/orchestrations              → all snapshots, newest first
- GET    /api/orchestrations/{id}         → snapshot or 404
- GET    /api/orchestrations/{id}/events  → SSE: replay snapshot, then live events
- DELETE /api/orchestrations/{id}         → cancel the run and drop its state

Event frames are plain dicts from utils/event.py; this router rebuilds
completion.py schema objects from them (Delta.model_validate ignores the
extra sub_agent_* identity keys, which are lifted onto Choice).
"""

from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from server.db.repository import load_events
from server.schema.completion import Choice, Delta, StreamChunk
from server.schema.orch import CreateOrchestrationRequest
from server.service.orch_manager import orch_manager
from utils import event
from utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _to_choice(evt: dict, orchestration_id: str, conversation_id: str) -> Choice:
    """Rebuild a Choice from a channel event dict (schema-agnostic bridge).

    Terminal events ("done"/"error") map to Choice.status with delta=None;
    everything else is validated into Delta (extra sub_agent_* keys are
    ignored by pydantic on Delta and lifted onto Choice instead).

    Args:
        evt:              Type-tagged event dict from the channel.
        orchestration_id: Id of the run (echoed on every frame).
        conversation_id:  Multi-turn anchor (echoed on every frame).

    Returns:
        A Choice ready for serialization into an SSE frame.
    """
    if evt["type"] == "done":
        return Choice(
            orchestration_id=orchestration_id,
            conversation_id=conversation_id,
            status="done",
            finish_reason="stop",
            delta=None,  # terminal frames carry no delta block
        )
    if evt["type"] == "error":
        return Choice(
            orchestration_id=orchestration_id,
            conversation_id=conversation_id,
            status="failed",
            error_message=evt.get("error_message"),
            delta=None,  # terminal frames carry no delta block
        )
    delta = Delta.model_validate(evt)
    return Choice(
        orchestration_id=orchestration_id,
        conversation_id=conversation_id,
        delta=delta,
        sub_agent_id=evt.get("sub_agent_id"),
        sub_agent_name=evt.get("sub_agent_name"),
    )


def _sse_frame(choice: Choice) -> str:
    """Serialize one Choice into an SSE data frame.

    Args:
        choice: The frame's candidate reply.

    Returns:
        A "data: {...}\n\n" string; None fields are omitted.
    """
    chunk = StreamChunk(choices=[choice])
    return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"


async def _event_stream(
    orchestration_id: str,
    queue: object,
    db_events: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    """SSE: in-memory replay+live, or one-shot replay from the DB archive.

    Memory mode (queue is not None): replays the channel snapshot, then
    streams live events until a terminal one. Replays first (subscribe +
    snapshot are synchronous, so nothing is duplicated into the live
    queue) and closes immediately when the snapshot already carries a
    terminal event. When the orchestration was deleted between
    subscribe() and this generator starting, a terminal error frame is
    emitted instead of hanging on an empty queue.

    DB mode (db_events is not None): the orchestration finished and was
    dropped from memory; its archived events are replayed once and the
    stream closes.

    Args:
        orchestration_id: Id of the run being streamed.
        queue:            Live queue from orch_manager.subscribe()
                          (None in DB mode).
        db_events:        Archived event list when streaming from the
                          DB (None in memory mode).

    Yields:
        SSE frame strings ("data: {...}\n\n").
    """
    snapshot = await orch_manager.get_orchestration(orchestration_id)
    if db_events is not None:
        logger.debug(
            "events_replayed_from_db",
            orchestration_id=orchestration_id,
            event_count=len(db_events),
        )
        conversation_id = (
            snapshot["conversation_id"] if snapshot is not None else ""
        )
        for evt in db_events:
            yield _sse_frame(_to_choice(evt, orchestration_id, conversation_id))
            if evt["type"] in ("done", "error"):
                # The archive carries the terminal event: close.
                return
        return
    channel = event.get_channel(orchestration_id)
    if channel is None or snapshot is None:
        # Deleted between subscribe() and generator start (concurrent
        # DELETE): send a terminal error frame instead of hanging.
        yield _sse_frame(_to_choice(
            {"type": "error", "error_message": "orchestration deleted"},
            orchestration_id,
            "",
        ))
        return
    conversation_id = snapshot["conversation_id"]
    try:
        # Replay first: subscribe + snapshot are synchronous, so no event
        # can slip between them (single-threaded event loop) — nothing is
        # duplicated into the live queue.
        for evt in channel.snapshot():
            yield _sse_frame(_to_choice(evt, orchestration_id, conversation_id))
            if evt["type"] in ("done", "error"):
                # Snapshot already carries the terminal event: close.
                return
        # Live events until the terminal state.
        while True:
            evt = await queue.get()
            yield _sse_frame(_to_choice(evt, orchestration_id, conversation_id))
            if evt["type"] in ("done", "error"):
                break
    finally:
        event.unsubscribe(orchestration_id, queue)


@router.post("/orchestrations", status_code=202, tags=["Orchestrations"])
async def create_orchestration(body: CreateOrchestrationRequest) -> dict:
    """Start an orchestration in the background; returns its id (202).

    Args:
        body: Request payload (user_query + conversation_id).

    Returns:
        Dict with orchestration_id / conversation_id / status.

    Raises:
        400: When user_query or conversation_id is blank (whitespace-only
            strings count as blank).
        500: When the freshly created orchestration has no retrievable
            snapshot (internal state inconsistency).
    """
    if not body.user_query.strip():
        raise HTTPException(status_code=400, detail="user_query must not be empty")

    if not body.conversation_id or not body.conversation_id.strip():
        raise HTTPException(status_code=400, detail="conversation_id is required")

    orchestration_id = orch_manager.create(body.user_query, body.conversation_id)

    try:
        snapshot = await orch_manager.get_orchestration(orchestration_id)
    except Exception as e:
        logger.error(
            "orchestration_snapshot_error",
            orchestration_id=orchestration_id,
            error=str(e)[:500],
        )
        raise HTTPException(
            status_code=500,
            detail="failed to read the orchestration snapshot",
        ) from e

    if snapshot is None:
        logger.error(
            "orchestration_snapshot_missing",
            orchestration_id=orchestration_id,
        )
        raise HTTPException(
            status_code=500,
            detail="orchestration created but its snapshot is unavailable",
        )
        
    try:
        conv_id = snapshot["conversation_id"]
        status = snapshot["status"]
    except (KeyError, TypeError) as exc:
        logger.error(
            "orchestration_snapshot_key_error",
            orchestration_id=orchestration_id,
            error=str(exc)[:500],
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="failed to read the orchestration snapshot",
        ) from exc

    return {
        "orchestration_id": orchestration_id,
        "conversation_id": conv_id,
        "status": status,
    }


@router.get("/orchestrations", tags=["Orchestrations"])
async def list_orchestrations() -> list[dict]:
    """All orchestration snapshots (memory + archive), newest first.

    Returns:
        Snapshots ordered newest first; empty list when none exist.
    """
    return await orch_manager.list_all_orchestrations()


@router.get("/orchestrations/{orchestration_id}", tags=["Orchestrations"])
async def get_orchestration(orchestration_id: str) -> dict:
    """Snapshot of one orchestration (status / response / error).

    Served from memory for live runs; archived runs are read from the
    DB, so finished orchestrations stay queryable after restarts.

    Raises:
        HTTPException(404): When the orchestration does not exist.
    """
    snapshot = await orch_manager.get_orchestration(orchestration_id)

    if snapshot is None:
        raise HTTPException(status_code=404, detail="orchestration not found")

    return snapshot


@router.get("/orchestrations/{orchestration_id}/events", tags=["Orchestrations"])
async def stream_events(orchestration_id: str) -> StreamingResponse:
    """SSE stream: memory replay+live, or one-shot replay from the DB.

    Live orchestrations are streamed from memory (replay, then live
    events). Finished orchestrations were dropped from memory after
    archiving, so their full event history is replayed from the DB — the
    stream replays everything once and closes.

    Raises:
        HTTPException(404): When the orchestration does not exist.
    """
    queue = orch_manager.subscribe(orchestration_id)

    if queue is not None:
        return StreamingResponse(
            _event_stream(orchestration_id, queue),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Not in memory: replay from the DB archive (finished runs).
    snapshot = await orch_manager.get_orchestration(orchestration_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="orchestration not found")
    db_events = await load_events(orchestration_id)
    if db_events is None:
        raise HTTPException(status_code=404, detail="orchestration not found")

    return StreamingResponse(
        _event_stream(orchestration_id, queue, db_events=db_events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/orchestrations/{orchestration_id}", status_code=204, tags=["Orchestrations"])
async def delete_orchestration(orchestration_id: str) -> None:
    """Cancel the run (if still running) and permanently remove it.

    Memory state is dropped and the DB archive is deleted, so the
    orchestration disappears from history and lists.

    Raises:
        HTTPException(404): When the orchestration does not exist.
    """
    if not await orch_manager.delete(orchestration_id):
        raise HTTPException(status_code=404, detail="orchestration not found")
