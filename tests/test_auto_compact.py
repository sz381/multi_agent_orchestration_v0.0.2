import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import RemoveMessage

from orchestration.contexts.auto_compact import (
    CompactionCheckpoint,
    _select_compression_window,
    incremental_compact,
)

_SEQ = [0]


def _new_id(prefix: str) -> str:
    _SEQ[0] += 1
    return f"{prefix}{_SEQ[0]:03d}"


def sysm(content="system prompt"):
    return SystemMessage(content=content, id=_new_id("s"))


def hum(content="user query"):
    return HumanMessage(content=content, id=_new_id("h"))


def ai(content="", tc_names=()):
    tool_calls = [
        {"name": n, "args": {}, "id": f"tc-{n}", "type": "tool_call"}
        for n in tc_names
    ]
    return AIMessage(content=content, tool_calls=tool_calls, id=_new_id("a"))


def tm(content="ok", tc="tc-bash", name="bash"):
    return ToolMessage(content=content, tool_call_id=tc, name=name, id=_new_id("t"))


def view_tm(tc, content_len=3000):
    return tm(content="v" * content_len, tc=tc, name="view_file")


def _tc_id(tc):
    return tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")


def assert_pair_integrity(seq):
    pending = []
    for m in seq:
        if m.type == "ai" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                if _tc_id(tc):
                    pending.append(_tc_id(tc))
        elif m.type == "tool":
            assert m.tool_call_id in pending, f"孤儿 ToolMessage: {m.tool_call_id}"
            pending.remove(m.tool_call_id)
    assert not pending, f"孤儿 tool_calls（AIMessage 缺 ToolMessage）: {pending}"


def _remaining_after(messages, middle):
    drop = {m.id for m in middle}
    return [m for m in messages if m.id not in drop]


def _select_and_check(messages, checkpoint=None, keep_recent=6):
    middle, anchor = _select_compression_window(messages, checkpoint, keep_recent)
    remaining = _remaining_after(messages, middle)
    assert_pair_integrity(remaining)
    return middle, anchor, remaining


def _window_msgs(ai_tool_pairs=3, tail=1):
    msgs = [sysm(), hum()]
    for i in range(ai_tool_pairs):
        msgs.append(ai(f"a{i}", (f"t{i}",)))
        msgs.append(tm(tc=f"tc-t{i}"))
    for i in range(tail):
        msgs.append(ai(f"tail{i}"))
    return msgs


class TestBasicWindow:
    def test_head_protected(self):
        msgs = _window_msgs()
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        m_ids = {m.id for m in middle}
        assert msgs[0].id not in m_ids
        assert msgs[1].id not in m_ids

    def test_tail_protected(self):
        msgs = _window_msgs(ai_tool_pairs=3, tail=2)
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        m_ids = {m.id for m in middle}
        assert msgs[-1].id not in m_ids
        assert msgs[-2].id not in m_ids
        assert middle

    def test_first_compaction_compacts_middle(self):
        msgs = _window_msgs(ai_tool_pairs=4, tail=1)
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        assert middle
        assert anchor is not None

    def test_keep_recent_consumes_all_returns_empty(self):
        msgs = [sysm(), hum(), ai("only")]
        middle, anchor, _ = _select_and_check(msgs, keep_recent=2)
        assert middle == []

    def test_plain_dialogue_all_compactable(self):
        msgs = [sysm(), hum()] + [ai(f"msg{i}") for i in range(6)] + [ai("tail")]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        assert len(middle) == 5
        assert anchor == msgs[7].id


