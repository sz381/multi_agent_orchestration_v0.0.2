"""
T2 Auto-Compact — core of incremental compaction (Context Engineering v2).

Runs after the budget check (T0/T1) and before the LLM request: compacts only
middle messages added since the last compaction checkpoint, calls a cheap model
(deepseek) to generate an incremental summary, and emits a list of RemoveMessages
that the LangGraph ``add_messages`` reducer applies to remove the original text
from state.

Core invariants:

- Incremental: ``CompactionCheckpoint.last_compacted_id`` is the compaction
  cursor (an anchor message id that stays in history); only messages added
  after the cursor are compacted each time, never re-abstracting an already
  compacted segment, avoiding information dilution (lost-in-the-middle).
- Pair protection: AIMessage(tool_calls) ⇄ ToolMessage(tool_call_id) pairs are
  never split, preventing OpenAI-compatible API 400 errors (tool_calls without
  a following tool message).
- Circuit breaker: after consecutive failures reach the threshold, the caller
  degrades to T0/T1 only (Snip + Microcompact).
- Recursion guard: skips when ``checkpoint.in_progress`` is True, preventing
  nested triggers.

Budget decisions (``compact_threshold = budget - MAX_SUMMARY_OUTPUT - BUFFER``)
are out of scope here; they belong to the pre-request hook (Phase 2
``context_pipeline``). This module only performs the "compaction action" itself.
"""

from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import RemoveMessage

from orchestration.prompts.system_prompt_auto_compact import SUMMARY_SYSTEM_PROMPT
from utils.logging import get_logger
from utils.model import ainvoke_with_retry, init_model
from utils.settings import settings

logger = get_logger(__name__)

MAX_FAILURES_BEFORE_OPEN = 3                        # 熔断阈值：连续失败 3 次
DEFAULT_KEEP_RECENT = 8                             # 尾部保留窗口：最近 N 条消息永不压缩
DEFAULT_MIN_MESSAGES = 6                            # 待压缩消息少于该值时不调用模型（防微压缩）
DEFAULT_SUMMARY_MAX_TOKENS = 512                    # 摘要模型输出上限
CONTENT_PREVIEW_LEN = 400                           # 摘要 prompt 中单条消息内容预览长度

# 代码内容保护：view_file 输出是模型修改代码的依据（不可重建资产）。
# T2 无差别压缩会让模型只凭摘要猜 old_str（str_replace Text not found）
# → 反复重读 → 上下文雪崩（见 8_02_003）。保护单位是“配对组”：
# 1 个 AIMessage + 其全部 tool_calls 的 ToolMessage（API 要求配对完整）。
# 从最近组到最旧组整组保护，累计不超过 MAX_PROTECTED_CONTENT_TOKENS；
# 超限的最旧组允许压缩，防止代码累积后 T2 永远无物可压。
PROTECTED_CONTENT_TOOLS = frozenset({"view_file"})
MAX_PROTECTED_CONTENT_TOKENS = 60_000               # 保护容量上限（与 orchestrator 预算对齐）


@dataclass
class CompactionCheckpoint:
    """Compaction cursor: records the last compaction point and drives incremental compaction.

    Attributes:
        last_compacted_id: Anchor message id of the last compaction window (the
            message stays in history as the starting point for the next
            compaction and is never deleted).
        summary:           Accumulated summary (L1 episodic summary), merged
            incrementally on the next compaction.
        in_progress:       Recursion guard: True while a compaction is running,
            preventing re-entry.
        failure_count:     Consecutive failure count; circuit opens (degrade to
            pure T0/T1) once the threshold is reached.
    """

    last_compacted_id: str | None = None
    summary: str = ""
    in_progress: bool = False
    failure_count: int = 0


@dataclass
class CompactionResult:
    """Result of one compaction action.

    Attributes:
        removals:          Original messages to remove (list of RemoveMessage, handed to state).
        summary:           New summary after compaction (new value on success, old value on failure).
        checkpoint:        Updated checkpoint (caller should write it back to state).
        compressed_count:  Number of messages actually compacted this run.
        tokens_freed:      Estimated tokens freed this run.
    """

    removals: list[RemoveMessage]
    summary: str
    checkpoint: CompactionCheckpoint
    compressed_count: int = 0
    tokens_freed: int = 0


