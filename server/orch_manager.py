"""Orchestration lifecycle manager for the HTTP layer.

Bridges HTTP routes and the core orchestration graph:

- create(): spawn a background task running ``graph.astream`` (streaming
  events flow through utils/event.py via the shared callbacks) and bind
  the orchestration_id to a contextvar for that task.
- get/list: request metadata + live status from the StreamChannel.
- subscribe(): SSE queue for an existing orchestration (None → 404).
- delete(): cancel the background task and drop all channel state.
- _run_orchestration(): the graph runner. Terminal done/error events are
  OWNED by this manager — callbacks only emit worker/plan/tool events —
  so the final state is published exactly once, after the graph fully
  returns (or fails), with the final response already collected.
"""

import asyncio
import time
import uuid

from langchain_core.messages import HumanMessage

from orchestration.graph import build_graph
from orchestration.tools._kernel._web import close_crawler
from utils import event
from utils.callbacks import create_orchestration_config
from utils.logging import get_logger

logger = get_logger(__name__)


def _initial_state(user_query: str, conversation_id: str, orchestration_id: str) -> dict:
    """Minimal OrchestrationState for a fresh HTTP request (mirrors entry.py).

    Optional fields (e.g. compaction_checkpoint) are intentionally omitted:
    the core layer reads them with .get() and treats None as "nothing
    compacted yet".

    Args:
        user_query:       The user's request text.
        conversation_id:  Multi-turn anchor.
        orchestration_id: Id of this orchestration run.

    Returns:
        A dict compatible with the orchestration graph's input schema.
    """
    return {
        "messages": [HumanMessage(content=user_query)],
        "user_query": user_query,
        "conversation_id": conversation_id,
        "orchestration_id": orchestration_id,
        "plan": [],
        "active_sub_agent_count": 0,
        "orchestration_iteration": 0,
        "sub_agent_round_tasks": [],
        "sub_agent_outputs": {},
        "orchestration_status": "",
        "should_orchestration_pause": False,
        "should_orchestration_stop": False,
        "response": "",
        "total_tokens": 0,
        "start_at": "",
        "time_elapsed": 0.0,
        "error_message": "",
    }


