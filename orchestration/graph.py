from langgraph.graph import StateGraph, START, END

from orchestration.state import OrchestrationState
from orchestration.orchestrator import orchestrator_node, interrupt_node


def build_graph():
    builder = StateGraph(OrchestrationState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("interrupt", interrupt_node)

    builder.add_edge(START, "orchestrator")
    builder.add_edge("orchestrator", "interrupt")
    builder.add_edge("interrupt", END)

    return builder.compile()
