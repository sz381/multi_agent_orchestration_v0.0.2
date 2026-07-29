"""LangChain tool definitions for the researcher sub-agent.

Thin wrappers that bind kernel implementations to ``@tool`` decorators.

├── view_file
├── write_file
├── web_search
└── fetch_web
"""

from langchain_core.tools import tool

from orchestration.tools.description.fs_readonly import TOOL_DESCRIPTION as FS_READONLY_DESCRIPTION
from orchestration.tools.description.fs_mutate import TOOL_DESCRIPTION as FS_MUTATE_DESCRIPTION
from orchestration.tools.description.web import TOOL_DESCRIPTION as WEB_DESCRIPTION
from orchestration.tools._kernel._fs_readonly import view_file as _view_file
from orchestration.tools._kernel._fs_mutate import write_file as _write_file
from orchestration.tools._kernel._web import (
    web_search as _web_search,
    fetch_web as _fetch_web,
)


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


"""All tools available to the researcher sub-agent."""
RESEARCHER_TOOLS = [
    view_file,
    write_file,
    web_search,
    fetch_web,
]
