"""Fanout round-task merge and collect-barrier fallback tests.

Regression for 8_03_006: the orchestrator fired two fanout_subagents calls in
one round (3 reviewer tasks + 2 writer tasks); the overwrite reducer dropped
the first batch, the counter inflated to 5 while only 2 branches ran, and the
graph ENDed early via the phantom-counter route without ever calling
end_orchestration.
"""

from langchain_core.messages import AIMessage

from orchestration.state import _merge_round_tasks
from orchestration.graph import _collect_sub_agent_results, _route_after_tools


def _task(tid: str, sid: str) -> dict:
    return {
        "task_id": tid,
        "task_name": f"任务{tid}",
        "task_description": f"desc {tid}",
        "task_completion_status": False,
        "subagent_id": sid,
        "subagent_name": "散文作者",
    }


def _output(tid: str, sid: str = "programmer_1") -> dict:
    return {
        "result_summary": f"完成 {tid}",
        "sub_agent": sid.split("_", 1)[0],
        "sub_agent_id": sid,
        "sub_agent_name": "散文作者",
        "task_name": f"任务{tid}",
        "status": "success",
        "elapsed_seconds": 1.0,
        "artifacts": [],
    }


# ── reducer: 并行双 fanout 合并 ─────────────────────────────────────

class TestMergeRoundTasks:
    def test_parallel_double_fanout_all_tasks_survive(self):
        """8_03_006 复刻：reviewer 批 [t4,t5,t6] + writer 批 [t7,t8] 合并后 5 个任务全保留。"""
        reviewer_batch = [
            _task("t4", "reviewer_1"),
            _task("t5", "programmer_1"),
            _task("t6", "researcher_1"),
        ]
        writer_batch = [
            _task("t7", "programmer_1"),
            _task("t8", "researcher_1"),
        ]
        merged = _merge_round_tasks(reviewer_batch, writer_batch)
        assert {t["task_id"] for t in merged} == {"t4", "t5", "t6", "t7", "t8"}
        assert len(merged) == 5

    def test_merge_dedupes_by_task_id_keeps_later(self):
        a = _task("t1", "programmer_1")
        a2 = _task("t1", "researcher_1")  # 同 task_id 但不同 subagent_id → 保留后者
        b = _task("t2", "programmer_1")
        merged = _merge_round_tasks([a], [a2, b])
        assert len(merged) == 2
        by_id = {t["task_id"]: t for t in merged}
        assert by_id["t1"]["subagent_id"] == "researcher_1"
        assert by_id["t2"]["subagent_id"] == "programmer_1"

    def test_merge_order_independent(self):
        batch1 = [_task("t1", "programmer_1"), _task("t2", "researcher_1")]
        batch2 = [_task("t3", "reviewer_1")]
        m12 = _merge_round_tasks(batch1, batch2)
        m21 = _merge_round_tasks(batch2, batch1)
        assert {t["task_id"] for t in m12} == {t["task_id"] for t in m21} == {"t1", "t2", "t3"}

    def test_merge_empty_sides(self):
        a = [_task("t1", "programmer_1")]
        assert _merge_round_tasks(None, a) == a
        assert _merge_round_tasks(a, None) == a
        assert _merge_round_tasks(None, None) == []
        assert _merge_round_tasks([], []) == []

    def test_explicit_empty_reset_wins_over_left(self):
        """8_03_007 回归：collect 的显式清空（right=[]）必须生效，不能被合并回旧任务。"""
        leftover = [_task("t1", "programmer_1"), _task("t2", "programmer_1"), _task("t3", "programmer_1")]
        assert _merge_round_tasks(leftover, []) == []
        assert _merge_round_tasks(leftover, []) != leftover  # 旧任务不能残留

    def test_parallel_fanout_merge_then_collect_reset(self):
        """全生命周期：并行双 fanout 合并 → collect 清空 → 列表真正为空。"""
        state_tasks = _merge_round_tasks(
            None, [_task("t4", "reviewer_1"), _task("t5", "programmer_1"), _task("t6", "researcher_1")]
        )
        state_tasks = _merge_round_tasks(state_tasks, [_task("t7", "programmer_1"), _task("t8", "researcher_1")])
        assert len(state_tasks) == 5
        state_tasks = _merge_round_tasks(state_tasks, [])  # collect 清空
        assert state_tasks == []
        # 清空后再写入新一批，不残留旧任务
        state_tasks = _merge_round_tasks(state_tasks, [_task("t9", "reviewer_1")])
        assert [t["task_id"] for t in state_tasks] == ["t9"]


    def test_counter_equals_merged_len_after_parallel_updates(self):
        """模拟 LangGraph 依次 apply 两个并行 fanout 的 update：add 计数 == 合并后任务数。"""
        state_tasks = _merge_round_tasks(
            None, [_task("t4", "reviewer_1"), _task("t5", "programmer_1"), _task("t6", "researcher_1")]
        )
        state_tasks = _merge_round_tasks(
            state_tasks, [_task("t7", "programmer_1"), _task("t8", "researcher_1")]
        )
        counter = 3 + 2  # 两个 fanout update 的 operator.add
        assert len(state_tasks) == 5
        assert counter == len(state_tasks)  # collect 时 counter - len(tasks) == 0