class TestCursor:
    def test_resumes_after_checkpoint(self):
        msgs = _window_msgs(ai_tool_pairs=5, tail=1)
        anchor_msg = msgs[4]
        cp = CompactionCheckpoint(last_compacted_id=anchor_msg.id, summary="old")
        middle, anchor, remaining = _select_and_check(msgs, checkpoint=cp, keep_recent=2)
        m_ids = {m.id for m in middle}
        for m in msgs[:5]:
            assert m.id not in m_ids
        assert middle

    def test_cursor_missing_restarts_from_head(self):
        msgs = _window_msgs(ai_tool_pairs=3, tail=1)
        cp = CompactionCheckpoint(last_compacted_id="no-such-id", summary="old")
        middle, anchor, _ = _select_and_check(msgs, checkpoint=cp, keep_recent=2)
        assert middle

    def test_cursor_past_tail_returns_empty(self):
        msgs = _window_msgs(ai_tool_pairs=2, tail=1)
        last = msgs[-1]
        cp = CompactionCheckpoint(last_compacted_id=last.id, summary="old")
        middle, anchor, _ = _select_and_check(msgs, checkpoint=cp, keep_recent=2)
        assert middle == []

    def test_checkpoint_anchor_is_tool_message(self):
        msgs = _window_msgs(ai_tool_pairs=4, tail=1)
        cp = CompactionCheckpoint(last_compacted_id=msgs[5].id, summary="old")
        middle, anchor, remaining = _select_and_check(msgs, checkpoint=cp, keep_recent=1)
        assert_pair_integrity(remaining)
        assert middle


