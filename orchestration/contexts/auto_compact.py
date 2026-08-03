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

import json
import os
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import RemoveMessage

from orchestration.prompts.system_prompt_auto_compact import SUMMARY_SYSTEM_PROMPT
from utils.logging import get_logger
from utils.model import ainvoke_with_retry, init_model
from utils.settings import settings

logger = get_logger(__name__)

MAX_FAILURES_BEFORE_OPEN = 3                        # 熔断阈值：连续失败 3 次
DEFAULT_KEEP_RECENT = 4                             # 尾部保留窗口：最近 N 条消息永不压缩（8→6→4, 与 pipeline.keep_recent 对齐；8_03_014：轻任务消息少, 大窗口致 T1/T2 无物可压）
DEFAULT_MIN_MESSAGES = 6                            # 待压缩消息少于该值时不调用模型（防微压缩）
DEFAULT_SUMMARY_MAX_TOKENS = 512                    # 摘要模型输出上限
CONTENT_PREVIEW_LEN = 400                           # 摘要 prompt 中单条消息内容预览长度

# 代码内容保护：view_file 输出是模型修改代码的依据（不可重建资产）。
# T2 无差别压缩会让模型只凭摘要猜 old_str（str_replace Text not found）
# → 反复重读 → 上下文雪崩（见 8_02_003）。保护单位是"配对组"：
# 1 个 AIMessage + 其全部 tool_calls 的 ToolMessage（API 要求配对完整）。
# 从最近组到最旧组整组保护，累计不超过 MAX_PROTECTED_CONTENT_TOKENS；
# 超限的最旧组允许压缩，防止代码累积后 T2 永远无物可压。
#
# v2 语义（8_03_011 讨论落地）：同一文件多次 view_file 时，只有"最新一次
# 视图"所在组是模型修改代码的依据——历史视图组若整组不含任何最新视图则
# 解除保护进入 T2 压缩窗口；被保护组内"非最新 path"的 ToolMessage 保留
# 配对结构，content 替换为 STALE 占位符（token 大降且提示模型勿重读）。
PROTECTED_CONTENT_TOOLS = frozenset({"view_file"})
MAX_PROTECTED_CONTENT_TOKENS = 16_000               # 保护容量上限（60K→20K→16K：随SUB_AGENT_BUDGET下调为18K, 保持保护预算<总预算防T2空转, 8_03_002验证）
STALE_PLACEHOLDER_PREFIX = "[stale content removed:"  # 占位符前缀：已替换消息不再参与保护判定
STALE_PLACEHOLDER_TEMPLATE = (
    "[stale content removed: {path} has a newer view_file result later in the "
    "conversation; refer to the most recent view. Do NOT re-read this file.]"
)


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
        replacements:      Same-id ToolMessage copies whose content is replaced by a
                           stale placeholder (protected groups holding non-latest
                           views); LangGraph's add_messages replaces the originals
                           in state when merged together with the removals.
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
    replacements: list = field(default_factory=list)


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


def _tc_id_of(tc) -> str:
    """Extract the id from a tool_call (dict or structured)."""
    return tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")


def _file_path_of(tc) -> str | None:
    """Extract the normalized file path from a tool_call's args.

    Compatible with dict/structured tool_calls and with ``args`` being a dict
    or a JSON string. Returns None when the path cannot be resolved (callers
    fall back to conservative whole-group protection).
    """
    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
    if not args:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    path = args.get("file_path") or args.get("path")
    if not path or not isinstance(path, str):
        return None
    return os.path.normpath(path.strip())


def _enforce_pair_integrity(middle: list, keep_ids: set, messages: list) -> list:
    """Converge the window to full AI⇄Tool pairing (same-birth-same-death).

    Only delete messages whose pairing is complete *inside* ``middle``:

    - An AIMessage whose tool_call has no ToolMessage in ``middle`` (it lives
      in the head/tail protection or was already kept) must be kept, otherwise
      its ToolMessage becomes an orphan.
    - A ToolMessage whose pairing AIMessage is not in ``middle`` must be kept,
      otherwise it becomes an orphan ToolMessage (the 8_03_004 API 400 crash).

    Iterates to a fixed point: keeping one side may orphan the other side, so
    re-scan until nothing changes. Termination is guaranteed because each pass
    either keeps at least one message (shrinking ``middle``) or stops.
    """
    while True:
        changed = False
        ai_by_tc: dict[str, str] = {}
        for m in middle:
            if m.type == "ai" and m.tool_calls:
                for tc in m.tool_calls:
                    tc_id = _tc_id_of(tc)
                    if tc_id:
                        ai_by_tc[tc_id] = m.id
        tool_tc_ids = {m.tool_call_id for m in middle if m.type == "tool"}
        for m in middle:
            if m.type == "ai" and m.tool_calls:
                if any(
                    tc_id and tc_id not in tool_tc_ids
                    for tc in m.tool_calls
                    if (tc_id := _tc_id_of(tc))
                ):
                    keep_ids.add(m.id)
                    changed = True
            elif m.type == "tool" and m.tool_call_id not in ai_by_tc:
                keep_ids.add(m.id)
                changed = True
        if not changed:
            break
        middle = [m for m in middle if m.id not in keep_ids]
        if not middle:
            break
    return middle


