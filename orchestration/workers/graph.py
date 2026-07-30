"""
ReAct agent factory for sub-agents.

Merges graph builder + agent assembler into one module.

Provides:
    build_react_agent(*, name, tools, system_prompt, state_cls) → CompiledStateGraph

Graph structure:
    has_tools:  START → prepare → llm ↔ tools → summarize → END
    no_tools:   START → prepare → llm → summarize → END
"""

from typing import Literal

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langchain_core.messages import AIMessage

from orchestration.workers.state import SubAgentState
from orchestration.workers.nodes.prepare import make_prepare
from orchestration.workers.nodes.llm import make_llm
from orchestration.workers.nodes.summarize import make_summarize


def _build_react_graph(
    *,
    state_cls: type,
    prepare_node,
    llm_node,
    tools: list | None,
    summarize_node,
):
    """
    Pure graph builder — no business logic, no parameter knowledge.
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

    # add nodes
    builder.add_node("prepare", prepare_node)
    builder.add_node("llm", llm_node)
    builder.add_node("summarize", summarize_node)

    has_tools = tools and len(tools) > 0
    if has_tools:
        builder.add_node("tools", ToolNode(tools, messages_key="sub_agent_messages"))

    # add edges
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "llm")

    if has_tools:
        builder.add_conditional_edges("llm", _route, {"tools": "tools", "summarize": "summarize"})
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
