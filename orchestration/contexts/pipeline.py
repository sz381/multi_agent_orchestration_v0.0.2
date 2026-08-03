"""
Pre-request context pipeline (T0 Snip → T1 Microcompact → budget check → T2 Auto-Compact).

Mount point: before ``ainvoke_with_retry`` in ``orchestrator_node`` / ``llm_node``.
Runs a check every round and acts only when conditions are met (T0/T1 are
zero-cost, T2 is threshold-triggered).

Layer responsibilities:
- T0 Snip: zero-cost head cleanup. ToolMessage content of early control tools
  (plan / fanout / end) is replaced with a placeholder, keeping the AI↔Tool
  pairing structure intact.
- T1 Microcompact: zero-cost decay cleanup. ToolMessages of content-type
  whitelisted tools (bash / view_file / fetch_web / write_file / str_replace /
  grep / glob / web_search) have their content replaced with a placeholder
  when outside the retention window (≈≥3 rounds) or older than 1 hour.
- Budget check: ``threshold = budget - summary_output_budget - buffer_tokens``;
  T2 triggers only when the current estimate exceeds the threshold (13k buffer
  keeps headroom for the compactor's own call).
- T2 Auto-Compact: delegates to ``incremental_compact`` to incrementally
  compact middle messages; ``removals`` are returned to state by the node
  (LangGraph reducer deletes the originals), and the new summary is injected
  into the SystemMessage's ``## CONVERSATION SUMMARY`` section.

Invariants:
- Except for T2's RemoveMessages, this pipeline only modifies the copy sent to
  the LLM, never writing back to state (keeping token statistics and identity
  callbacks intact).
- Any exception fails open: returns the original message copy and never blocks
  the orchestration main flow.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import RemoveMessage

from orchestration.contexts.auto_compact import (
    CompactionCheckpoint,
    CompactionResult,
    incremental_compact,
)
from utils.logging import get_logger

logger = get_logger(__name__)

ORCHESTRATOR_BUDGET = 34_000              # orchestrator 上下文总预算（60K→42K: 激活T2; 42K→34K: threshold 27K→19K, T1更早触发）
ORCHESTRATOR_SUMMARY_OUTPUT_BUDGET = 2_000  # MAX_SUMMARY_OUTPUT（预算预留）
SUB_AGENT_BUDGET = 18_000                 # sub-agent 上下文总预算（40K→27K: 激活T2; 27K→18K: 8_03_013轻任务T1全程0触发, threshold 12.5K→3.5K, T1约每4-5轮触发）
SUB_AGENT_SUMMARY_OUTPUT_BUDGET = 1_500   # MAX_SUMMARY_OUTPUT（预算预留）
BUFFER_TOKENS = 13_000                    # 13k 缓冲：保证压缩器自身调用有空间

CONTENT_CLEARED = "[content cleared]"

# T0：头部控制类工具——结果是一次性动作确认，plan/fanout 状态已实时注入 system。
SNIP_TOOLS = frozenset(
    {"make_plan", "edit_plan", "delete_plan", "end_orchestration", "fanout_subagents"}
)
# T1：内容型工具——大输出原样进入历史是 O(n²) 增长主因之一。
# 注意：view_file 不在其中——文件内容是模型修改代码的依据（不可重建），
# T1 清理会造成"失忆"式重读循环（8_02_003：并发读入的代码被清 → 重读 → 再被清）。
# T1 只清"可重建"输出：bash 结果可重跑、web 结果可重抓、grep/glob 可重搜。
MICROCOMPACT_TOOLS = frozenset(
    {"bash", "fetch_web", "write_file", "str_replace", "grep_tool", "glob_tool", "web_search"}
)

STALE_AGE_SECONDS = 3600  # 超过 1 小时视为过期


@dataclass
class PipelineResult:
    """Result of one pre-request pipeline execution.

    Attributes:
        messages_for_llm:    Processed copy sent to the LLM (after T0/T1/T2).
        checkpoint:          New checkpoint after T2 compaction; unchanged if not triggered.
        removals:            RemoveMessages produced by T2 (the node should merge them into the state update).
        replacements:        Same-id ToolMessage copies with stale placeholders produced by T2
                             (the node should merge them into the state update; LangGraph's
                             add_messages replaces the originals).
        tokens_before:       Estimated tokens after summary injection, before cleanup.
        tokens_after:        Estimated tokens of the processed copy to send.
        threshold:           Budget threshold for this round; None means T2 disabled.
        snip_count:          Number of messages cleaned by T0.
        microcompact_count:  Number of messages cleaned by T1.
        compact_triggered:   Whether T2 was triggered and compaction succeeded.
    """

    messages_for_llm: list
    checkpoint: CompactionCheckpoint | None
    removals: list[RemoveMessage]
    replacements: list = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0
    threshold: int | None = None
    snip_count: int = 0
    microcompact_count: int = 0
    compact_triggered: bool = False


def _replace_content(msg, placeholder: str = CONTENT_CLEARED):
    """Return a copy of the message with content replaced by a placeholder (keeping id and pairing structure).

    Args:
        msg:         Message to process (any BaseMessage).
        placeholder: Placeholder string; defaults to ``CONTENT_CLEARED``.

    Returns:
        The original message when its content is already the placeholder;
        otherwise a ``model_copy`` with ``content`` replaced.
    """
    if str(msg.content) == placeholder:
        return msg
    return msg.model_copy(update={"content": placeholder})


def _is_stale(m, index: int, recent_start: int, now: datetime) -> bool:
    """Whether the ToolMessage is stale: outside the retention window (≥3 rounds) or older than 1 hour.

    Args:
        m:            ToolMessage to inspect.
        index:        Index of the message in the list.
        recent_start: Start index of the retention window.
        now:          Current UTC time for staleness comparison.

    Returns:
        True when ``index < recent_start`` or ``created_at`` is older than
        ``STALE_AGE_SECONDS``; False when ``created_at`` is missing or
        unparsable (fail-open: keeps the content).
    """
    if index < recent_start:
        return True
    created = getattr(m, "created_at", None)
    if not created:
        return False
    try:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created < now - timedelta(seconds=STALE_AGE_SECONDS)
    except (TypeError, ValueError):
        return False


def _apply_snip_and_microcompact(msgs: list, keep_recent: int) -> tuple[int, int]:
    """T0 + T1: replace stale ToolMessage content with placeholders; returns (snip_count, micro_count).

    Only processes ToolMessage copies; control tools inside the retention
    window (most recent ``keep_recent`` messages) are not cleared, and
    content-type tools are cleaned only by time-based decay.

    Args:
        msgs:        Message list copy; stale ToolMessages are replaced in place.
        keep_recent: Retention window size; messages inside are never snipped.

    Returns:
        ``(snip_count, micro_count)``: number of messages cleared by T0
        (control tools) and T1 (content tools) respectively.
    """
    recent_start = max(0, len(msgs) - keep_recent)
    now = datetime.now(timezone.utc)
    snip_count = 0
    micro_count = 0

    for i, m in enumerate(msgs):
        if m.type != "tool":
            continue
        name = m.name or ""
        if name in SNIP_TOOLS:
            if i < recent_start:
                msgs[i] = _replace_content(m)
                snip_count += 1
        elif name in MICROCOMPACT_TOOLS:
            if _is_stale(m, i, recent_start, now):
                msgs[i] = _replace_content(m)
                micro_count += 1

    return snip_count, micro_count


def _inject_summary(msgs: list, summary: str) -> list:
    """Inject the accumulated summary into the SystemMessage's ``## CONVERSATION SUMMARY`` section (idempotent).

    Args:
        msgs:    Message list copy to modify.
        summary: Accumulated summary text; empty string returns ``msgs`` unchanged.

    Returns:
        The message list with the summary appended to the first SystemMessage,
        replacing any previous ``## CONVERSATION SUMMARY`` section; unchanged
        when ``summary`` is empty or no SystemMessage is present.
    """
    if not summary:
        return msgs
    marker = "\n## CONVERSATION SUMMARY"
    for i, m in enumerate(msgs):
        if isinstance(m, SystemMessage):
            content = str(m.content)
            if marker in content:
                content = content[: content.index(marker)]
            content += f"\n\n## CONVERSATION SUMMARY\n{summary}"
            msgs[i] = m.model_copy(update={"content": content})
            break
    return msgs


async def run_pre_request_pipeline(
    messages: list,
    checkpoint: CompactionCheckpoint | None = None,
    *,
    budget: int | None = None,
    summary_output_budget: int = 2_000,
    buffer_tokens: int = BUFFER_TOKENS,
    summary_max_tokens: int = 512,
    # 保留窗口（最近 N 条消息永不清理）。8→4（8_03_014：轻任务总消息仅 9~13 条,
    # 窗口=8 时 recent_start 只有 1~5, 窗口外几乎全是 system/human, T1 无物可清;
    # 窗口=4 后旧 bash/grep/glob 输出更快落出窗口, T1 常态化清理才有对象)。
    keep_recent: int = 4,
    min_messages: int = 6,
    config: dict | None = None,
) -> PipelineResult:
    """Run the context pipeline before an LLM request; returns the message copy ready to send.

    Args:
        messages:               Full message list ([0] is usually the SystemMessage after injection).
        checkpoint:             Last compaction cursor (for T2 incremental compaction).
        budget:                 Total context budget; None skips T2 (T0/T1 only).
        summary_output_budget:  MAX_SUMMARY_OUTPUT reserved budget (summary section placeholder).
        buffer_tokens:          13k buffer, preventing the compactor's own call from exceeding the limit.
        summary_max_tokens:     Summary model output limit (passed through to incremental_compact).
        keep_recent:            Retention window size; messages inside are not cleaned by T0/T1/T2.
        min_messages:           Skip calling the model when there are fewer messages to compact in T2.
        config:                 RunnableConfig, passed through to the summary model.

    Returns:
        ``PipelineResult``: ``messages_for_llm`` is the copy to send; if
        ``removals`` is non-empty, the node should return it to state together
        with the LLM response and write ``checkpoint`` back to state.
    """
    # ── 摘要注入（先于 token 基线统计）────────────────────────────
    # 已有累积摘要 → 每轮注入 SystemMessage（幂等；T2 触发后下方会替换为新摘要）。
    # 必须每轮注入，否则压缩后的消息信息只在触发当轮可见，后续轮次直接丢失。
    # 先注入再统计 tokens_before：freed = before - after 只反映清理净收益；
    # 否则每轮注入的摘要会被算成"负收益"（8_02_005：freed 恒为负）。
    msgs = list(messages)
    if checkpoint and checkpoint.summary:
        msgs = _inject_summary(msgs, checkpoint.summary)

    tokens_before = count_tokens_approximately(msgs)

    # ── 预算检查（前置）：threshold = budget - 摘要预留 - 缓冲 ────
    threshold = None
    if budget is not None:
        threshold = budget - summary_output_budget - buffer_tokens

    # ── T0 / T1：副本级清理（仅预算紧张时执行）────────────────────
    # 平时保留完整工具输出：模型审查/修改代码时需要引用文件内容，
    # 提前清理会造成反复重读（表现为"失忆"式重复劳动，见 8_02_002）。
    snip_count = micro_count = 0
    if threshold is not None and tokens_before > threshold:
        try:
            snip_count, micro_count = _apply_snip_and_microcompact(msgs, keep_recent)
        except Exception as e:
            logger.warning("context_pipeline_snip_failed", error=str(e)[:200])
            snip_count, micro_count = 0, 0

    # ── T2：阈值触发（双重条件）──────────────────────────────────
    #   1) 原始消息总量超 budget 硬限 —— state 逼近模型窗口，强制增量摘要；
    #   2) T1 清不动（副本仍超 threshold）—— 必须摘要。
    comp = None
    compact_triggered = False
    new_checkpoint = checkpoint
    if threshold is not None and (
        tokens_before > budget or count_tokens_approximately(msgs) > threshold
    ):
        try:
            comp: CompactionResult | None = await incremental_compact(
                messages,
                checkpoint,
                keep_recent=keep_recent,
                summary_max_tokens=summary_max_tokens,
                min_messages=min_messages,
                config=config,
            )
        except Exception as e:
            logger.warning("context_pipeline_compact_failed", error=str(e)[:200])
            comp = None

        if comp and comp.removals:
            removed_ids = {r.id for r in comp.removals}
            msgs = [m for m in msgs if m.id not in removed_ids]
            msgs = _inject_summary(msgs, comp.summary)
            new_checkpoint = comp.checkpoint
            compact_triggered = True

        # T2 替换：同 id 的新 ToolMessage（stale 占位符）覆盖副本中的旧消息。
        # 与 removals 互不重叠：被删消息不会再被替换（解绑组 vs 保护组）。
        if comp and comp.replacements:
            repl_by_id = {r.id: r for r in comp.replacements}
            msgs = [repl_by_id.get(m.id, m) for m in msgs]

    tokens_after = count_tokens_approximately(msgs)
    stale_count = len(comp.replacements) if (comp and comp.replacements) else 0

    logger.info(
        "context_pipeline",
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        freed=tokens_before - tokens_after,
        threshold=threshold or 0,
        snip=snip_count,
        microcompact=micro_count,
        auto_compact=compact_triggered,
        stale=stale_count,
    )

    return PipelineResult(
        messages_for_llm=msgs,
        checkpoint=new_checkpoint,
        removals=list(comp.removals) if (comp and comp.removals) else [],
        replacements=list(comp.replacements) if (comp and comp.replacements) else [],
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        threshold=threshold,
        snip_count=snip_count,
        microcompact_count=micro_count,
        compact_triggered=compact_triggered,
    )
