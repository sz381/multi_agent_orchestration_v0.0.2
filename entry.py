import asyncio
import json

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from orchestration.graph import build_graph


def _safe_initial_state(**overrides) -> dict:
    defaults = {
        "messages": [],
        "user_query": "",
        "conversation_id": "default",
        "orchestration_id": "default",
        "plan": [],
        "sub_agent_round_tasks": [],
        "sub_agent_outputs": {},
        "orchestration_status": "",
        "should_orchestration_pause": False,
        "should_orchestration_stop": False,
        "response": "",
        "output_artifacts": [],
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
    try:
        r = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return msg.content[:100]

    name = msg.name

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
                return f"ok  {r['total_files']} files matched{truncated} ({total} matches)"
            if mode == "count":
                return f"ok  {r['total_files']} files, {r['total_occurrences']} occurrences{truncated} ({total} matches)"
            if mode == "content":
                n = len(r.get("results", []))
                return f"ok  {n} lines{truncated} ({total} matches)"
            return f"ok  {total} matches{truncated}"
        return f"{r['status']}  {r['message']}"

    if name in ("str_replace", "write_file"):
        return f"{r['status']}  {r['message']}"

    if name in ("finish", "delegate", "make_plan", "edit_plan", "delete_plan"):
        return f"ok  {msg.content[:80]}"

    raise ValueError(f"Unknown tool: {name}")


async def main():
    graph = build_graph()

    state = _safe_initial_state(
        user_query="测试view file 性能，用最最最严格的方式测试，你认为都全面了为止。",
        conversation_id="demo_001",
        orchestration_id="demo_001",
    )

    print(f"[USER] {state['user_query']}\n")

    _header_printed = False

    async for mode, data in graph.astream(
        state, stream_mode=["updates", "messages"]
    ):
        if mode == "updates":
            for node_name, output in data.items():
                if node_name == "tools":
                    for msg in output.get("messages", []):
                        if isinstance(msg, ToolMessage):
                            print(f"  TOOL [DONE] {msg.name}  {_fmt_tool_result(msg)}", flush=True)
                    _header_printed = False

                elif node_name == "interrupt":
                    print("\n[INTERRUPT] PAUSED — waiting for human input", flush=True)

        elif mode == "messages":
            msg_chunk, _metadata = data

            if isinstance(msg_chunk, AIMessageChunk):
                if msg_chunk.tool_call_chunks:
                    if not _header_printed:
                        print(f"\n[ORCHESTRATOR]", flush=True)
                        _header_printed = True
                    for tc in msg_chunk.tool_call_chunks:
                        if name := tc.get("name"):
                            print(f"  TOOL [EXECUTING] {name}", flush=True)
                    continue

                content = msg_chunk.content
                if isinstance(content, str) and content:
                    if not _header_printed:
                        print(f"\n[ORCHESTRATOR]", end=" ", flush=True)
                        _header_printed = True
                    print(content, end="", flush=True)

                if not content and not msg_chunk.tool_call_chunks:
                    _header_printed = False

    print(f"\n\n[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
