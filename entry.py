"""
临时测试文件，以后会被删除
temporary test file, will be deleted later
"""
import asyncio
import logging

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from orchestration.graph import build_graph
from orchestration.tools._kernel._web import close_crawler
from utils.callbacks import create_orchestration_config
from utils.console import tool_summary
from utils.logging import setup_logging

setup_logging(dev_mode=True, log_level=logging.DEBUG)


# TEST_QUERY = """
# 做一个个人财务管理应用。
# 后端 FastAPI + SQLite + requirements.txt + venv 虚拟环境。
# 前端纯 HTML/CSS/JS（单页应用，无需框架）+ Chart.js（CDN引入，不需要npm install）。
# 功能：
# 1. 分类管理（CRUD，预置 收入/支出 两大类，支出下预置 餐饮/交通/购物/娱乐/居住/医疗/教育/其他）
# 2. 交易记录（CRUD，每笔记录关联分类、金额、日期、备注，支持按月份筛选和分页）
# 3. 月度概览仪表盘（当月总收入/总支出/结余卡片，支出分类饼图，近6个月收支趋势折线图）
# 4. 预算管理（为每个支出分类设置月度预算，超出预算时醒目提醒）
# 你可以先自己思考一下然后写一个 program_architecture.md 出来，里面详细的阐述了前后端怎么设计怎么联调。
# 尽量fanout subagents 一个前端，一个后端，并且告诉前端agent和后端agent 参考你刚刚写的 architecture 文件去做。
# 前后端 agent 写完之后，你去请检查代码并确保其正确性。
# 所有代码输出到 /Users/shenweizhang/Desktop/ai/run_test_040
# """

# TEST_QUERY="""
# 测试任务，严格按以下步骤执行：

# 1. 创建计划，并用 bash 创建目录 /Users/shenweizhang/Desktop/ai/run_test_032

# 2. 并发3个 programmer subagent，分别写三篇文章，都保存到 /Users/shenweizhang/Desktop/ai/run_test_032/：
#    - 我的妈妈.md — 写一篇关于妈妈的文章（800字以上，有真情实感）
#    - 我的爸爸.md — 写一篇关于爸爸的文章（800字以上，有真情实感）
#    - 我的姥爷.md — 写一篇关于姥爷的文章（800字以上，有真情实感）

# 3. 等第2步全部完成后，并发5个 subagent：
#    - 3个 reviewer 分别审查 我的妈妈.md、我的爸爸.md、我的姥爷.md
#    - 2个 programmer 分别写 我的小猫.md、我的女朋友.md（都保存到同一目录，各800字以上）

# 4. 等第3步全部完成后，你自己审查 我的小猫.md 和 我的女朋友.md 的内容质量

# 5. 调用 end_orchestration 结束
# """

# QUERY 1
# TEST_QUERY = """
# 测试任务，严格按以下步骤执行：
# 1. 创建计划，并用 bash 创建目录 /Users/shenweizhang/Desktop/ai/run_test_038
# 2. 并发2个 programmer subagent，在 run_test_038/ 下开发一个简单的记账应用：
#    - backend_impl：Python 后端，实现 add_expense / list_expenses / summary 三个接口，
#      数据存 JSON 文件，代码写入 backend/app.py，并编写 backend/test_app.py 覆盖三个接口，
#      完成后运行测试确认全部通过
#    - frontend_impl：HTML+JS 前端，页面含表单（金额/备注/日期）和列表展示，代码写入 frontend/index.html
# 3. 等第2步全部完成后，并发2个 reviewer subagent，分别用 view_file 完整阅读
#    backend/app.py 和 frontend/index.html，各输出至少3条带具体行号的修改建议
# 4. 等第3步全部完成后，并发2个 programmer subagent 按建议修改两个文件：
#    - 修改前必须先 view_file 读取最新内容，每次 str_replace 后重新 view_file 验证改动生效
#    - 修改全部完成后再次 view_file 复查两个文件确认无遗漏
# 5. 你自己用 view_file 检查 backend/app.py 和 frontend/index.html 的最终状态，
#    确认所有建议已落实
# 6. 调用 end_orchestration 结束
# """

# QUERY 2
# TEST_QUERY = """
# 测试任务，严格按以下步骤执行：
# 1. 创建计划，并用 bash 创建目录 /Users/shenweizhang/Desktop/ai/run_test_036
# 2. 并发2个 programmer subagent，在 run_test_036/ 分别编写：
#    - gen_data.py — 生成 1000 条随机销售记录写入 sales.csv（字段：date/product/amount/region）
#    - report.py — 读取 sales.csv 输出月度汇总报表（控制台打印即可）
# 3. 等第2步全部完成后，1 个 reviewer subagent 用 view_file 完整阅读两个文件，
#    输出至少5条改进建议（含代码质量与健壮性）
# 4. 等第3步完成后，1 个 programmer subagent 用 view_file 读取两个文件后逐一落实建议：
#    - 每完成一个文件的修改，立即 view_file 复查该文件
#    - 全部落实后运行 python gen_data.py && python report.py 验证无报错
# 5. 你自己运行 python report.py 验证输出正确，并 view_file 检查两个文件最终内容
# 6. 调用 end_orchestration 结束
# """