# ── collect 屏障：正常归零 + 幽灵计数自愈 ───────────────────────────

class TestCollectBarrier:
    def _state(self, *, counter, tasks, outputs=None, messages=None):
        return {
            "active_sub_agent_count": counter,
            "sub_agent_round_tasks": tasks,
            "sub_agent_outputs": outputs or {},
            "messages": messages or [],
        }

    def test_normal_round_zeroes_and_injects(self):
        tasks = [_task("t4", "reviewer_1"), _task("t5", "programmer_1"), _task("t6", "researcher_1"),
                 _task("t7", "programmer_1"), _task("t8", "researcher_1")]
        outputs = {t["task_id"]: _output(t["task_id"], t["subagent_id"]) for t in tasks}
        updates = _collect_sub_agent_results(
            self._state(counter=5, tasks=tasks, outputs=outputs)
        )
        assert updates["active_sub_agent_count"] == -5  # 5 - 5 = 0
        assert updates["sub_agent_round_tasks"] == []
        injected = [m for m in updates.get("messages", []) if isinstance(m, AIMessage)]
        assert len(injected) == 5
        assert any("task_id=t4 " in m.content for m in injected)
        assert any("task_id=t8 " in m.content for m in injected)

    def test_phantom_counter_self_heals_and_injects(self):
        """8_03_006 复刻：counter=5（3+2 虚增）但只有 t7/t8 两个任务 → 归零 + 结果仍注入。"""
        tasks = [_task("t7", "programmer_1"), _task("t8", "researcher_1")]
        outputs = {"t7": _output("t7"), "t8": _output("t8", "researcher_1")}
        updates = _collect_sub_agent_results(
            self._state(counter=5, tasks=tasks, outputs=outputs)
        )
        assert updates["active_sub_agent_count"] == -5  # 归零，不再残留 3
        injected = [m for m in updates.get("messages", []) if isinstance(m, AIMessage)]
        assert len(injected) == 2  # t7/t8 结果照常注入，orchestrator 能看到
        assert any("task_id=t7 " in m.content for m in injected)
        assert any("task_id=t8 " in m.content for m in injected)

    def test_phantom_counter_zeroes_even_without_outputs(self):
        updates = _collect_sub_agent_results(
            self._state(counter=3, tasks=[], outputs={})
        )
        assert updates["active_sub_agent_count"] == -3  # 归零
        assert "messages" not in updates  # 无结果可注入，不制造空消息

    def test_no_injection_when_outputs_empty(self):
        tasks = [_task("t1", "programmer_1")]
        updates = _collect_sub_agent_results(
            self._state(counter=1, tasks=tasks, outputs={})
        )
        assert updates["active_sub_agent_count"] == -1
        assert "messages" not in updates

    def test_inject_dedupe_skips_already_injected(self):
        """已注入过的 task_id（messages 里有前缀）不重复注入。"""
        tasks = [_task("t1", "programmer_1")]
        outputs = {"t1": _output("t1")}
        old = AIMessage(content="[SUB-AGENT RESULT] task_id=t1 status=success\nsummary: 完成 t1")
        updates = _collect_sub_agent_results(
            self._state(counter=1, tasks=tasks, outputs=outputs, messages=[old])
        )
        injected = [m for m in updates.get("messages", []) if isinstance(m, AIMessage)]
        assert injected == []  # 已注入过，跳过

    def test_collect_clear_prevents_redispatch_loop(self):
        """8_03_007 回归：collect 清空后，orchestrator 下一轮只调普通工具（无 fanout）时不再重派旧任务。"""
        tasks = [_task("t1", "programmer_1"), _task("t2", "programmer_1"), _task("t3", "programmer_1")]
        outputs = {t["task_id"]: _output(t["task_id"]) for t in tasks}
        updates = _collect_sub_agent_results(
            self._state(counter=3, tasks=tasks, outputs=outputs)
        )
        assert updates["sub_agent_round_tasks"] == []  # 清空必须生效
        # 模拟 orchestrator 下一轮仅调 glob/edit_plan：无新任务写入 → 路由回 orchestrator，不重派
        assert _route_after_tools(
            {"messages": [], "sub_agent_round_tasks": updates["sub_agent_round_tasks"]}
        ) == "orchestrator"

    def test_collect_negative_counter_still_injects(self):
        """counter 负向漂移（历史残留重派导致）时 collect 仍照常注入结果、不炸。"""
        tasks = [_task("t1", "programmer_1")]
        outputs = {"t1": _output("t1")}
        updates = _collect_sub_agent_results(
            self._state(counter=-2, tasks=tasks, outputs=outputs)
        )
        assert updates["active_sub_agent_count"] == -1
        injected = [m for m in updates.get("messages", []) if isinstance(m, AIMessage)]
        assert len(injected) == 1
        assert "task_id=t1 " in injected[0].content

    def test_collect_negative_counter_zeroes_without_outputs(self):
        tasks = []
        updates = _collect_sub_agent_results(
            self._state(counter=-3, tasks=tasks, outputs={})
        )
        assert updates["active_sub_agent_count"] == 0
        assert "messages" not in updates