class TestAnchorPairProtection:
    def test_anchor_ai_keeps_its_tool_messages(self):
        msgs = [sysm(), hum(), ai("a1", ("t1",)), tm(tc="tc-t1"), ai("a2", ("t2",)), tm(tc="tc-t2"), ai("tail")]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        remaining_ids = [m.id for m in remaining]
        assert msgs[4].id in remaining_ids
        assert msgs[5].id in remaining_ids

    def test_anchor_tool_keeps_pair_ai(self):
        msgs = [sysm(), hum(), ai("a1", ("t1",)), tm(tc="tc-t1"), ai("a2", ("t2",)), tm(tc="tc-t2"), ai("tail")]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        remaining_ids = [m.id for m in remaining]
        assert msgs[4].id in remaining_ids
        assert msgs[5].id in remaining_ids

    def test_8_03_004_anchor_tool_multi_tc_ai_no_orphan(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("a4", ("t4", "t5", "t6")), tm(tc="tc-t4"), tm(tc="tc-t5"), tm(tc="tc-t6"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        remaining_ids = {m.id for m in remaining}
        assert msgs[8].id in remaining_ids
        assert msgs[9].id in remaining_ids
        assert msgs[10].id in remaining_ids
        assert msgs[11].id in remaining_ids

    def test_middle_ai_with_tool_in_recent_shrinks(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        assert_pair_integrity(remaining)

    def test_multi_tc_partial_tools_shrink(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2", "t3")), tm(tc="tc-t2"), tm(tc="tc-t3"),
            ai("a3", ("t4",)), tm(tc="tc-t4"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        assert_pair_integrity(remaining)

    def test_interleaved_groups_boundary(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("a4", ("t4",)), tm(tc="tc-t4"),
            ai("tail"),
        ]
        for keep in (1, 2, 3):
            middle, anchor, remaining = _select_and_check(msgs, keep_recent=keep)
            assert_pair_integrity(remaining)

    def test_retained_ai_pulls_missing_tools_from_middle(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("a4", ("t4",)), tm(tc="tc-t4"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        assert_pair_integrity(remaining)
        remaining_ids = {m.id for m in remaining}
        assert msgs[-1].id in remaining_ids
        assert msgs[-2].id in remaining_ids

    def test_anchor_and_pair_pull_out_all_middle_empty(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("tail"),
        ]
        middle, anchor, _ = _select_and_check(msgs, keep_recent=1)
        assert middle == []

    def test_window_left_edge_orphan_tool_kept(self):
        msgs = _window_msgs(ai_tool_pairs=5, tail=1)
        cp = CompactionCheckpoint(last_compacted_id=msgs[4].id, summary="old")
        middle, anchor, remaining = _select_and_check(msgs, checkpoint=cp, keep_recent=2)
        remaining_ids = {m.id for m in remaining}
        assert msgs[4].id in remaining_ids
        assert msgs[5].id in remaining_ids
        assert middle

    def test_complete_pair_group_deletable_incomplete_kept(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1", "t2")), tm(tc="tc-t1"), tm(tc="tc-t2"),
            ai("a2", ("t3", "t4")), tm(tc="tc-t3"), tm(tc="tc-t4"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        remaining_ids = {m.id for m in remaining}
        assert msgs[2].id not in remaining_ids
        assert msgs[3].id not in remaining_ids
        assert msgs[4].id not in remaining_ids
        assert msgs[5].id in remaining_ids
        assert msgs[6].id in remaining_ids
        assert msgs[7].id in remaining_ids

    def test_orphan_both_edges_shrink_converges(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2", "t3")), tm(tc="tc-t2"), tm(tc="tc-t3"),
            ai("a3", ("t4",)), tm(tc="tc-t4"),
            ai("tail"),
        ]
        cp = CompactionCheckpoint(last_compacted_id=msgs[4].id, summary="old")
        middle, anchor, remaining = _select_and_check(msgs, checkpoint=cp, keep_recent=1)
        assert_pair_integrity(remaining)
        assert middle == []

    def test_nested_pairs_converge(self):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("a4", ("t4",)), tm(tc="tc-t4"),
            ai("a5", ("t5",)), tm(tc="tc-t5"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=3)
        assert_pair_integrity(remaining)

    def test_multiround_compaction_pairs_stay_intact(self):
        msgs = _window_msgs(ai_tool_pairs=3, tail=1)
        cp = None
        for r in range(1, 5):
            middle, anchor, remaining = _select_and_check(msgs, checkpoint=cp, keep_recent=2)
            assert middle, f"round {r}: window must not be empty"
            cp = CompactionCheckpoint(last_compacted_id=anchor, summary=f"r{r}")
            msgs = remaining + [
                ai(f"g{r}a", (f"ga{r}",)), tm(tc=f"tc-ga{r}"),
                ai(f"g{r}b", (f"gb{r}",)), tm(tc=f"tc-gb{r}"),
                ai(f"g{r}c", (f"gc{r}",)), tm(tc=f"tc-gc{r}"),
            ]
            assert_pair_integrity(msgs)


class TestViewFileGroupProtection:
    def test_group_under_budget_protected(self):
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1", "v2")), view_tm("tc-v1"), view_tm("tc-v2"),
            ai("r2", ("v3",)), view_tm("tc-v3"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        m_ids = {m.id for m in middle}
        for m in msgs[2:8]:
            assert m.id not in m_ids

    def test_pure_bash_group_not_protected(self):
        msgs = [
            sysm(), hum(),
            ai("b1", ("b1",)), tm(content="x" * 3000, tc="tc-b1"),
            ai("b2", ("b2",)), tm(content="x" * 3000, tc="tc-b2"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        m_ids = {m.id for m in middle}
        assert msgs[2].id in m_ids

    def test_mixed_group_with_view_protected(self):
        msgs = [
            sysm(), hum(),
            ai("m1", ("v1", "b1")), view_tm("tc-v1"), tm(content="x" * 3000, tc="tc-b1"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        m_ids = {m.id for m in middle}
        assert msgs[2].id not in m_ids
        assert msgs[3].id not in m_ids

    def test_over_budget_oldest_group_compactable(self):
        big = 6000
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",)), view_tm("tc-v1", content_len=big),
            ai("r2", ("v2",)), view_tm("tc-v2", content_len=big),
            ai("r3", ("v3",)), view_tm("tc-v3", content_len=big),
            ai("r4", ("v4",)), view_tm("tc-v4", content_len=big),
            ai("r5", ("v5",)), view_tm("tc-v5", content_len=big),
            ai("r6", ("v6",)), view_tm("tc-v6", content_len=big),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        m_ids = {m.id for m in middle}
        protected_newest = [msgs[10], msgs[11], msgs[12], msgs[13]]
        for m in protected_newest:
            assert m.id not in m_ids
        assert_pair_integrity(remaining)

    def test_concurrent_14_views_cost_not_inflated(self):
        tcs = [f"tc-v{i}" for i in range(14)]
        msgs = [
            sysm(), hum(),
            ai("r1", tuple(f"v{i}" for i in range(14))),
        ] + [view_tm(tc) for tc in tcs] + [ai("tail")]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=1)
        m_ids = {m.id for m in middle}
        assert msgs[2].id not in m_ids
        for tm_ in msgs[3:17]:
            assert tm_.id not in m_ids

    def test_view_protected_then_pair_convergence(self):
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1", "v2")), view_tm("tc-v1"), view_tm("tc-v2"),
            ai("r2", ("v3",)), view_tm("tc-v3"),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("tail"),
        ]
        middle, anchor, remaining = _select_and_check(msgs, keep_recent=2)
        remaining_ids = {m.id for m in remaining}
        for m in msgs[2:7]:
            assert m.id in remaining_ids
        assert len(middle) == 2
        assert_pair_integrity(remaining)


class TestIncrementalCompact:
    @pytest.fixture
    def fake_llm(self, monkeypatch):
        class FakeResp:
            content = "incremental summary text"

        async def fake_ainvoke(model, messages, config=None):
            return FakeResp()

        monkeypatch.setattr(
            "orchestration.contexts.auto_compact.ainvoke_with_retry", fake_ainvoke
        )
        monkeypatch.setattr(
            "orchestration.contexts.auto_compact.init_model", lambda **kw: object()
        )

    def test_basic_success(self, fake_llm):
        msgs = _window_msgs(ai_tool_pairs=6, tail=1)
        result = asyncio.run(incremental_compact(msgs, keep_recent=3, min_messages=3))
        assert result is not None
        assert result.removals
        assert all(isinstance(r, RemoveMessage) for r in result.removals)
        assert result.summary == "incremental summary text"
        assert result.checkpoint.last_compacted_id is not None
        assert result.tokens_freed > 0
        remaining = _remaining_after(msgs, result.removals)
        assert_pair_integrity(remaining)

    def test_too_few_messages_none(self):
        msgs = [sysm(), hum()]
        assert asyncio.run(incremental_compact(msgs)) is None

    def test_min_messages_skip(self, fake_llm):
        msgs = [sysm(), hum(), ai("a1"), ai("a2"), ai("a3")]
        result = asyncio.run(incremental_compact(msgs, keep_recent=0, min_messages=6))
        assert result is None

    def test_circuit_open_skip(self, fake_llm):
        msgs = _window_msgs(ai_tool_pairs=4, tail=1)
        cp = CompactionCheckpoint(failure_count=3)
        assert asyncio.run(incremental_compact(msgs, checkpoint=cp)) is None

    def test_in_progress_skip(self, fake_llm):
        msgs = _window_msgs(ai_tool_pairs=4, tail=1)
        cp = CompactionCheckpoint(in_progress=True)
        assert asyncio.run(incremental_compact(msgs, checkpoint=cp)) is None

    def test_summarize_failure_returns_empty_removals(self, monkeypatch):
        async def fake_ainvoke(model, messages, config=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "orchestration.contexts.auto_compact.ainvoke_with_retry", fake_ainvoke
        )
        monkeypatch.setattr(
            "orchestration.contexts.auto_compact.init_model", lambda **kw: object()
        )
        msgs = _window_msgs(ai_tool_pairs=4, tail=1)
        result = asyncio.run(incremental_compact(msgs, keep_recent=1, min_messages=2))
        assert result is not None
        assert result.removals == []
        assert result.checkpoint.failure_count == 1

    def test_end_to_end_8_03_004_scenario_no_orphans(self, fake_llm):
        msgs = [
            sysm(), hum(),
            ai("a1", ("t1",)), tm(tc="tc-t1"),
            ai("a2", ("t2",)), tm(tc="tc-t2"),
            ai("a3", ("t3",)), tm(tc="tc-t3"),
            ai("a4", ("t4", "t5", "t6")), tm(tc="tc-t4"), tm(tc="tc-t5"), tm(tc="tc-t6"),
            ai("a5", ("t7",)), tm(tc="tc-t7"),
            ai("tail"),
        ]
        result = asyncio.run(incremental_compact(msgs, keep_recent=3, min_messages=3))
        assert result is not None
        remaining = _remaining_after(msgs, result.removals)
        assert_pair_integrity(remaining)
        assert len(result.removals) > 0
