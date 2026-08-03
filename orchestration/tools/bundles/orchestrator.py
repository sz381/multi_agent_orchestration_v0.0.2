"""LangChain tool definitions for the orchestrator.

Thin wrappers that bind kernel implementations to ``@tool`` decorators.

├── view_file
├── glob_tool
├── grep_tool
├── str_replace
├── write_file
├── clean_dir
├── bash
├── web_search
├── fetch_web
├── end_orchestration
├── fanout_subagents
├── make_plan
├── edit_plan
└── delete_plan
"""

import json
from typing import Any

from langchain_core.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.prebuilt import ToolRuntime

from orchestration.tools.description.fs_mutate import TOOL_DESCRIPTION as FS_MUTATE_DESCRIPTION
from orchestration.tools.description.fs_readonly import TOOL_DESCRIPTION as FS_READONLY_DESCRIPTION
from orchestration.tools.description.orch_control import TOOL_DESCRIPTION as ORCH_CONTROL_DESCRIPTION
from orchestration.tools.description.plan import TOOL_DESCRIPTION as PLAN_DESCRIPTION
from orchestration.tools.description.web import TOOL_DESCRIPTION as WEB_DESCRIPTION
from orchestration.tools.description.bash import TOOL_DESCRIPTION as BASH_DESCRIPTION
from orchestration.tools._kernel._fs_mutate import (
    str_replace as _str_replace,
    write_file as _write_file,
    clean_dir as _clean_dir,
)
from orchestration.tools._kernel._fs_readonly import (
    view_file as _view_file,
    glob_tool as _glob_tool,
    grep_tool as _grep_tool,
)
from orchestration.tools._kernel._plan import (
    make_plan as _make_plan,
    edit_plan as _edit_plan,
    delete_plan as _delete_plan,
)
from orchestration.tools._kernel._orch_control import (
    end_orchestration as _end_orchestration,
    fanout_subagents as _fanout_subagents,
)
from orchestration.tools._kernel._web import (
    web_search as _web_search,
    fetch_web as _fetch_web,
)
from orchestration.tools._kernel._bash import bash as _bash
from utils.model import count_tokens


@tool("view_file", description=FS_READONLY_DESCRIPTION["view_file"])
def view_file(
    file_path: str,
    offset: int = 1,
    limit: int = 100,
    encoding: str = "utf-8",
    allow_external_reads: bool = False,
) -> str:
    return _view_file(
        file_path,
        offset,
        limit,
        encoding,
        allow_external_reads,
    )


@tool("glob_tool", description=FS_READONLY_DESCRIPTION["glob"])
def glob_tool(
    pattern: str, 
    dir_path: str = ".",
    allow_external_reads: bool = False
) -> str:
    return _glob_tool(
        pattern, 
        dir_path, 
        allow_external_reads
    )


@tool("grep_tool", description=FS_READONLY_DESCRIPTION["grep"])
def grep_tool(
    pattern: str,
    path: str = ".",
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    context_lines: int = 2,
    head_limit: int = 200,
    offset: int = 0,
    case_sensitive: bool = True,
    multiline: bool = False,
    encoding: str = "utf-8",
    allow_external_reads: bool = False,
) -> str:
    return _grep_tool(
        pattern, path,
        glob_pattern,
        output_mode,
        context_lines,
        head_limit,
        offset,
        case_sensitive,
        multiline,
        encoding,
        allow_external_reads
    )


@tool("str_replace", description=FS_MUTATE_DESCRIPTION["str_replace"])
async def str_replace(
    file_path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
    encoding: str = "utf-8",
) -> str:
    return await _str_replace(
        file_path,
        old_str,
        new_str,
        replace_all,
        encoding,
    )


@tool("write_file", description=FS_MUTATE_DESCRIPTION["write_file"])
async def write_file(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
) -> str:
    return await _write_file(
        file_path,
        content,
        encoding,
    )


@tool("clean_dir", description=FS_MUTATE_DESCRIPTION["clean_dir"])
async def clean_dir(
    dir_path: str,
    patterns: list[str] | None = None,
) -> str:
    return await _clean_dir(
        dir_path,
        patterns,
    )


@tool("bash", description=BASH_DESCRIPTION["bash"])
def bash(
    cmd: str,
    cwd: str = ".",
    timeout: int = 30,
    allow_network: bool = True,
) -> str:
    return _bash(
        cmd,
        cwd,
        timeout,
        allow_network,
    )


