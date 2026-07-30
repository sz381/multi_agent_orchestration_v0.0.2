"""LangGraph graph definition for the orchestrator.

Builds the main orchestration loop:
    START → orchestrator → tools → fanout (Send) or back
    sub-agents → collect → route_after_collect → orchestrator (only when all done)
    interrupt → orchestrator

Uses LangGraph Send API for dynamic parallel sub-agent dispatch.
Fanout is dispatched immediately after tools execution — the orchestrator
never gets a turn with pending tasks.
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

    Priority: stop → pause → response → tool calls → END.

    Subagent dispatch is handled by route_after_tools, not here.

    Returns:
        str for single-target routing.
    """
    result = END
    if state["should_orchestration_stop"]:
        result = END
    elif state["should_orchestration_pause"]:
        result = "interrupt"
    elif _has_tool_calls(state):
        result = "tools"
    elif state["response"]:
        result = END
    # print(f"[DEBUG route_after_orchestrator] -> {result}  response={state.get('response')!r}  has_tool_calls={_has_tool_calls(state)}", flush=True)
    return result


def route_after_tools(state: OrchestrationState):
    """Dispatch subagents immediately after tools finish, or loop back.

    Priority: fanout (Send API) → orchestrator.

    Dispatching here (not after orchestrator) guarantees that pending
    subagent tasks are never blocked by the orchestrator generating
    new tool calls in the same turn.

    Returns:
        str for single-target routing, list[Send] for fanout to multiple sub-agents.
    """
    tasks = state.get("sub_agent_round_tasks") or []
    # print(f"\n[DEBUG route_after_tools] sub_agent_round_tasks={tasks}  plan={[(p.get('phase_id'), p.get('phase_status')) for p in (state.get('plan') or [])]}  response={state.get('response')!r}", flush=True)
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
                    **(
                        {"project_dir": t["project_dir"]}
                        if t.get("project_dir", "").strip()
                        else {}
                    ),
                },
            )
            for t in tasks
        ]

    return "orchestrator"


def _collect_sub_agent_results(state: OrchestrationState) -> dict:
    """Clear dispatched tasks after sub-agents complete.

    Without this, the route function would re-dispatch the same tasks
    on every subsequent orchestrator turn (infinite loop).

    Also decrements the active sub-agent counter so the routing logic
    knows when all dispatched sub-agents have finished.
    """
    current = state.get("active_sub_agent_count", 0)
    return {
        "sub_agent_round_tasks": [],
        "active_sub_agent_count": max(0, current - 1),
    }


def route_after_collect(state: OrchestrationState) -> str:
    """Only wake the orchestrator when all dispatched sub-agents have finished.

    During fanout, every sub-agent completion triggers collect → this route.
    By checking the active count we avoid waking the orchestrator (and its
    expensive summarize/LLM calls) while sibling sub-agents are still running.
    """
    if state.get("active_sub_agent_count", 0) > 0:
        return END
    return "orchestrator"


def build_graph():
    """Construct and compile the orchestrator StateGraph."""

    builder = StateGraph(OrchestrationState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("tools", ToolNode(list(ORCHESTRATOR_TOOLS)))
    builder.add_node("interrupt", interrupt_node)
    builder.add_node("collect_sub_agent_results", _collect_sub_agent_results)

    SUBAGENT_MAP = {
        "programmer": PROGRAMMER_GRAPH,
        "researcher": RESEARCHER_GRAPH,
        "reviewer": REVIEWER_GRAPH,
    }
    for name, graph in SUBAGENT_MAP.items():
        builder.add_node(name, graph)

    builder.add_edge(START, "orchestrator")
    builder.add_edge("interrupt", "orchestrator")

    for name in SUBAGENT_MAP:
        builder.add_edge(name, "collect_sub_agent_results")
    builder.add_conditional_edges(
        "collect_sub_agent_results",
        route_after_collect,
        {
            "orchestrator": "orchestrator",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "tools": "tools",
            "interrupt": "interrupt",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {
            "orchestrator": "orchestrator",
        },
    )

    return builder.compile()
