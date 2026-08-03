"""
ReAct agent factory for sub-agents.

Merges graph builder + agent assembler into one module.

Provides:
    build_react_agent(*, name, tools, system_prompt, state_cls) → CompiledStateGraph
"""

import json
from typing import Literal

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langchain_core.messages import AIMessage

from orchestration.workers.state import SubAgentState
from orchestration.workers.nodes.prepare import make_prepare
from orchestration.workers.nodes.llm import make_llm
from orchestration.workers.nodes.summarize import make_summarize

from utils.logging import get_logger

logger = get_logger(__name__)

# File-producing tools → "path" field in successful results. Artifacts are
# recorded in real time because T2 compaction removes early ToolMessages,
# making them unrecoverable from message history (8_02_005: artifacts_count=0).
_FILE_PRODUCING_TOOLS = {"write_file": "path", "str_replace": "path"}

# Circuit breaker for result parsing failures: consecutive failures reaching
# the threshold indicate a tool contract change (systemic failure); raise to
# stop instead of silently dropping artifacts.
_file_change_parse_failures = 0
_FILE_CHANGE_PARSE_FAILURES_LIMIT = 3


def _collect_file_changes(tool_messages: list) -> list[str]:
    """Extract file paths successfully written/modified in this round of tool results.

    Deduplicated and order-preserving.
    """
    global _file_change_parse_failures
    changes: list[str] = []
    
    for m in tool_messages:
        if m.type != "tool":
            continue

        if getattr(m, "name", "") not in _FILE_PRODUCING_TOOLS:
            continue

        try:
            payload = json.loads(str(m.content))
            _file_change_parse_failures = 0
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _file_change_parse_failures += 1
            # A parse failure silently drops this artifact from file_changes
            # (which feeds summarize's artifacts). Log it; consecutive
            # failures are treated as a contract change → raise to stop.
            logger.warning(
                "write_tool 结果解析失败, ⚠️请注意排查",
                tool_name=getattr(m, "name", "N/A"),
                message_id=getattr(m, "id", "N/A"),
                content_preview=str(m.content)[:200],
                consecutive_failures=_file_change_parse_failures,
            )
            if _file_change_parse_failures >= _FILE_CHANGE_PARSE_FAILURES_LIMIT:
                raise RuntimeError(
                    f"{getattr(m, 'name', 'tool')} 结果连续 "
                    f"{_FILE_CHANGE_PARSE_FAILURES_LIMIT} 次解析失败, 疑似工具契约变更"
                ) from exc
            continue

        if payload.get("status") != "ok":
            continue

        path = str(payload.get("path") or "").strip()
        if path and path not in changes:
            changes.append(path)

    return changes


def _make_tools_node(tools: list):
    """
    Wrap ToolNode: after execution, record written file paths into state.file_changes in real time.
    """
    tool_node = ToolNode(tools, messages_key="sub_agent_messages")

    async def node(state: dict, config=None) -> dict:
        result = await tool_node.ainvoke(state, config=config)
        changes = _collect_file_changes(result.get("sub_agent_messages") or [])
        if changes:
            result["file_changes"] = changes
        return result

    return node


def _build_react_graph(
    *,
    state_cls: type,
    prepare_node,
    llm_node,
    tools: list | None,
    summarize_node,
):
    """Build and compile the ReAct sub-agent graph.

    With tools:    prepare → llm ↔ tools → summarize
    Without tools: prepare → llm → summarize

    The llm node is followed by a conditional edge: tool-calling responses go
    to the tools node, plain responses go to the summarize node.

    Args:
        state_cls:      Sub-agent state schema (TypedDict).
        prepare_node:   Node that injects identity and system prompt.
        llm_node:       Node that invokes the LLM with bound tools.
        tools:          Tool list; None/empty builds a tool-less graph.
        summarize_node: Node that extracts the final response and artifacts.

    Returns:
        Compiled StateGraph ready to run as a sub-graph node.
    """
    builder = StateGraph(state_cls)

    def _route(state) -> Literal["tools", "summarize"]:
        """
        Route the state to either the tools node or the summarize node.
        """
        if not state["sub_agent_messages"]:
            return "summarize"

        last_msg = state["sub_agent_messages"][-1]

        if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
            return "tools"

        return "summarize"

    builder.add_node("prepare", prepare_node)
    builder.add_node("llm", llm_node)
    builder.add_node("summarize", summarize_node)

    has_tools = tools and len(tools) > 0
    if has_tools:
        builder.add_node("tools", _make_tools_node(tools))

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "llm")

    if has_tools:
        builder.add_conditional_edges(
            "llm", 
            _route, 
            {
                "tools": "tools", 
                "summarize": "summarize"
            }
        )
        builder.add_edge("tools", "llm")
    else:
        builder.add_edge("llm", "summarize")

    builder.add_edge("summarize", END)

    return builder.compile()


def build_react_agent(
    *,
    name: str,
    tools: list | None = None,
    system_prompt: str,
    state_cls: type = SubAgentState,
):
    """Build a ReAct agent sub-graph for a sub-agent.

    When tools are non-empty:  prepare → llm ↔ tools → summarize
    When tools are empty:      prepare → llm → summarize

    Args:
        name:          sub-agent type for logging (e.g. "programmer")
        tools:         @tool-decorated functions, or None/[] for no agents    
        system_prompt: sub-agent's system prompt string
        state_cls:     TypedDict state schema (default SubAgentState)

    Returns:
        Compiled StateGraph, ready for use as a sub-graph node in the main graph
        via langgraph Send API.

    Example:
        researcher_graph = build_react_agent(
            name="researcher",
            tools=researcher_tools,
            system_prompt=RESEARCHER_SYSTEM_PROMPT,
        )
    """
    prepare_node = make_prepare(name, system_prompt)
    llm_node = make_llm(tools)
    summarize_node = make_summarize(name)

    return _build_react_graph(
        state_cls=state_cls,
        prepare_node=prepare_node,
        llm_node=llm_node,
        tools=tools,
        summarize_node=summarize_node,
    )
