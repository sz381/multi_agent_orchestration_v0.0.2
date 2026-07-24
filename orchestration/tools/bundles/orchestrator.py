from langchain_core.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.prebuilt import ToolRuntime

from orchestration.tools.description.fs_mutate import TOOL_DESCRIPTION as FS_MUTATE_DESCRIPTION
from orchestration.tools.description.fs_readonly import TOOL_DESCRIPTION as FS_READONLY_DESCRIPTION
from orchestration.tools.description.control import CONTROL_DESCRIPTIONS
from orchestration.tools.description.plan import TOOL_DESCRIPTIONS as PLAN_DESCRIPTION
from orchestration.tools._kernel._fs_mutate import (
    str_replace as _str_replace,
    write_file as _write_file,
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


@tool("view_file", description=FS_READONLY_DESCRIPTION["view_file"])
def view_file(
    file_path: str, 
    offset: int = 1, 
    limit: int = 100, 
    allow_external_reads: bool = False
) -> str:
    return _view_file(
        file_path, 
        offset, 
        limit, 
        allow_external_reads
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
    replace_all: bool = False
) -> str:
    return await _str_replace(
        file_path, 
        old_str, 
        new_str, 
        replace_all
    )


@tool("write_file", description=FS_MUTATE_DESCRIPTION["write_file"])
async def write_file(
    file_path: str, 
    content: str
) -> str:
    return await _write_file(
        file_path, 
        content
    )
    

@tool("finish", description=CONTROL_DESCRIPTIONS["finish"])
def finish(response: str, runtime: ToolRuntime) -> Command:
    return Command(update={
        "response": response,
        "messages": [ToolMessage(content="Task completed", tool_call_id=runtime.tool_call_id)],
    })


@tool("delegate", description=CONTROL_DESCRIPTIONS["delegate"])
def delegate(tasks: list[dict], runtime: ToolRuntime) -> Command:
    required_task_fields = {
        "task_id", "task_name", "task_description", "task_completion_status", "subagent_id", "subagent_name",
    }
    
    for i, t in enumerate(tasks):
        missing = required_task_fields - t.keys()
        if missing:
            return f"task[{i}] missing required fields: {missing}"
    
    return Command(update={
        "sub_agent_round_tasks": tasks,
        "messages": [ToolMessage(content="Delegated tasks", tool_call_id=runtime.tool_call_id)],
    })


@tool("make_plan", description=PLAN_DESCRIPTION["make_plan"])
def make_plan(phases: list[dict], runtime: ToolRuntime) -> Command:
    required_phase_fields = {
        "phase_id", "phase_name", "phase_status", "phase_description",
    }
    
    for i, p in enumerate(phases):
        missing = required_phase_fields - p.keys()
        if missing:
            return f"phase[{i}] missing required fields: {missing}"
    
    return Command(update={
        "plan": _make_plan(phases),
        "messages": [ToolMessage(content="Plan created", tool_call_id=runtime.tool_call_id)],
    })


@tool("edit_plan", description=PLAN_DESCRIPTION["edit_plan"])
def edit_plan(phase_id: str, update: dict, runtime: ToolRuntime) -> Command:
    new_plan = _edit_plan(phase_id, update, runtime.state["plan"])
    return Command(update={
        "plan": new_plan,
        "messages": [ToolMessage(content="Plan edited", tool_call_id=runtime.tool_call_id)],
    })


@tool("delete_plan", description=PLAN_DESCRIPTION["delete_plan"])
def delete_plan(
    phase_id: str = "",
    delete_all: bool = False,
    runtime: ToolRuntime = None,
) -> Command:
    if delete_all:
        return Command(update={
            "plan": [],
            "messages": [ToolMessage(content="All plans deleted", tool_call_id=runtime.tool_call_id)],
        })
    
    new_plan = _delete_plan(phase_id, runtime.state["plan"])
    return Command(update={
        "plan": new_plan,
        "messages": [ToolMessage(content="Plan deleted", tool_call_id=runtime.tool_call_id)],
    })


ORCHESTRATOR_TOOLS = [
    view_file, 
    glob_tool, 
    grep_tool,
    str_replace, 
    write_file,
    finish, 
    delegate,
    make_plan, 
    edit_plan, 
    delete_plan,
]
