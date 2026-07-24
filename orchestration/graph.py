from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from orchestration.state import OrchestrationState
from orchestration.orchestrator import orchestrator_node, interrupt_node
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS


def _has_tool_calls(state: OrchestrationState) -> bool:
    messages = state.get("messages", [])
    
    if not messages:
        return False
    
    return bool(getattr(messages[-1], "tool_calls", None))


def route_after_orchestrator(state: OrchestrationState) -> str:
    if state["should_orchestration_stop"]:
        return END

    if state["should_orchestration_pause"]:
        return "interrupt"

    if state["response"]:
        return END

    if _has_tool_calls(state):
        return "tools"

    if state["sub_agent_round_tasks"]:
        return "fanout"

    return END


def build_graph():
    builder = StateGraph(OrchestrationState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("tools", ToolNode(list(ORCHESTRATOR_TOOLS)))
    builder.add_node("interrupt", interrupt_node)

    builder.add_edge(START, "orchestrator")
    builder.add_edge("tools", "orchestrator")
    builder.add_edge("interrupt", "orchestrator")

    builder.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "tools": "tools",
            "interrupt": "interrupt",
            "fanout": END,
            END: END,
        },
    )

    return builder.compile()