@tool("web_search", description=WEB_DESCRIPTION["web_search"])
async def web_search(
    query: str,
    max_results: int = 5,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    return await _web_search(
        query,
        max_results,
        allowed_domains,
        blocked_domains,
    )


@tool("fetch_web", description=WEB_DESCRIPTION["fetch_web"])
async def fetch_web(
    url: str,
    prompt: str,
) -> str:
    return await _fetch_web(
        url,
        prompt,
    )

    
@tool("end_orchestration", description=ORCH_CONTROL_DESCRIPTION["end_orchestration"])
async def end_orchestration(
    response: Any,
    runtime: ToolRuntime
) -> Command | str:
    result = await _end_orchestration(
        response,
        runtime.state["response"],
        plan=runtime.state.get("plan"),
    )

    r = json.loads(result)

    if r["status"] == "error":
        return r["message"]

    orch_tokens = count_tokens(runtime.state.get("messages", []))
    sub_agent_outputs = runtime.state.get("sub_agent_outputs", {})
    sub_total = sum(
        output.get("token_used", 0) for output in sub_agent_outputs.values()
    )
    grand_total = orch_tokens["total_tokens"] + sub_total

    print(f"\n  📊 Total tokens: {grand_total}  (orchestrator={orch_tokens['total_tokens']} + sub_agents={sub_total})")
    for task_id, output in sub_agent_outputs.items():
        print(f"     💰 [{output.get('sub_agent_id', task_id)}] {output.get('sub_agent_name', '?')}: {output.get('token_used', 0)} tokens")

    return Command(update={
        "response": response.strip(),
        "messages": [ToolMessage(content=result, tool_call_id=runtime.tool_call_id)],
    })


@tool("fanout_subagents", description=ORCH_CONTROL_DESCRIPTION["fanout_subagents"])
async def fanout_subagents(
    tasks: Any, 
    runtime: ToolRuntime
) -> Command | str:
    result = await _fanout_subagents(tasks, runtime.state["sub_agent_round_tasks"])

    r = json.loads(result)

    if r["status"] == "error":
        return r["message"]

    return Command(update={
        "sub_agent_round_tasks": r["tasks"],
        "active_sub_agent_count": len(r["tasks"]),
        "messages": [ToolMessage(content=result, tool_call_id=runtime.tool_call_id)],
    })


@tool("make_plan", description=PLAN_DESCRIPTION["make_plan"])
def make_plan(phases: list[dict], runtime: ToolRuntime) -> Command | str:
    result = _make_plan(phases, existing_plan=runtime.state.get("plan") or [])
    
    r = json.loads(result)
    
    if r["status"] == "error":
        return r["message"]
    
    return Command(update={
        "plan": r["plan"],
        "messages": [ToolMessage(content=result, tool_call_id=runtime.tool_call_id)],
    })


@tool("edit_plan", description=PLAN_DESCRIPTION["edit_plan"])
def edit_plan(updates: list[dict], runtime: ToolRuntime) -> Command | str:
    result = _edit_plan(updates, runtime.state["plan"] or [])
    
    r = json.loads(result)
    
    if r["status"] == "error":
        return r["message"]
    
    return Command(update={
        "plan": r["plan"],
        "messages": [ToolMessage(content=result, tool_call_id=runtime.tool_call_id)],
    })


@tool("delete_plan", description=PLAN_DESCRIPTION["delete_plan"])
def delete_plan(
    runtime: ToolRuntime,
    phase_id: str = "",
    delete_all: bool = False,
) -> Command | str:
    result = _delete_plan(phase_id, runtime.state["plan"] or [], delete_all)
    
    r = json.loads(result)
    
    if r["status"] == "error":
        return r["message"]
    
    return Command(update={
        "plan": r["plan"],
        "messages": [ToolMessage(content=result, tool_call_id=runtime.tool_call_id)],
    })


"""
All tools available to the orchestrator LLM node.
"""
ORCHESTRATOR_TOOLS = [
    view_file,
    glob_tool,
    grep_tool,
    str_replace,
    write_file,
    clean_dir,
    bash,
    web_search,
    fetch_web,
    end_orchestration,
    fanout_subagents,
    make_plan,
    edit_plan,
    delete_plan,
]
