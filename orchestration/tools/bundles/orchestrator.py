from langchain_core.tools import tool

from orchestration.tools.description.fs_mutate import TOOL_DESCRIPTION as FS_MUTATE_DESCRIPTION
from orchestration.tools.description.fs_readonly import TOOL_DESCRIPTION as FS_READONLY_DESCRIPTION
from orchestration.tools._kernel._fs_mutate import (
    str_replace as _str_replace,
    write_file as _write_file,
)
from orchestration.tools._kernel._fs_readonly import (
    view_file as _view_file,
    glob_tool as _glob_tool,
    grep_tool as _grep_tool,
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
    