class CircuitBreaker:
    """
    Circuit breaker state machine for consecutive failures, used with ``checkpoint.failure_count``.
    """

    def __init__(self, max_failures: int = MAX_FAILURES_BEFORE_OPEN) -> None:
        self.max_failures = max_failures

    def is_open(self, checkpoint: CompactionCheckpoint) -> bool:
        """
        Whether the circuit is open: consecutive failures reached the threshold.
        """
        return checkpoint.failure_count >= self.max_failures

    def record_failure(self, checkpoint: CompactionCheckpoint) -> bool:
        """
        Record a failure; returns whether the circuit opened.
        """
        checkpoint.failure_count += 1
        return self.is_open(checkpoint)

    def record_success(self, checkpoint: CompactionCheckpoint) -> None:
        """
        Record a success; reset the failure count.
        """
        checkpoint.failure_count = 0


def _build_summary_prompt(existing_summary: str, messages_to_compress: list) -> str:
    """
    Build the incremental summary prompt: existing summary + newly added messages.
    """
    messages_text = []
    for m in messages_to_compress:
        content = str(m.content)
        if len(content) > CONTENT_PREVIEW_LEN:
            content = content[:CONTENT_PREVIEW_LEN] + "..."
        messages_text.append(f"[{m.type}] {content}")
    history = "\n---\n".join(messages_text)

    if existing_summary:
        return (
            f"## Existing Summary\n{existing_summary}\n\n"
            f"## New Messages (extend the summary with these)\n{history}\n\n"
            f"Update the summary to incorporate the new messages. Keep total under 300 words."
        )
    return (
        f"## Conversation History\n{history}\n\n"
        f"Create a summary capturing key information. Keep under 300 words."
    )


