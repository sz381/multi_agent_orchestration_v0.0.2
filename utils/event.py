"""Event bridge between orchestration core and the HTTP streaming layer.

Single-process, in-memory fan-out bus. Producers are synchronous LLM
callbacks (running inside the async orchestration task); consumers are
async SSE endpoints. The two ends never touch: callbacks call
push_stream / push_event, the router subscribes and drains an
asyncio.Queue.

Identity & fail-open
--------------------
The orchestration_id is bound to a contextvar when an orchestration
starts; callbacks read it from the current async context (copied into
the task by asyncio). entry.py (CLI) never binds it, so push_* degrade
to debug-level logging and return False — the CLI keeps running with
zero changes.

Buffers (per channel)
---------------------
- subscribers: one asyncio.Queue per SSE connection; live events are
  delivered via put_nowait (non-blocking, sync-producer / async-consumer
  bridge). A slow consumer that overflows its queue is dropped.
- replay:      structured events (plan / tool_status / tool_output /
  worker_done / file_changes / done / error) kept for reconnect replay;
  capped at MAX_REPLAY_BYTES, oldest dropped first.
- aggregates:  per-sub-agent concatenated token text, used for the
  full-view replay (token deltas go ONLY here, never into replay, since
  they are the bulk of the bytes).

Payloads are plain type-tagged dicts, NOT completion.py schema: this
module stays schema-agnostic. Keys mirror Delta fields so the router can
rebuild Choice/Delta with Delta.model_validate(evt); sub_agent_id /
sub_agent_name travel as extra keys (pydantic ignores them by default)
and are lifted onto Choice by the router.
"""

import asyncio
from contextvars import ContextVar, Token

from utils.logging import get_logger

logger = get_logger(__name__)

# Keep the latest ~5 MB of structured events per channel for replay.
MAX_REPLAY_BYTES = 5 * 1024 * 1024

# Live queue capacity per SSE subscriber; overflow drops the consumer.
QUEUE_MAXSIZE = 1024


class StreamChannel:
    """Live streaming state of a single orchestration run.

    Tracks the status machine (pending -> running -> done / failed) and
    holds the three buffers documented at module level.
    """

    def __init__(self, orchestration_id: str) -> None:
        self.orchestration_id = orchestration_id
        self.status: str = "pending"  # pending / running / done / failed
        self.subscribers: list[asyncio.Queue] = []
        self.replay: list[dict] = []
        self._replay_bytes = 0
        self.aggregates: dict[str, dict] = {}  # sub_agent_id -> {"name", "text"}

    def subscribe(self) -> asyncio.Queue:
        """Open a live queue for one SSE connection.

        Returns:
            A bounded queue (QUEUE_MAXSIZE) that receives a copy of every
            pushed event until unsubscribed.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Drop a previously subscribed queue (idempotent).

        Args:
            queue: Queue previously returned by subscribe().

        Returns:
            None. Missing queues are silently ignored.
        """
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass

    def snapshot(self) -> list[dict]:
        """Reconnect replay: per-sub-agent full text first, then events.

        Returns:
            One "stream" event per sub-agent carrying the aggregated full
            text, then the structured replay events in push order. Empty
            list when nothing has been pushed yet.
        """
        events: list[dict] = []
        for sub_agent_id, agg in self.aggregates.items():
            events.append({
                "type": "stream",
                "content": agg["text"],
                "sub_agent_id": sub_agent_id,
                "sub_agent_name": agg["name"],
            })
        events.extend(self.replay)
        return events

    def broadcast(self, event: dict, replay: bool = False) -> None:
        """Deliver to every live subscriber; optionally store for replay.

        Uses put_nowait: a slow consumer that overflows its queue is
        dropped (unsubscribed). The replay log is capped at
        MAX_REPLAY_BYTES, oldest events dropped first.

        Args:
            event:  Type-tagged event dict to fan out.
            replay: Whether to also append it to the replay log.

        Returns:
            None.
        """
        stale: list[asyncio.Queue] = []
        for queue in self.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Consumer too slow: drop the subscription.
                stale.append(queue)
        for queue in stale:
            self.unsubscribe(queue)
        if replay:
            self.replay.append(event)
            self._replay_bytes += len(str(event))
            while self._replay_bytes > MAX_REPLAY_BYTES and self.replay:
                dropped = self.replay.pop(0)
                self._replay_bytes -= len(str(dropped))


# Global registry: orchestration_id -> StreamChannel (single event loop).
_channels: dict[str, StreamChannel] = {}

# Current orchestration bound to the async context (set by orch_manager).
_current_orchestration_id: ContextVar[str | None] = ContextVar(
    "current_orchestration_id", default=None
)


