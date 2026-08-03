"""
临时测试文件，以后会被删除
temporary test file, will be deleted later
"""
import asyncio
import json
import logging

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from orchestration.graph import build_graph
from orchestration.tools._kernel._web import close_crawler
from utils.callbacks import create_orchestration_config
from utils.logging import setup_logging

setup_logging(dev_mode=True, log_level=logging.DEBUG)


TEST_QUERY="""
做一个个人财务管理应用。
后端 FastAPI + SQLite + requirements.txt + venv 虚拟环境。
前端纯 HTML/CSS/JS（单页应用，无需框架）+ Chart.js（CDN引入，不需要npm install）。
功能：
1. 分类管理（CRUD，预置 收入/支出 两大类，支出下预置 餐饮/交通/购物/娱乐/居住/医疗/教育/其他）
2. 交易记录（CRUD，每笔记录关联分类、金额、日期、备注，支持按月份筛选和分页）
3. 月度概览仪表盘（当月总收入/总支出/结余卡片，支出分类饼图，近6个月收支趋势折线图）
4. 预算管理（为每个支出分类设置月度预算，超出预算时醒目提醒）
你可以先自己思考一下然后写一个 program_architecture.md 出来，里面详细的阐述了前后端怎么设计怎么联调。
尽量fanout subagents 一个前端，一个后端，并且告诉前端agent和后端agent 参考你刚刚写的 architecture 文件去做。
前后端 agent 写完之后，你去请检查代码并确保其正确性。
所有代码输出到 /Users/shenweizhang/Desktop/ai/run_test_026
"""
# TEST_QUERY="""
# 测试任务，严格按以下步骤执行：

# 1. 创建计划，并用 bash 创建目录 /Users/shenweizhang/Desktop/ai/run_test_015

# 2. 并发3个 programmer subagent，分别写三篇文章，都保存到 /Users/shenweizhang/Desktop/ai/run_test_015/：
#    - 我的妈妈.md — 写一篇关于妈妈的文章（800字以上，有真情实感）
#    - 我的爸爸.md — 写一篇关于爸爸的文章（800字以上，有真情实感）
#    - 我的姥爷.md — 写一篇关于姥爷的文章（800字以上，有真情实感）

# 3. 等第2步全部完成后，并发5个 subagent：
#    - 3个 reviewer 分别审查 我的妈妈.md、我的爸爸.md、我的姥爷.md
#    - 2个 programmer 分别写 我的小猫.md、我的女朋友.md（都保存到同一目录，各800字以上）

# 4. 等第3步全部完成后，你自己审查 我的小猫.md 和 我的女朋友.md 的内容质量

# 5. 调用 end_orchestration 结束
# """