def _select_compression_window(
    messages: list,
    checkpoint: CompactionCheckpoint | None,
    keep_recent: int,
) -> tuple[list, str | None]:
    """Select the compression window; returns (middle, anchor_id).

    Rules:
    - Head protection: all SystemMessages + the first HumanMessage are never compacted.
    - Tail protection: the most recent ``keep_recent`` messages are never compacted.
    - Cursor: only messages added after ``checkpoint.last_compacted_id`` are compacted.
    - Pair protection (bidirectional): AI↔Tool pairs are never split; the anchor
      message is kept and so are its paired messages.

    Args:
        messages:    Full message list (usually starts with a SystemMessage).
        checkpoint:  Last compaction cursor; None means first compaction
                     (start right after the head-protected prefix).
        keep_recent: Tail window size; messages inside the window are never
                     compacted.

    Returns:
        ``(middle, anchor_id)``:

        - Non-empty ``middle``: messages to compact (protected messages already
          excluded); ``anchor_id`` is the id of the last message in ``middle`` —
          it stays in history as the next compaction cursor, and its AI↔Tool
          paired messages are kept as well.
        - ``([], None)``: nothing to compact — the window is empty (cursor has
          passed the tail window) or every message in the window is protected
          (pair protection / protected content).
    """
    # ── 头部 / 尾部保留边界 ─────────────────────────────
    protected_idx = {i for i, m in enumerate(messages) if isinstance(m, SystemMessage)}
    first_human = next((i for i, m in enumerate(messages) if m.type == "human"), None)
    if first_human is not None:
        protected_idx.add(first_human)
    head_end = (max(protected_idx) + 1) if protected_idx else 0

    recent_start = max(head_end, len(messages) - keep_recent)

    # ── 游标：从上次压缩锚点的下一条开始 ─────────────────
    start = head_end
    if checkpoint and checkpoint.last_compacted_id:
        cursor = next(
            (i for i, m in enumerate(messages) if m.id == checkpoint.last_compacted_id),
            None,
        )
        if cursor is not None:
            start = cursor + 1
        # 锚点消息不存在（被外部清掉）→ 从 head 重新开始，可接受。

    if start >= recent_start:
        return [], None

    window = messages[start:recent_start]

    # ── AI↔Tool 配对保护（双向）─────────────────────────
    keep_ids = {m.id for m in messages[:head_end]} | {m.id for m in messages[recent_start:]}

    # 尾部窗口以 ToolMessage 开头 → 拉入其配对 AIMessage。
    if recent_start < len(messages) and messages[recent_start].type == "tool":
        for m in reversed(messages[:recent_start]):
            if m.type == "ai" and m.tool_calls:
                keep_ids.add(m.id)
                break

    # keep_ids 中 AIMessage 的 tool_calls 全部配对 ToolMessage 保留。
    keep_tool_call_ids: set[str] = set()
    for m in messages:
        if m.id in keep_ids and m.type == "ai" and m.tool_calls:
            for tc in m.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id:
                    keep_tool_call_ids.add(tc_id)
    if keep_tool_call_ids:
        keep_ids |= {m.id for m in messages if m.type == "tool" and m.tool_call_id in keep_tool_call_ids}

    # ── 代码内容保护（按配对组，从最近到最早整组保护）──────────
    # 并发读 N 个文件 = 1 个 AIMessage + N 个 ToolMessage 的配对组；
    # 压缩掉代码后模型失去修改依据 → str_replace 失败 → 重读循环。
    # 以组为单位保证配对完整性（API 要求 AI 每个 tool_call 都有 ToolMessage）。
    window_tool_msgs = [
        m for m in window
        if m.type == "tool" and (m.name or "") in PROTECTED_CONTENT_TOOLS
        and str(m.content) != "[content cleared]"
    ]
    protected_ids: set[str] = set()
    if window_tool_msgs:
        # 每个 view_file ToolMessage 向前配对到其所属 AIMessage，按 AI 分组。
        groups: dict[str, list] = {}  # ai_id -> [该 AI 的全部 window 内消息]
        ai_by_tc: dict[str, str] = {}  # tool_call_id -> ai_id
        for m in window:
            if m.type == "ai" and m.tool_calls:
                # 注意：append 必须在 tc 循环外——同一 AI 有多个 tool_calls 时
                # 只入组一次，否则组内重复 AI 会让 cost 被夸大（如 14 并发读
                # 组重复 14 次 → 误超 60k 上限 → 保护失效，见 8_02_003 复现测试）。
                groups.setdefault(m.id, []).append(m)
                for tc in m.tool_calls:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if tc_id:
                        ai_by_tc[tc_id] = m.id
        for m in window:
            if m.type == "tool" and m.tool_call_id in ai_by_tc:
                groups.setdefault(ai_by_tc[m.tool_call_id], []).append(m)

        # 从最近到最早，按组累计 token，不超过保护上限。
        # 只保护包含 view_file 输出的组；纯 bash/grep 组不保护。
        view_tc_ids = {m.tool_call_id for m in window_tool_msgs}
        budget_left = MAX_PROTECTED_CONTENT_TOKENS
        for ai_id in reversed(groups):
            group_msgs = groups[ai_id]
            if not any(m.type == "tool" and m.tool_call_id in view_tc_ids
                       for m in group_msgs):
                continue
            cost = sum(count_tokens_approximately([m]) for m in group_msgs)
            if cost > budget_left:
                break
            protected_ids |= {m.id for m in group_msgs}
            budget_left -= cost

    if protected_ids:
        keep_ids |= protected_ids

    middle = [m for m in window if m.id is not None and m.id not in keep_ids]
    if not middle:
        return [], None

    # ── 锚点保护：middle[-1] 保留作游标锚点，且配对消息也保留 ──
    anchor = middle[-1]
    keep_ids.add(anchor.id)
    if anchor.type == "tool":
        # 拉入配对的 AIMessage（若在窗口内），避免孤儿 ToolMessage。
        for m in reversed(middle[:-1]):
            if m.type == "ai" and m.tool_calls:
                keep_ids.add(m.id)
                break
    elif anchor.type == "ai" and anchor.tool_calls:
        # 锚点是带 tool_calls 的 AIMessage → 其 ToolMessage 也保留。
        anchor_tc_ids = {
            tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            for tc in anchor.tool_calls
        }
        keep_ids |= {
            m.id for m in middle if m.type == "tool" and m.tool_call_id in anchor_tc_ids
        }

    middle = [m for m in middle if m.id not in keep_ids]
    return middle, anchor.id