def bind_orchestration(orchestration_id: str) -> Token:
    """Bind the orchestration_id to the current async context.

    Returns:
        Token to pass to unbind_orchestration() when the run ends.
    """
    return _current_orchestration_id.set(orchestration_id)


def unbind_orchestration(token: Token) -> None:
    """Restore the context after the orchestration finished.

    Args:
        token: Token returned by bind_orchestration().

    Returns:
        None.
    """
    _current_orchestration_id.reset(token)


def ensure_channel(orchestration_id: str) -> StreamChannel:
    """Get or create the channel (called when an orchestration starts).

    Returns:
        The existing channel for the id, or a fresh one (status pending).
    """
    channel = _channels.get(orchestration_id)
    if channel is None:
        channel = StreamChannel(orchestration_id)
        _channels[orchestration_id] = channel
    return channel


def get_channel(orchestration_id: str) -> StreamChannel | None:
    """Look up a channel without creating one.

    Returns:
        The channel, or None when the orchestration id is unknown.
    """
    return _channels.get(orchestration_id)


def remove_channel(orchestration_id: str) -> bool:
    """Drop all state for an orchestration (DELETE handler).

    Returns:
        True if a channel existed and was removed, False otherwise.
    """
    return _channels.pop(orchestration_id, None) is not None


def subscribe(orchestration_id: str) -> asyncio.Queue | None:
    """Open a live queue for an SSE consumer; None if id unknown (404).

    Returns:
        A bounded live queue, or None when the orchestration id is
        unknown (the router turns this into 404).
    """
    channel = _channels.get(orchestration_id)
    if channel is None:
        return None
    return channel.subscribe()


def unsubscribe(orchestration_id: str, queue: asyncio.Queue) -> None:
    """Close a live queue (SSE generator finally block).

    Args:
        orchestration_id: Id the queue was subscribed for.
        queue:            Queue previously obtained from subscribe().

    Returns:
        None. No-op when the channel is already gone.
    """
    channel = _channels.get(orchestration_id)
    if channel is not None:
        channel.unsubscribe(queue)


def set_status(orchestration_id: str, status: str) -> bool:
    """Update channel status: pending -> running -> done / failed.

    Returns:
        True when the channel exists and was updated, False otherwise.
    """
    channel = _channels.get(orchestration_id)
    if channel is None:
        return False
    channel.status = status
    return True


def push_stream(content: str, sub_agent_id: str, sub_agent_name: str) -> bool:
    """Push one token delta (called by on_llm_new_token, sync producer).

    Fail-open: no bound orchestration (CLI mode) or missing channel
    degrades to debug logging and False; never raises.

    Args:
        content:        One token delta (raw text).
        sub_agent_id:   Id of the emitting sub-agent (or "orchestrator").
        sub_agent_name: Human-readable label of the emitting sub-agent.

    Returns:
        True when the delta was delivered, False otherwise (fail-open).
    """
    orchestration_id = _current_orchestration_id.get()
    if orchestration_id is None:
        logger.debug("push_stream skipped: no orchestration bound")
        return False
    channel = _channels.get(orchestration_id)
    if channel is None:
        logger.debug(
            "push_stream skipped: channel not found",
            orchestration_id=orchestration_id,
        )
        return False
    agg = channel.aggregates.setdefault(
        sub_agent_id, {"name": sub_agent_name, "text": ""}
    )
    agg["text"] += content
    channel.broadcast({
        "type": "stream",
        "content": content,
        "sub_agent_id": sub_agent_id,
        "sub_agent_name": sub_agent_name,
    })
    return True


def push_event(event: str, data: dict) -> bool:
    """Push one structured event (plan / tool_status / tool_output /
    worker_done / file_changes / done / error).

    `data` keys must mirror the Delta payload shape (e.g. {"tool_status":
    {...}}); sub_agent_id / sub_agent_name may be included and are
    lifted onto Choice by the router. The event is stored in replay.

    Fail-open like push_stream.

    Args:
        event: Event type (plan / tool_status / tool_output /
               worker_done / file_changes / done / error).
        data:  Payload dict; keys mirror the Delta fields, with optional
               sub_agent_id / sub_agent_name lifted onto Choice.

    Returns:
        True when the event was delivered and replayed, False otherwise
        (fail-open).
    """
    orchestration_id = _current_orchestration_id.get()
    if orchestration_id is None:
        logger.debug("push_event skipped: no orchestration bound", event_type=event)
        return False
    channel = _channels.get(orchestration_id)
    if channel is None:
        logger.debug(
            "push_event skipped: channel not found",
            orchestration_id=orchestration_id,
            event_type=event,
        )
        return False
    payload = {"type": event, **data}
    channel.broadcast(payload, replay=True)
    return True