def _select_compression_window(
    messages: list,
    checkpoint: CompactionCheckpoint | None,
    keep_recent: int,
) -> tuple[list, str | None, list]:
    """Select the compression window; returns (middle, anchor_id, replacements).

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
        ``(middle, anchor_id, replacements)``:

        - Non-empty ``middle``: messages to compact (protected messages already
          excluded); ``anchor_id`` is the id of the last message in ``middle`` —
          it stays in history as the next compaction cursor, and its AI↔Tool
          paired messages are kept as well.
        - ``replacements``: same-id ToolMessage copies whose content is replaced
          by a stale placeholder (protected groups whose file path is no longer
          the newest view); empty when no stale views exist.
        - ``([], None, [])``: nothing to compact — the window is empty (cursor
          has passed the tail window) or every message in the window is
          protected (pair protection / protected content).
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
        return [], None, []

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

    # ── 代码内容保护 v2（按配对组，从最近到最早整组保护）────────
    # 并发读 N 个文件 = 1 个 AIMessage + N 个 ToolMessage 的配对组；
    # 压缩掉代码后模型失去修改依据 → str_replace 失败 → 重读循环。
    # 以组为单位保证配对完整性（API 要求 AI 每个 tool_call 都有 ToolMessage）。
    #
    # v2 决策（8_03_011）：
    # - path 最新视图映射：同一文件多次 view_file，仅"最新一次视图"所在组是
    #   修改依据；不含任何最新视图的整组 → 解除保护进 T2 压缩窗口（v1）。
    # - stale 替换：被保护组内非最新 path 的 ToolMessage 保留配对结构，
    #   content 替换为 STALE 占位符（token 大降且提示模型勿重读）（v2）。
    # - 兼容回退：file_path 解析不出 → 保守整组保护（绝不误解绑）。
    window_tool_msgs = [
        m for m in window
        if m.type == "tool" and (m.name or "") in PROTECTED_CONTENT_TOOLS
        and str(m.content) != "[content cleared]"
        and not str(m.content).startswith(STALE_PLACEHOLDER_PREFIX)
    ]
    protected_ids: set[str] = set()
    replacements: list[ToolMessage] = []
    if window_tool_msgs:
        # 全量扫描（含 head/tail 保护区）：tool_call_id → ai_id / 规范化 file_path。
        # 必须全量——tail 窗口内的"最新视图"也要能解除 window 内同 path 的保护。
        ai_by_tc: dict[str, str] = {}  # tool_call_id -> ai_id
        tc_path: dict[str, str] = {}  # tool_call_id -> 规范化 file_path
        for m in messages:
            if m.type == "ai" and m.tool_calls:
                for tc in m.tool_calls:
                    tc_id = _tc_id_of(tc)
                    if tc_id:
                        ai_by_tc[tc_id] = m.id
                        path = _file_path_of(tc)
                        if path:
                            tc_path[tc_id] = path

        # window 内按 AI 分组（保护决策只作用于 window；tail 组天然在 keep_ids）。
        # 注意：append 必须在 tc 循环外——同一 AI 有多个 tool_calls 时只入组一次，
        # 否则组内重复 AI 会让 cost 被夸大（如 14 并发读组重复 14 次 → 误超 60k
        # 上限 → 保护失效，见 8_02_003 复现测试）。
        groups: dict[str, list] = {}  # ai_id -> [该 AI 的全部 window 内消息]
        for m in window:
            if m.type == "ai" and m.tool_calls:
                groups.setdefault(m.id, []).append(m)
        for m in window:
            if m.type == "tool" and m.tool_call_id in ai_by_tc:
                groups.setdefault(ai_by_tc[m.tool_call_id], []).append(m)

        # path → 最新视图所在组（全消息范围，后出现者覆盖）。
        # content 已被清空/替换的消息无内容可保护，不算最新视图。
        path_latest_group: dict[str, str] = {}
        for m in messages:
            if m.type == "tool" and (m.name or "") in PROTECTED_CONTENT_TOOLS:
                content = str(m.content)
                if content == "[content cleared]" or content.startswith(STALE_PLACEHOLDER_PREFIX):
                    continue
                p = tc_path.get(m.tool_call_id)
                if p:
                    path_latest_group[p] = ai_by_tc.get(m.tool_call_id, "")

        # 观测日志：窗口内 view path 解析率——全部解析失败说明模型未传
        # file_path，v2 静默回退为整组保护（防止误以为去重在生效）。
        window_view_count = len(window_tool_msgs)
        window_path_known = sum(1 for m in window_tool_msgs if m.tool_call_id in tc_path)
        if window_path_known == 0:
            logger.warning(
                "auto_compact_view_fallback",
                view_count=window_view_count,
                path_known=0,
                reason="all_paths_unresolved",
            )
        elif window_path_known < window_view_count:
            logger.warning(
                "auto_compact_view_fallback",
                view_count=window_view_count,
                path_known=window_path_known,
                reason="partial_paths_unresolved",
            )

        # 从最近到最早，按组累计 token，不超过保护上限。
        # 只保护包含 view_file 输出的组；纯 bash/grep 组不保护。
        view_tc_ids = {m.tool_call_id for m in window_tool_msgs}
        budget_left = MAX_PROTECTED_CONTENT_TOKENS
        for ai_id in reversed(groups):
            group_msgs = groups[ai_id]
            view_tools = [
                m for m in group_msgs
                if m.type == "tool" and m.tool_call_id in view_tc_ids
            ]
            if not view_tools:
                continue
            # v1 解绑判定：组内含任一 path 的最新视图 → 保护；
            # path 全部解析失败 → 保守保护；否则整组解绑进 T2 窗口。
            path_known = [m for m in view_tools if m.tool_call_id in tc_path]
            group_paths = [tc_path[m.tool_call_id] for m in path_known]
            unresolved = len(view_tools) - len(path_known)
            has_latest = True if not path_known else any(
                path_latest_group.get(tc_path[m.tool_call_id]) == ai_id
                for m in path_known
            )
            if not has_latest:
                # 观测日志：整组不含任何最新视图 → 解除保护进 T2 窗口（v1 解绑）。
                logger.info(
                    "auto_compact_view_decision",
                    group_id=ai_id,
                    paths=group_paths,
                    decision="release",
                    reason="no_latest_view",
                    path_unresolved=unresolved,
                )
                continue
            cost = sum(count_tokens_approximately([m]) for m in group_msgs)
            if cost > budget_left:
                logger.info(
                    "auto_compact_view_decision",
                    group_id=ai_id,
                    paths=group_paths,
                    decision="skip",
                    reason="budget_exhausted",
                    path_unresolved=unresolved,
                )
                break
            protected_ids |= {m.id for m in group_msgs}
            budget_left -= cost
            # v2 stale 替换：组已确认保护，组内非最新 path 的 ToolMessage
            # 换为占位符（配对结构不变，id 不变，仅 content 替换）。
            stale_paths: list[str] = []
            for m in path_known:
                p = tc_path[m.tool_call_id]
                if path_latest_group.get(p) != ai_id:
                    replacements.append(
                        m.model_copy(
                            update={"content": STALE_PLACEHOLDER_TEMPLATE.format(path=p)}
                        )
                    )
                    stale_paths.append(p)
            logger.info(
                "auto_compact_view_decision",
                group_id=ai_id,
                paths=group_paths,
                decision="protect",
                stale_paths=stale_paths,
                path_unresolved=unresolved,
            )

    if protected_ids:
        keep_ids |= protected_ids

    middle = [m for m in window if m.id is not None and m.id not in keep_ids]
    if not middle:
        # 窗口全保护但可能已产生 stale 替换 → 单独带出（无损，无需摘要调用）。
        return [], None, replacements

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

    # ── 配对完整性收敛：删除区间两侧 AI⇄Tool 必须同生同灭 ──
    # 覆盖窗口左缘（游标/head 切断配对）、右缘（anchor 拉入 AI 后其
    # 其余 ToolMessage 仍在窗口内）、多 tool_calls 部分配对等场景；
    # 边缘不完整 → 整组保留（收缩窗口），直到不动点（见 8_03_004 崩溃）。
    middle = _enforce_pair_integrity(middle, keep_ids, messages)
    if not middle:
        # 配对收敛后无可压消息 → 同样带出已生成的 stale 替换。
        return [], None, replacements
    return middle, anchor.id, replacements


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

    middle, anchor_id, replacements = _select_compression_window(messages, cp, keep_recent)
    if not middle:
        # 窗口无可压消息，但产生了 stale 替换 → 单独产出替换（无损操作，无需
        # 摘要调用；替换后保护预算释放，下轮可保护更多组）。
        if replacements:
            logger.info(
                "auto_compact_stale_only",
                stale_count=len(replacements),
            )
            return CompactionResult(
                removals=[],
                replacements=replacements,
                summary=cp.summary,
                checkpoint=cp,
                compressed_count=0,
                tokens_freed=0,
            )
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
        stale_count=len(replacements),
    )
    return CompactionResult(
        removals=[RemoveMessage(id=m.id) for m in middle],
        replacements=replacements,
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
