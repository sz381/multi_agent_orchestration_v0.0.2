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
- Cross-conversation memory: before each run the archived rows of the
  conversation are injected into the initial state (budget-driven
  sliding window: recent turns keep their full terminal messages, older
  turns degrade to their T2 summary). The terminal state (messages +
  compaction summary + diagnostics) is archived on finish.
"""

import asyncio
import time
import uuid

from langchain_core.load import load
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from orchestration.graph import build_graph
from orchestration.tools._kernel._web import close_crawler
from server.db.repository import (
    archive_orchestration,
    delete_archive,
    list_orchestrations,
    load_orchestration,
    load_conversation_history,
)
from utils import event
from utils.callbacks import create_orchestration_config
from utils.console import tool_summary
from utils.logging import get_logger
from utils.model import count_tokens
from utils.settings import settings

logger = get_logger(__name__)

# 跨会话注入预算：16 万 chars ≈ 8 万 tokens。从最新往回装，预算内的轮次
# 全量注入 messages（tool↔ai 配对完整），超出部分降级为 T2 summary。
HISTORY_BUDGET_CHARS = 160_000


def _initial_state(
    user_query: str,
    conversation_id: str,
    orchestration_id: str,
    history_messages: list | None = None,
) -> dict:
    """Minimal OrchestrationState for a fresh HTTP request (mirrors entry.py).

    Optional fields (e.g. compaction_checkpoint) are intentionally omitted:
    the core layer reads them with .get() and treats None as "nothing
    compacted yet".

    Args:
        user_query:       The user's request text.
        conversation_id:  Multi-turn anchor.
        orchestration_id: Id of this orchestration run.
        history_messages: Cross-conversation memory: BaseMessages from
                          previous orchestrations (None/empty → no memory
                          injection, the run starts fresh).

    Returns:
        A dict compatible with the orchestration graph's input schema.
    """
    messages = list(history_messages or []) + [HumanMessage(content=user_query)]
    return {
        "messages": messages,
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

    def _memory_snapshot(self, orchestration_id: str) -> dict | None:
        """In-memory snapshot (live state); None when the channel is gone.

        Args:
            orchestration_id: Id of the orchestration.

        Returns:
            The snapshot dict assembled from the live channel + request
            registries, or None when the orchestration is not in memory.
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

    async def get_orchestration(self, orchestration_id: str) -> dict | None:
        """Snapshot: live in-memory state first, DB archive as fallback.

        Finished orchestrations are dropped from memory right after they
        are archived, so their snapshots come from the DB here.

        Args:
            orchestration_id: Id of the orchestration.

        Returns:
            The assembled snapshot dict, or None when the orchestration
            does not exist (neither in memory nor archived).
        """
        snapshot = self._memory_snapshot(orchestration_id)
        if snapshot is not None:
            return snapshot
        return await load_orchestration(orchestration_id)

    async def list_all_orchestrations(self) -> list[dict]:
        """All snapshots (memory + archive), newest first.

        Live orchestrations come from memory; archived ones come from the
        DB. Snapshots are merged by id (memory wins) and ordered by
        creation time, newest first.

        Returns:
            Merged snapshots, newest first. Empty list when nothing
            exists.
        """
        mem = [
            s
            for s in (
                self._memory_snapshot(i) for i in list(self._tasks)
            )
            if s is not None
        ]
        merged = {s["orchestration_id"]: s for s in mem}
        for s in await list_orchestrations():
            merged.setdefault(s["orchestration_id"], s)
        return sorted(
            merged.values(), key=lambda s: s["created_at"], reverse=True
        )

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

    async def delete(self, orchestration_id: str) -> bool:
        """Cancel the runner, drop memory state and the DB archive.

        DELETE means permanent removal: the archived rows are deleted as
        well, so the orchestration disappears from history and lists.

        Returns:
            True when the orchestration existed (in memory and/or in the
            DB) and was removed, False otherwise (no-op, maps to 404).
        """
        task = self._tasks.pop(orchestration_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._queries.pop(orchestration_id, None)
        self._conversations.pop(orchestration_id, None)
        self._responses.pop(orchestration_id, None)
        self._errors.pop(orchestration_id, None)
        self._created_at.pop(orchestration_id, None)
        removed = event.remove_channel(orchestration_id)
        db_deleted = await delete_archive(orchestration_id)
        return removed or db_deleted

    def _drop_from_memory(self, orchestration_id: str) -> None:
        """Remove an archived orchestration from memory (DB owns it now).

        Called only after a successful archive: from this point on every
        query and replay for the id is served from the DB. Fallback mode
        (archive failed) keeps the orchestration in memory instead.

        Args:
            orchestration_id: Id of the archived run.

        Returns:
            None.
        """
        self._tasks.pop(orchestration_id, None)
        self._queries.pop(orchestration_id, None)
        self._conversations.pop(orchestration_id, None)
        self._responses.pop(orchestration_id, None)
        self._errors.pop(orchestration_id, None)
        self._created_at.pop(orchestration_id, None)
        event.remove_channel(orchestration_id)

    async def _load_conversation_chunks(self, conversation_id: str) -> list:
        """Budget-driven sliding window over archived rows (oldest→newest).

        Recent turns within ``HISTORY_BUDGET_CHARS`` keep their full
        terminal messages (tool↔ai pairing intact); older turns degrade
        to their T2 summary, merged into one leading SystemMessage.
        Fail-open: any DB/parse failure degrades to a fresh conversation
        (no memory injection), never blocks the run.

        Args:
            conversation_id: Multi-turn anchor to load history for.

        Returns:
            BaseMessage chunks ordered oldest→newest, or [] when the
            conversation has no archived (done) rows.
        """
        rows = await load_conversation_history(conversation_id)
        done = [
            r for r in rows if r.get("status") == "done" and r.get("messages")
        ]
        if not done:
            return []

        # 从最新往回装：预算内全量 messages，超出部分降级为 summary。
        budgeted: list[dict] = []
        used = 0
        for r in reversed(done):
            chars = sum(
                len(str((m.get("kwargs") or {}).get("content") or ""))
                for m in r["messages"]
            )
            if used + chars > HISTORY_BUDGET_CHARS:
                break
            budgeted.append(r)
            used += chars
        budgeted.reverse()
        budgeted_ids = {r["orchestration_id"] for r in budgeted}

        # 超出预算轮次的摘要（预算内轮次自身的 T2 摘要跟随其 messages）。
        older_summaries = [
            r["summary"]
            for r in done
            if r.get("summary") and r["orchestration_id"] not in budgeted_ids
        ]
        chunks: list = []
        if older_summaries:
            chunks.append(
                SystemMessage(
                    content="## PREVIOUS CONVERSATIONS SUMMARY\n"
                    + "\n\n".join(older_summaries)
                )
            )
        for r in budgeted:
            if r.get("summary"):
                chunks.append(
                    SystemMessage(content="## PREVIOUS TURN SUMMARY\n" + r["summary"])
                )
            for m in r["messages"]:
                try:
                    # allowed_objects="messages"：只允许反序列化为消息类（
                    # 存档是可信数据，白名单同时防未来 schema 漂移）。
                    msg = load(m, allowed_objects="messages")
                    # 注入历史只承载文本记忆：清掉 AIMessage.usage_metadata，
                    # 避免历史账单混入本轮 count_tokens（打印/落库统计失真）。
                    if getattr(msg, "usage_metadata", None) is not None:
                        msg.usage_metadata = None
                    chunks.append(msg)
                except Exception as e:
                    logger.warning(
                        "history_message_load_failed",
                        orchestration_id=r["orchestration_id"],
                        error=str(e)[:200],
                    )
        logger.info(
            "conversation_history_injected",
            conversation_id=conversation_id,
            full_turns=len(budgeted),
            summary_turns=len(done) - len(budgeted),
            estimated_chars=used,
            message_count=len(chunks),
        )
        return chunks

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
            history_chunks = await self._load_conversation_chunks(conversation_id)
            injected_ids = {
                m.id for m in history_chunks if getattr(m, "id", None)
            }
            state = _initial_state(
                user_query,
                conversation_id,
                orchestration_id,
                history_messages=history_chunks,
            )
            final_state: dict | None = None
            async for mode, data in graph.astream(
                state,
                config=create_orchestration_config(),
                stream_mode=["updates", "messages", "values"],
            ):
                # Streaming events already flow to the channel via callbacks.
                if mode == "updates" and isinstance(data, dict):
                    self._collect_updates(orchestration_id, data)
                elif mode == "values" and isinstance(data, dict):
                    # values = full state after each super-step; the last
                    # one is the terminal state (post-T2 messages +
                    # compaction checkpoint + diagnostics).
                    final_state = data
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
            if await self._archive_orchestration(
                orchestration_id, final_state, injected_ids
            ):
                # Archive succeeded: the DB now owns the finished run,
                # drop it from memory (fallback mode keeps it in memory
                # when the archive failed).
                self._drop_from_memory(orchestration_id)
            event.unbind_orchestration(token)

    async def _archive_orchestration(
        self,
        orchestration_id: str,
        final_state: dict | None = None,
        injected_ids: set[str] | None = None,
    ) -> bool:
        """Best-effort terminal archive: snapshot + full event list + memory.

        Called from the runner's finally block after the terminal event
        was published, so the archive always contains the terminal
        done/error event. When the channel is already gone (user DELETE
        cancelled the run) nothing is archived — the run never finished.

        Args:
            orchestration_id: Id of the finished run.
            final_state:      Terminal state from the last "values"
                              stream mode (None on failure paths); its
                              messages / compaction_checkpoint are
                              archived for cross-conversation memory.
            injected_ids:     Ids of the history messages injected into
                              this run. They are filtered out before
                              archiving so each row stores only this
                              run's own increments — otherwise every row
                              would carry the full history and cascade
                              into O(n²) duplication across turns.

        Returns:
            True when persisted (the caller may drop the memory state),
            False otherwise (fail-open: DB errors are logged inside the
            repository and never surface here).
        """
        channel = event.get_channel(orchestration_id)
        if channel is None:
            return False
        snapshot = self._memory_snapshot(orchestration_id)
        if snapshot is None:
            return False
        state = final_state or {}
        checkpoint = state.get("compaction_checkpoint")
        messages = state.get("messages") or []
        if injected_ids:
            messages = [
                m
                for m in messages
                if (getattr(m, "id", None) or "") not in injected_ids
            ]
        token_stats = count_tokens(messages)
        # 总账单 = orchestrator 主状态 messages + 本轮全部 sub-agent 消耗
        # （sub_agent_outputs 只存本轮 fanout 结果，历史轮不在此列）。
        sub_total = sum(
            (output or {}).get("token_used", 0)
            for output in (state.get("sub_agent_outputs") or {}).values()
        )
        return await archive_orchestration(
            orchestration_id=orchestration_id,
            conversation_id=snapshot["conversation_id"],
            user_query=snapshot["user_query"],
            status=snapshot["status"],
            response=snapshot["response"],
            error_message=snapshot["error_message"],
            created_at=snapshot["created_at"],
            events=channel.snapshot(),
            messages=messages,
            summary=checkpoint.summary if checkpoint else "",
            total_tokens=token_stats["total_tokens"] + sub_total,
            time_elapsed=max(0.0, time.time() - snapshot["created_at"]),
        )

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
                if not isinstance(item, dict):
                    continue
                if item.get("response"):
                    self._responses[orchestration_id] = item["response"]
                self._print_console_blocks(item)

    def _print_console_blocks(self, item: dict) -> None:
        """Render PLAN/FANOUT blocks for tool messages to uvicorn stdout.

        Args:
            item: One "tools" node update item (dict with messages).

        Returns:
            None.
        """
        if not settings.console_print:
            return
        for msg in item.get("messages", []):
            if isinstance(msg, ToolMessage):
                summary = tool_summary(msg)
                if summary:
                    print(summary, flush=True)


orch_manager = OrchManager()
