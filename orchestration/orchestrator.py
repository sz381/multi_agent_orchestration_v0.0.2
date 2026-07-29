"""Orchestrator node — the central LLM decision-maker in the graph.

Initialises the model, bind tools, and exposes ``orchestrator_node`` and ``interrupt_node``.
"""

from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.runnables import RunnableConfig

from orchestration.state import OrchestrationState
from orchestration.prompts.system_prompt_orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from utils.model import init_model, ainvoke_with_retry
from utils.logging import get_logger

logger = get_logger(__name__)

_model = init_model(
    model_name="deepseek-v4-flash",
    temperature=0.3,
    max_tokens=16384,
    streaming=True,
)
_model_with_tools = _model.bind_tools(ORCHESTRATOR_TOOLS)


async def orchestrator_node(state: OrchestrationState, config: RunnableConfig) -> dict:
    """Invoke the LLM with the system prompt, plan status, and message history.

    The current plan is injected into the system prompt so the orchestrator
    always knows which phases are still pending — no tool call needed.
    """

    try:
        plan = state.get("plan") or []
        system_content = ORCHESTRATOR_SYSTEM_PROMPT
        if plan:
            plan_lines = ["\n## CURRENT PLAN"]
            for p in plan:
                icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(p.get("phase_status", ""), "?")
                plan_lines.append(
                    f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}"
                )
            plan_lines.append(
                "\nBefore ending, verify ALL phases are ●. If any are ○ or ◐, you MUST act on them first."
            )
            system_content += "\n".join(plan_lines)

        system_msg = SystemMessage(content=system_content)
        history = state["messages"]
        messages = [system_msg] + history

        response = await ainvoke_with_retry(_model_with_tools, messages, config=config)

        return {"messages": [response]}

    except Exception as e:
        logger.error(f"Orchestrator invocation failed: {e}", exc_info=True)
        return {
            "messages": [AIMessage(content="An internal error occurred. Please try again.")],
            "error_message": str(e),
        }


async def interrupt_node(state: OrchestrationState) -> dict:
    """
    No-op node that pauses the graph for human input.
    """
    return {}