def _safe_initial_state(**overrides) -> dict:
    defaults = {
        "messages": [],
        "user_query": "",
        "conversation_id": "default",
        "orchestration_id": "default",
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
    defaults.update(overrides)
    
    if defaults["user_query"] and not defaults["messages"]:
        defaults["messages"] = [HumanMessage(content=defaults["user_query"])]
        
    return defaults


def _fmt_tool_result(msg: ToolMessage) -> str:
    name = msg.name

    if name == "fetch_web":
        try:
            r = json.loads(msg.content)
            if r.get("status") == "error":
                return f"{r['status']}  {r['message']}"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"ok  {len(msg.content)} chars"

    try:
        r = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return msg.content[:100]

    if name == "view_file":
        if r["status"] == "ok":
            return f"ok  {r['path']} ({r['total_lines']} lines)"
        return f"{r['status']}  {r['message']}"

    if name == "glob_tool":
        if r["status"] == "ok":
            return f"ok  {r['message']} ({r['count']} files)"
        return f"{r['status']}  {r['message']}"

    if name == "grep_tool":
        if r["status"] == "ok":
            mode = r.get("output_mode", "files_with_matches")
            total = r.get("total_matches", 0)
            truncated = " (truncated)" if r.get("truncated") else ""
            if mode == "files_with_matches":
                return f"ok  {r.get('total_files', 0)} files matched{truncated} ({total} matches)"
            if mode == "count":
                return f"ok  {r.get('total_files', 0)} files, {r.get('total_occurrences', 0)} occurrences{truncated} ({total} matches)"
            if mode == "content":
                n = len(r.get("results", []))
                return f"ok  {n} lines{truncated} ({total} matches)"
            return f"ok  {total} matches{truncated}"
        return f"{r['status']}  {r['message']}"

    if name in ("str_replace", "write_file"):
        return f"{r['status']}  {r['message']}"

    if name == "make_plan":
        return f"{r.get('status', 'ok')}  {r.get('message', msg.content[:80])}"

    if name == "edit_plan":
        return f"{r.get('status', 'ok')}  {r.get('message', msg.content[:80])}"

    if name == "delete_plan":
        return f"{r.get('status', 'ok')}  {r.get('message', msg.content[:80])}"

    if name in ("end_orchestration", "fanout_subagents"):
        return f"ok  {msg.content[:80]}"

    if name == "web_search":
        if r["status"] == "ok":
            n = r.get("total_results", 0)
            return f"ok  {n} result(s)"
        return f"{r['status']}  {r['message']}"

    if name == "bash":
        if r["status"] == "ok":
            cmd = r.get("command", "")
            exit_code = r.get("exit_code")
            elapsed = r.get("elapsed", 0)
            if exit_code is None:
                exit_str = "TIMEOUT"
            else:
                exit_str = f"exit={exit_code}"
            cmd_display = cmd if len(cmd) <= 120 else cmd[:117] + "..."
            return f"{exit_str}  {elapsed}s  {cmd_display}"
        return f"{r['status']}  {r['message']}"

    return msg.content[:80]


def _fmt_fanout_tasks(tasks: list[dict] | None) -> str:
    if not tasks:
        return ""
    lines = ["\n" + "=" * 60]
    lines.append(f"  FANOUT — {len(tasks)} task(s) dispatched")
    lines.append("=" * 60)
    for t in tasks:
        status_icon = "●" if t.get("task_completion_status") else "○"
        lines.append(f"  {status_icon} [{t.get('task_id', '?')}] {t.get('task_name', '?')}")
        lines.append(f"    agent:  {t.get('subagent_name', '?')} ({t.get('subagent_id', '?')})")
        desc = t.get('task_description', '')
        if desc:
            lines.append(f"    desc:   {desc}")
    lines.append("=" * 60)
    return "\n".join(lines)


def _fmt_plan(plan: list[dict] | None) -> str:
    if not plan:
        return ""
    lines = ["\n" + "=" * 60]
    lines.append(f"  PLAN ({len(plan)} phases)")
    lines.append("=" * 60)
    for p in plan:
        status_icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(p.get("phase_status", ""), "○")
        lines.append(f"  {status_icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}")
        lines.append(f"    status: {p.get('phase_status', '?')}")
        desc = p.get('phase_description', '')
        if desc:
            lines.append(f"    desc: {desc}")
    lines.append("=" * 60)
    return "\n".join(lines)


async def main():
    graph = build_graph()

    state = _safe_initial_state(
        user_query=TEST_QUERY,
        conversation_id="demo_001",
        orchestration_id="demo_001",
    )

    print(f"[USER] {state['user_query']}\n")

    _header_printed = False
    _first_tool_in_batch = False
    _ended_properly = False

    async for mode, data in graph.astream(
        state, config=create_orchestration_config(), stream_mode=["updates", "messages"]
    ):
        if mode == "updates":
            for node_name, output in data.items():
                if node_name == "tools":
                    if not isinstance(output, dict):
                        # Command-based tools (edit_plan, delete_plan, etc.)
                        # can produce state updates as lists (plan) instead of dicts.
                        # Still need to show the result and reset header state.
                        # print(f"  [DEBUG tools output type={type(output).__name__}] {str(output)[:200]}", flush=True)
                        _header_printed = False
                        continue
                    for msg in output.get("messages", []):
                        if isinstance(msg, ToolMessage):
                            # print(f"  TOOL [DONE] {msg.name}  {_fmt_tool_result(msg)}", flush=True)
                            if msg.name in ("make_plan", "edit_plan", "delete_plan"):
                                try:
                                    r = json.loads(msg.content)
                                    if r.get("status") == "ok" and r.get("plan"):
                                        print(_fmt_plan(r["plan"]), flush=True)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                        else:
                            print(f"\n[DEBUG] unexpected msg type: {type(msg).__name__}, content: {str(getattr(msg, 'content', msg))[:200]}", flush=True)
                    # Print fanout tasks if dispatched in this turn
                    tasks = output.get("sub_agent_round_tasks", [])
                    if tasks:
                        print(_fmt_fanout_tasks(tasks), flush=True)
                    _header_printed = False

                elif node_name == "__error__":
                    print(f"\n[ERROR] {output}", flush=True)

                elif node_name == "interrupt":
                    print("\n[INTERRUPT] PAUSED — waiting for human input", flush=True)

                else:
                    if isinstance(output, dict) and output.get("error_message"):
                        print(f"\n[NODE ERROR] {node_name}: {output['error_message']}", flush=True)

        elif mode == "messages":
            msg_chunk, _metadata = data

            if isinstance(msg_chunk, AIMessageChunk):
                if msg_chunk.tool_call_chunks:
                    if not _header_printed:
                        print(f"\n[ORCHESTRATOR] ", end="", flush=True)
                        _header_printed = True
                        _first_tool_in_batch = True
                    for tc in msg_chunk.tool_call_chunks:
                        if name := tc.get("name"):
                            if name == "end_orchestration":
                                _ended_properly = True
                            # if _first_tool_in_batch:
                            #     print(f"\n  TOOL [EXECUTING] {name}", flush=True)
                            #     _first_tool_in_batch = False
                            # else:
                            #     print(f"  TOOL [EXECUTING] {name}", flush=True)
                    continue

                content = msg_chunk.content
                if isinstance(content, str) and content:
                    if not _header_printed:
                        print(f"\n[ORCHESTRATOR] ", end="", flush=True)
                        _header_printed = True
                    print(content, end="", flush=True)

                if not content and not msg_chunk.tool_call_chunks:
                    if _header_printed:
                        print(flush=True)
                    _header_printed = False

    await close_crawler()
    if not _ended_properly:
        print(f"\n\n⚠️  Orchestrator did not call end_orchestration. Graph ended via fallback.")
    print(f"\n[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
