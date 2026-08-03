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


def ai(content="", tc_names=(), paths=None):
    tool_calls = []
    for i, n in enumerate(tc_names):
        args = {"file_path": paths[i]} if paths and i < len(paths) else {}
        tool_calls.append({"name": n, "args": args, "id": f"tc-{n}", "type": "tool_call"})
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


def _apply_replacements(messages, replacements):
    """Apply same-id replacements (stale placeholder copies) to a message list."""
    repl_by_id = {r.id: r for r in replacements}
    return [repl_by_id.get(m.id, m) for m in messages]


def _select_and_check(messages, checkpoint=None, keep_recent=6):
    middle, anchor, replacements = _select_compression_window(messages, checkpoint, keep_recent)
    remaining = _apply_replacements(_remaining_after(messages, middle), replacements)
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


class TestViewFilePathDedup:
    """v2 路径去重 + stale 替换（8_03_011）：同一文件多次 view_file 只保护最新视图组。"""

    @staticmethod
    def _select_v2(messages, keep_recent=1):
        """窗口选择 + 应用替换后验配对，返回 (middle, anchor, replacements, remaining)。"""
        middle, anchor, replacements = _select_compression_window(messages, None, keep_recent)
        remaining = _apply_replacements(_remaining_after(messages, middle), replacements)
        assert_pair_integrity(remaining)
        return middle, anchor, replacements, remaining

    def test_same_path_only_latest_group_protected(self):
        # 场景 A：4 组纯 hello.py → 前 3 组解绑进 T2 窗口，最新组保护，无 stale。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",), ("/a/hello.py",)), view_tm("tc-v1"),
            ai("r2", ("v2",), ("/a/hello.py",)), view_tm("tc-v2"),
            ai("r3", ("v3",), ("/a/hello.py",)), view_tm("tc-v3"),
            ai("r4", ("v4",), ("/a/hello.py",)), view_tm("tc-v4"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        m_ids = {m.id for m in middle}
        assert msgs[2].id in m_ids and msgs[3].id in m_ids   # r1/v1 解绑
        assert msgs[8].id not in m_ids and msgs[9].id not in m_ids  # r4/v4 最新保护
        assert replacements == []

    def test_mixed_group_old_path_stale_replaced(self):
        # 场景 B：纯旧组解绑；混合组保护但组内旧 path → stale 替换；最新 path 完整保留。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",), ("/a/hello.py",)), view_tm("tc-v1"),
            ai("r2", ("v2", "v3", "v4"), ("/a/hello.py", "/a/asdf.py", "/a/zxcv.py")),
            view_tm("tc-v2"), view_tm("tc-v3"), view_tm("tc-v4"),
            ai("r3", ("v5", "v6"), ("/a/hello.py", "/a/zzzzz.py")),
            view_tm("tc-v5"), view_tm("tc-v6"),
            ai("r4", ("v7",), ("/a/hello.py",)), view_tm("tc-v7"),
            ai("r5", ("v8",), ("/a/hello.py",)), view_tm("tc-v8"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        m_ids = {m.id for m in middle}
        # 纯旧组 r1 解绑（r4 同为解绑组但作为锚点保留，下一轮从 v7 续压）
        assert msgs[2].id in m_ids and msgs[3].id in m_ids
        # 最新组 r5 保护
        assert msgs[13].id not in m_ids and msgs[14].id not in m_ids
        # stale：v2(hello 旧) 与 v5(hello 旧)；v3/v4/v6/v8 是各文件最新 → 不动
        repl = {r.id: r for r in replacements}
        assert len(replacements) == 2
        assert repl[msgs[5].id].content.startswith("[stale content removed:")
        assert repl[msgs[9].id].content.startswith("[stale content removed:")
        assert "/a/hello.py" in repl[msgs[5].id].content
        assert msgs[6].id not in repl and msgs[7].id not in repl
        assert msgs[10].id not in repl and msgs[14].id not in repl

    def test_interleaved_groups_release_and_stale(self):
        # 场景 C：穿插 —— 纯 hello 组解绑，混合组 stale 掉 hello 旧视图。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1", "v2", "v3"), ("/a/hello.py", "/a/asdf.py", "/a/zxcv.py")),
            view_tm("tc-v1"), view_tm("tc-v2"), view_tm("tc-v3"),
            ai("r2", ("v4",), ("/a/hello.py",)), view_tm("tc-v4"),
            ai("r3", ("v5", "v6"), ("/a/hello.py", "/a/zzzzz.py")),
            view_tm("tc-v5"), view_tm("tc-v6"),
            ai("r4", ("v7",), ("/a/hello.py",)), view_tm("tc-v7"),
            ai("r5", ("v8",), ("/a/hello.py",)), view_tm("tc-v8"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        m_ids = {m.id for m in middle}
        # 纯 hello 旧组 r2 解绑（r4 同为解绑组但作为锚点保留，下一轮从 v7 续压）
        assert msgs[6].id in m_ids and msgs[7].id in m_ids
        # 最新组 r5 保护
        assert msgs[13].id not in m_ids and msgs[14].id not in m_ids
        # stale：r1 组 hello(v1)、r3 组 hello(v5)；asdf/zxcv/zzzzz/最新 hello 不动
        repl = {r.id: r for r in replacements}
        assert len(replacements) == 2
        assert repl[msgs[3].id].content.startswith("[stale content removed:")
        assert repl[msgs[9].id].content.startswith("[stale content removed:")
        assert msgs[4].id not in repl and msgs[5].id not in repl
        assert msgs[10].id not in repl and msgs[14].id not in repl

    def test_tail_newest_view_unprotects_window_groups(self):
        # 最新视图落在尾部保护窗口 → window 内同 path 组全部解绑。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",), ("/a/hello.py",)), view_tm("tc-v1"),
            ai("r2", ("v2",), ("/a/hello.py",)), view_tm("tc-v2"),
            ai("r3", ("v3",), ("/a/hello.py",)), view_tm("tc-v3"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs, keep_recent=2)
        m_ids = {m.id for m in middle}
        # r3 的 v3 在 tail（最新视图）→ r1 解绑、r3 保护
        assert msgs[2].id in m_ids and msgs[3].id in m_ids
        assert msgs[6].id not in m_ids
        assert replacements == []

    def test_cleared_view_not_latest_candidate(self):
        # 最新一次视图 content 已被 T0/T1 清空 → 不算最新视图 → 前一次正常视图生效。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",), ("/a/hello.py",)), view_tm("tc-v1"),
            ai("r2", ("v2",), ("/a/hello.py",)),
            tm(content="[content cleared]", tc="tc-v2", name="view_file"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        m_ids = {m.id for m in middle}
        assert msgs[2].id not in m_ids and msgs[3].id not in m_ids  # r1 组是最新有效视图 → 保护
        assert replacements == []

    def test_stale_placeholder_keeps_id_and_pairing(self):
        # stale 替换保留 id/tool_call_id/name，仅 content 替换；最新视图完整保留。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1", "v2"), ("/a/hello.py", "/a/asdf.py")),
            view_tm("tc-v1"), view_tm("tc-v2"),
            ai("r2", ("v3",), ("/a/hello.py",)), view_tm("tc-v3"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        repl = {r.id: r for r in replacements}
        assert len(replacements) == 1
        stale = repl[msgs[3].id]
        assert stale.tool_call_id == "tc-v1"
        assert stale.name == "view_file"
        assert stale.id == msgs[3].id
        assert str(stale.content).startswith("[stale content removed:")
        # 最新视图组 r2 完整保留且内容未动
        remaining_ids = {m.id for m in remaining}
        assert msgs[5].id in remaining_ids and msgs[6].id in remaining_ids
        assert str(msgs[6].content).startswith("v")

    def test_no_path_info_falls_back_protect_all(self):
        # 兼容回退：file_path 解析不出 → 整组保护（旧行为，绝不误解绑）。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1",)), view_tm("tc-v1"),
            ai("r2", ("v2",)), view_tm("tc-v2"),
            ai("r3", ("v3",)), view_tm("tc-v3"),
            ai("tail"),
        ]
        middle, anchor, replacements, remaining = self._select_v2(msgs)
        assert middle == []
        assert replacements == []


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

    def test_stale_only_result_without_summary_call(self, fake_llm):
        # 窗口全保护但产生 stale 替换 → incremental_compact 产出替换，不调用摘要模型。
        msgs = [
            sysm(), hum(),
            ai("r1", ("v1", "v2"), ("/a/hello.py", "/a/asdf.py")),
            view_tm("tc-v1"), view_tm("tc-v2"),
            ai("r2", ("v3",), ("/a/hello.py",)), view_tm("tc-v3"),
            ai("tail"),
        ]
        result = asyncio.run(incremental_compact(msgs, keep_recent=1, min_messages=1))
        assert result is not None
        assert result.removals == []
        assert len(result.replacements) == 1
        assert result.replacements[0].id == msgs[3].id
        assert str(result.replacements[0].content).startswith("[stale content removed:")

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