class OrchManager:
    """Owns orchestration lifecycle: create / get / list / subscribe / delete."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}    # id -> background runner
        self._queries: dict[str, str] = {}           # id -> user query
        self._conversations: dict[str, str] = {}     # id -> conversation_id
        self._responses: dict[str, str] = {}         # id -> final response
        self._errors: dict[str, str] = {}            # id -> error message
        self._created_at: dict[str, float] = {}      # id -> timestamp

    def create(self, user_query: str, conversation_id: str) -> str:
        """Start an orchestration in the background; return its id (202).

        Creates the channel (status pending), spawns the background runner
        task, and registers request metadata. The router guarantees a
        non-blank conversation_id before this is called.

        Args:
            user_query:      The user's request text (non-empty).
            conversation_id: Multi-turn anchor (non-blank, required).

        Returns:
            The new orchestration_id (the HTTP layer replies 202).
        """
        orchestration_id = uuid.uuid4().hex
        conv_id = conversation_id

        # create channel using orchestration_id
        event.ensure_channel(orchestration_id)
        
        self._tasks[orchestration_id] = asyncio.create_task(
            self._run_orchestration(orchestration_id, user_query, conv_id)
        )

        self._queries[orchestration_id] = user_query
        self._conversations[orchestration_id] = conv_id
        self._created_at[orchestration_id] = time.time()

        logger.info(
            "orchestration_created",
            orchestration_id=orchestration_id,
            conversation_id=conv_id,
        )

        return orchestration_id

    def get_orchestration(self, orchestration_id: str) -> dict | None:
        """Snapshot: id / conversation / query / status / response / error.

        Returns:
            The assembled snapshot dict, or None when the orchestration
            does not exist (channel already removed).
        """
        channel = event.get_channel(orchestration_id)
        if channel is None:
            return None
        return {
            "orchestration_id": orchestration_id,
            "conversation_id": self._conversations[orchestration_id],
            "user_query": self._queries[orchestration_id],
            "status": channel.status,
            "response": self._responses.get(orchestration_id, ""),
            "error_message": self._errors.get(orchestration_id, ""),
            "created_at": self._created_at[orchestration_id],
        }

    def list_all_orchestrations(self) -> list[dict]:
        """All orchestration snapshots, newest first.

        Returns:
            Snapshots ordered by creation time, newest first. Empty list
            when no orchestration has been created yet.
        """
        snapshots = [self.get_orchestration(i) for i in self._tasks]
        return [s for s in snapshots if s is not None][::-1]

    def update_orchestration(
        self,
        orchestration_id: str,
        *,
        user_query: str | None = None,
    ) -> bool:
        """Update request metadata (query replacement, not yet re-run).

        Args:
            orchestration_id: Id of the orchestration to update.
            user_query:       New query text; skipped when None.

        Returns:
            True on success, False when the orchestration does not exist.
        """
        if orchestration_id not in self._tasks:
            return False
        if user_query is not None:
            self._queries[orchestration_id] = user_query
        return True

    def subscribe(self, orchestration_id: str) -> asyncio.Queue | None:
        """SSE queue for an existing orchestration; None -> 404.

        Returns:
            A bounded live queue, or None when the orchestration does not
            exist (the router turns this into 404).
        """
        if orchestration_id not in self._tasks:
            return None
        return event.subscribe(orchestration_id)

    def delete(self, orchestration_id: str) -> bool:
        """Cancel the runner and drop all channel state.

        Cancellation is asynchronous: the runner's CancelledError handler
        may still run after this returns, but its event pushes fail open
        once the channel is gone.

        Returns:
            True when the orchestration existed and was removed, False
            otherwise (no-op, maps to 404).
        """
        task = self._tasks.pop(orchestration_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._queries.pop(orchestration_id, None)
        self._conversations.pop(orchestration_id, None)
        self._responses.pop(orchestration_id, None)
        self._errors.pop(orchestration_id, None)
        self._created_at.pop(orchestration_id, None)
        return event.remove_channel(orchestration_id)

    async def _run_orchestration(
        self,
        orchestration_id: str,
        user_query: str,
        conversation_id: str,
    ) -> None:
        """Background runner: bind contextvar, stream the graph, publish terminal state.

        Terminal done/error events are published exactly once, after the
        graph fully returns (or fails), with the final response already
        collected. Cancellation publishes an error event and re-raises.

        Args:
            orchestration_id: Id of this run (contextvar + channel key).
            user_query:       The user's request text.
            conversation_id:  Multi-turn anchor.

        Returns:
            None.
        """
        token = event.bind_orchestration(orchestration_id)
        try:
            event.set_status(orchestration_id, "running")
            graph = build_graph()
            state = _initial_state(user_query, conversation_id, orchestration_id)
            async for mode, data in graph.astream(
                state,
                config=create_orchestration_config(),
                stream_mode=["updates", "messages"],
            ):
                # Streaming events already flow to the channel via callbacks.
                if mode == "updates" and isinstance(data, dict):
                    self._collect_updates(orchestration_id, data)
            event.set_status(orchestration_id, "done")
            event.push_event("done", {"status": "done"})
        except asyncio.CancelledError:
            event.set_status(orchestration_id, "failed")
            self._errors[orchestration_id] = "cancelled by user"
            event.push_event("error", {"error_message": "cancelled by user"})
            raise
        except Exception as e:
            logger.error(
                "orchestration_failed",
                orchestration_id=orchestration_id,
                error=str(e)[:500],
            )
            event.set_status(orchestration_id, "failed")
            self._errors[orchestration_id] = str(e)
            event.push_event("error", {"error_message": str(e)})
        finally:
            try:
                await close_crawler()
            except Exception:
                logger.warning("close_crawler_failed", exc_info=True)
            event.unbind_orchestration(token)

    def _collect_updates(self, orchestration_id: str, data: dict) -> None:
        """Track the final response emitted by end_orchestration.

        Only the "tools" node output carries the response field; other
        node updates are ignored.

        Args:
            orchestration_id: Id of this run (response registry key).
            data:             One "updates" payload (node name -> output).

        Returns:
            None.
        """
        for node_name, output in data.items():
            if node_name != "tools":
                continue
            items = output if isinstance(output, list) else [output]
            for item in items:
                if isinstance(item, dict) and item.get("response"):
                    self._responses[orchestration_id] = item["response"]


orch_manager = OrchManager()