# TEST_QUERY = """
# 你好！
# """

# QUERY SEARCH
TEST_QUERY = """
调研任务：2026 年出境游热门目的地对比调研，最终输出一份中文调研报告。

严格按以下步骤执行：
1. 创建计划，并用 bash 创建目录 /Users/shenweizhang/Desktop/ai/run_test_041

2. 并发 5 个 researcher subagent，分别调研一个目的地（必须用 web_search / fetch_web 搜索最新资料，不要凭记忆）：
   - japan_research：日本（东京/京都），重点：2026 年签证政策、最佳旅行季节、日均预算、亲子/美食/古迹玩法
   - thailand_research：泰国（曼谷/清迈/普吉），重点：2026 年签证政策、最佳旅行季节、日均预算、海岛/夜市玩法
   - singapore_research：新加坡，重点：2026 年签证与入境政策、最佳旅行季节、日均预算、亲子/都市玩法
   - italy_research：意大利（罗马/佛罗伦萨/威尼斯），重点：申根签证难度、最佳旅行季节、日均预算、人文/建筑玩法
   - newzealand_research：新西兰（南岛），重点：签证难度、最佳旅行季节、日均预算、自然/户外玩法
   每个 subagent 调研完成后，把各自调研结论写入 /Users/shenweizhang/Desktop/ai/run_test_041/<目的地名>_research.md（中文，含参考资料链接）

3. 等第 2 步全部完成后，并发 2 个 researcher subagent 做横向对比：
   - comparison_research：对比各目的地的签证难度、机票与住宿成本、日均消费、最佳出行季节、语言障碍、适合人群（亲子/情侣/独自/老年），写入 /Users/shenweizhang/Desktop/ai/run_test_041/comparison.md
   - recommendation_research：针对“第一次出国游 / 亲子游 / 情侣蜜月 / 独自穷游 / 摄影爱好者”五类人群各推荐 1-2 个目的地（含理由与预算区间），写入 /Users/shenweizhang/Desktop/ai/run_test_041/recommendation.md

4. 等第 3 步全部完成后，并发 1 个 reviewer subagent 用 view_file 完整阅读全部 7 个 md 文件，检查内容一致性（预算/签证/季节等数据是否冲突）、结构完整性，输出至少 5 条修改建议写入 /Users/shenweizhang/Desktop/ai/run_test_041/review_notes.md

5. 等第 4 步全部完成后，你自己阅读各调研文件与 review_notes.md，撰写最终报告 /Users/shenweizhang/Desktop/ai/run_test_041/出境游目的地对比调研报告.md（中文，3000 字以上），结构：
   - 引言（调研背景与方法）
   - 各目的地概览与亮点（引用各子调研文件的关键发现）
   - 横向对比表（签证/预算/季节/适合人群）
   - 出行建议（结合 recommendation.md，按人群分类推荐）
   - 附录：参考资料链接列表

6. 用 view_file 检查最终报告存在且内容完整，调用 end_orchestration 结束
"""


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


def _handle_updates(data: dict, header_ref: list):
    for node_name, output in data.items():
        if node_name == "tools":
            header_ref[0] = False
            items = output if isinstance(output, list) else [output]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for msg in item.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        summary = tool_summary(msg)
                        if summary:
                            print(summary, flush=True)
            continue
        if node_name == "__error__":
            print(f"\n[ERROR] {output}", flush=True)
        elif node_name == "interrupt":
            print("\n[INTERRUPT] PAUSED — waiting for human input", flush=True)
        elif isinstance(output, dict) and output.get("error_message"):
            print(f"\n[NODE ERROR] {node_name}: {output['error_message']}", flush=True)


async def main():
    graph = build_graph()
    state = _safe_initial_state(
        user_query=TEST_QUERY,
        conversation_id="demo_001",
        orchestration_id="demo_001",
    )

    print(f"[USER] {state['user_query']}\n")

    header = [False]
    ended_properly = [False]

    async for mode, data in graph.astream(
        state, config=create_orchestration_config(), stream_mode=["updates", "messages"]
    ):
        if mode == "updates":
            _handle_updates(data, header)
            continue

        chunk, _metadata = data
        if not isinstance(chunk, AIMessageChunk):
            continue
        if chunk.tool_call_chunks:
            for tc in chunk.tool_call_chunks:
                if tc.get("name") == "end_orchestration":
                    ended_properly[0] = True
            continue
        content = chunk.content
        if isinstance(content, str) and content:
            if not header[0]:
                print("\n[ORCHESTRATOR] ", end="", flush=True)
                header[0] = True
            print(content, end="", flush=True)
        elif not content and header[0]:
            print(flush=True)
            header[0] = False

    await close_crawler()
    if not ended_properly[0]:
        print("\n\n⚠️  Orchestrator did not call end_orchestration. Graph ended via fallback.")
    print("\n[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