async def incremental_compact(
    messages: list,
    checkpoint: CompactionCheckpoint | None = None,
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    min_messages: int = DEFAULT_MIN_MESSAGES,
    config: dict | None = None,
) -> CompactionResult | None:
    """Run one incremental compaction over the middle messages after the checkpoint.

    Only compacts messages added after the last compaction point, emitting a
    RemoveMessage list and an incremental summary; the anchor message stays in
    history so the next compaction resumes from the correct position.

    Args:
        messages:            Full message list ([0] is usually the SystemMessage).
        checkpoint:          Cursor of the last compaction; None means first compaction.
        keep_recent:         Tail retention window size; messages inside are never compacted.
        summary_max_tokens:  Output cap of the summary model.
        min_messages:        Skip model calls when fewer messages are pending (avoid micro-compaction).
        config:              RunnableConfig, passed through to the summary model's ainvoke.

    Returns:
        ``CompactionResult``:

        - Non-empty ``removals``: compaction succeeded; the caller should hand
          removals to state and write ``result.checkpoint`` back to state.
        - Empty ``removals``: compaction failed (circuit failure count +1);
          the caller still writes ``result.checkpoint`` back to preserve the
          failure count; skip compaction this time.
        - ``None``: nothing to compact (circuit open / recursion guard /
          no new messages in the window).
    """
    if not messages or len(messages) < 3:
        return None

    cp = checkpoint or CompactionCheckpoint()
    breaker = CircuitBreaker()

    # 熔断：连续失败过多，本次跳过（调用方降级纯 T0/T1）。
    if breaker.is_open(cp):
        logger.warning("auto_compact_circuit_open", failure_count=cp.failure_count)
        return None

    # 递归保护：压缩执行期间禁止再次触发。
    if cp.in_progress:
        logger.warning("auto_compact_skip", reason="compaction already in progress")
        return None

    middle, anchor_id = _select_compression_window(messages, cp, keep_recent)
    if not middle:
        return None
    if len(middle) < min_messages:
        return None

    tokens_freed = sum(count_tokens_approximately([m]) for m in middle)
    prompt = _build_summary_prompt(cp.summary, middle)
    summary_model = init_model(
        model_name=settings.deepseek_model_name,
        temperature=0.0,
        max_tokens=summary_max_tokens,
        streaming=False,
    )

    try:
        # 摘要调用身份透传 + 角色标记：与子代理真轮次共用 LLM Start/End 回调日志，
        # 无标记时会被误读为"缺失 context_pipeline 日志"（见 8_02_007 L509）。
        summary_config = (
            {**config, "metadata": {**(config.get("metadata") or {}), "llm_role": "summarizer"}}
            if config
            else None
        )
        response = await ainvoke_with_retry(
            summary_model,
            [SystemMessage(content=SUMMARY_SYSTEM_PROMPT), HumanMessage(content=prompt)],
            config=summary_config,
        )
        new_summary = str(response.content).strip()
    except Exception as e:
        logger.error("auto_compact_summarize_failed", error=str(e)[:300])
        breaker.record_failure(cp)
        return CompactionResult(
            removals=[],
            summary=cp.summary,
            checkpoint=CompactionCheckpoint(
                last_compacted_id=cp.last_compacted_id,
                summary=cp.summary,
                in_progress=False,
                failure_count=cp.failure_count,
            ),
        )

    breaker.record_success(cp)
    logger.info(
        "auto_compact_done",
        compressed_count=len(middle),
        tokens_freed=tokens_freed,
        summary_length=len(new_summary),
        anchor_id=anchor_id,
    )
    return CompactionResult(
        removals=[RemoveMessage(id=m.id) for m in middle],
        summary=new_summary,
        checkpoint=CompactionCheckpoint(
            last_compacted_id=anchor_id,
            summary=new_summary,
            in_progress=False,
            failure_count=0,
        ),
        compressed_count=len(middle),
        tokens_freed=tokens_freed,
    )