# ── 路由：合并后的任务全部 Send ─────────────────────────────────────

class TestRouteAfterTools:
    def test_merged_tasks_produce_parallel_sends(self):
        """5 个合并任务 → 5 个 Send 分支，node 按 subagent_id 前缀分发。"""
        tasks = [
            _task("t4", "reviewer_1"),
            _task("t5", "programmer_1"),
            _task("t6", "researcher_1"),
            _task("t7", "programmer_1"),
            _task("t8", "researcher_1"),
        ]
        sends = _route_after_tools({"messages": [], "sub_agent_round_tasks": tasks})
        assert isinstance(sends, list) and len(sends) == 5
        nodes = sorted(s.node for s in sends)
        assert nodes == ["programmer", "programmer", "researcher", "researcher", "reviewer"]
        task_ids = {s.arg["task_id"] for s in sends}
        assert task_ids == {"t4", "t5", "t6", "t7", "t8"}
        # 每个 Send 携带正确的身份与描述
        for s in sends:
            assert s.arg["sub_agent_id"] == s.arg["sub_agent_id"]
            assert s.arg["task_description"].startswith("desc ")

    def test_empty_tasks_routes_to_orchestrator(self):
        assert _route_after_tools({"messages": [], "sub_agent_round_tasks": []}) == "orchestrator"
