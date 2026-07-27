"""LangGraph graph definition for the orchestrator.

Builds the main orchestration loop:
    START → orchestrator → tools → back
                        → fanout (Send to workers) → workers → orchestrator
                        → interrupt / END

Uses LangGraph Send API for dynamic parallel worker dispatch.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import Send

from orchestration.state import OrchestrationState
from orchestration.orchestrator import orchestrator_node, interrupt_node
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from orchestration.workers import (
    PROGRAMMER_GRAPH,
    RESEARCHER_GRAPH,
    REVIEWER_GRAPH,
)


def _has_tool_calls(state: OrchestrationState) -> bool:
    """Check whether the last message in state contains tool calls."""

    messages = state["messages"]
    if not messages:
        return False
    return bool(getattr(messages[-1], "tool_calls", None))


def route_after_orchestrator(state: OrchestrationState):
    """Decide where the graph should go after the orchestrator runs.

    Priority: stop → pause → response → tool calls → fanout → END.

    Returns:
        str for single-target routing, list[Send] for fanout to multiple workers.
    """
    if state["should_orchestration_stop"]:
        return END

    if state["should_orchestration_pause"]:
        return "interrupt"

    if state["response"]:
        return END

    if _has_tool_calls(state):
        return "tools"

    tasks = state.get("sub_agent_round_tasks") or []
    if tasks:
        return [
            Send(
                node=t["subagent_id"].split("_", 1)[0],
                arg={
                    "sub_agent_id": t["subagent_id"],
                    "sub_agent_name": t["subagent_name"],
                    "task_id": t["task_id"],
                    "task_name": t["task_name"],
                    "task_description": t["task_description"],
                },
            )
            for t in tasks
        ]

    return END


def _collect_worker_results(state: OrchestrationState) -> dict:
    """Clear dispatched tasks after workers complete.

    Without this, the route function would re-dispatch the same tasks
    on every subsequent orchestrator turn (infinite loop).
    """
    return {"sub_agent_round_tasks": []}


def build_graph():
    """Construct and compile the orchestrator StateGraph."""

    builder = StateGraph(OrchestrationState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("tools", ToolNode(list(ORCHESTRATOR_TOOLS)))
    builder.add_node("interrupt", interrupt_node)
    builder.add_node("collect_worker_results", _collect_worker_results)

    SUBAGENT_MAP = {
        "programmer": PROGRAMMER_GRAPH,
        "researcher": RESEARCHER_GRAPH,
        "reviewer": REVIEWER_GRAPH,
    }
    for name, graph in SUBAGENT_MAP.items():
        builder.add_node(name, graph)

    builder.add_edge(START, "orchestrator")
    builder.add_edge("tools", "orchestrator")
    builder.add_edge("interrupt", "orchestrator")

    for name in SUBAGENT_MAP:
        builder.add_edge(name, "collect_worker_results")
    builder.add_edge("collect_worker_results", "orchestrator")

    builder.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "tools": "tools",
            "interrupt": "interrupt",
            END: END,
        },
    )

    return builder.compile()
