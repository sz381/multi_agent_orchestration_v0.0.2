"""Orchestrator node factory — the central LLM decision-maker in the graph.

exposes orchestrator_node and interrupt_node.

⚠️ 缺少 Trim Message + Summarize Message 逻辑，此逻辑不必现在添加，需要等到 全量注入稳定了之后在做 context engineering
"""

from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langchain_core.runnables import RunnableConfig

from orchestration.state import OrchestrationState
from orchestration.prompts.system_prompt_orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from utils.model import init_model, ainvoke_with_retry
from utils.settings import settings
from utils.logging import get_logger
from utils.summarize import maybe_summarize
from utils.settings import settings

logger = get_logger(__name__)

MAX_ITERATIONS = 50


def _check_iteration_limit(state: OrchestrationState) -> bool:
    """Check if the orchestrator has reached the maximum iteration count.

    Args:
        state: The orchestrator state.

    Returns:
        True if the iteration limit has been reached.
    """
    iteration_cnt = state["orchestration_iteration"] + 1
    hit_limit = iteration_cnt >= MAX_ITERATIONS

    if hit_limit:
        logger.warning(
            "orchestrator_iteration_limit_reached",
            iteration_cnt=iteration_cnt,
        )

    return hit_limit


def _inject_workspace_dir(system_content: str) -> str:
    """Inject the current workspace directory into the system prompt.

    Args:
        system_content: The raw system prompt template.

    Returns:
        System prompt with <CURRENT_WORKSPACE> replaced by the actual path.
    """
    return system_content.replace(
        "<CURRENT_WORKSPACE>",
        f"Your workspace root is: {settings.workspace_dir}",
    )


def _inject_plan(system_content: str, plan: list[dict]) -> str:
    """Inject current plan status into the system prompt.

    Args:
        system_content: The system prompt (after workspace injection).
        plan:            List of phase dicts with phase_id, phase_name, phase_status.

    Returns:
        System prompt with plan status appended. Unchanged if plan is empty.
    """
    if not plan:
        return system_content

    lines = ["\n## CURRENT PLAN"]
    for p in plan:
        icon = {"pending": "○", "in_progress": "◐", "done": "●"}[p["phase_status"]]
        lines.append(f"  {icon} [{p['phase_id']}] {p['phase_name']}")
        
    lines.append("\nBefore ending, verify ALL phases are ●. If any are ○ or ◐, you MUST act on them first.")
    return system_content + "\n".join(lines)


def make_orchestrator_node():
    """Create the orchestrator LLM decision-maker node.
    
    Returns:
        Callable[[OrchestrationState, RunnableConfig], dict] — async LangGraph node
    """
    async def orchestrator_node(state: OrchestrationState, config: RunnableConfig) -> dict:
        logger.debug(
            "orchestrator_node_called",
            iteration=state["orchestration_iteration"],
            counter=state.get("active_sub_agent_count", "N/A"),
        )
        # Inject workspace directory into system prompt
        try:
            system_content = _inject_workspace_dir(ORCHESTRATOR_SYSTEM_PROMPT)
        except Exception as e:
            raise RuntimeError(
                f"Failed to inject workspace: {e.__class__.__name__}: {e}"
            ) from e

        # Inject plan status into system prompt
        try:
            system_content = _inject_plan(system_content, state["plan"])
        except Exception as e:
            raise RuntimeError(
                f"Failed to inject plan: {e.__class__.__name__}: {e}"
            ) from e
        
        # Concatenate system message with existing messages
        messages = [SystemMessage(content=system_content)] + list(state["messages"])

        # Initialize model and bind tools if needed
        model = init_model(
            model_name=settings.xiaomi_mimo_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )
        model = model.bind_tools(ORCHESTRATOR_TOOLS) if not _check_iteration_limit(state) else model
        
        # Invoke the LLM with the system prompt and message history
        try:
            response = await ainvoke_with_retry(model, messages, config=config)
            return {
                "messages": [response], 
                "context_summary": state.get("context_summary", ""), 
                "orchestration_iteration": state["orchestration_iteration"] + 1,
            }
        except Exception as e:
            logger.error("orchestrator_invocation_failed", error=str(e))
            return {
                "messages": [AIMessage(content="An internal error occurred. Please try again.")],
                "error_message": str(e),
            }

    return orchestrator_node


def make_interrupt_node():
    """Create a no-op pause node for human-in-the-loop.

    Returns:
        Callable[[OrchestrationState], dict] — async LangGraph node
    """
    async def interrupt_node(state: OrchestrationState) -> dict:
        pass

    return interrupt_node





















# """Orchestrator node — the central LLM decision-maker in the graph.

# Initialises the model, bind tools, and exposes ``orchestrator_node`` and ``interrupt_node``.
# """

# from langchain_core.messages import SystemMessage, AIMessage
# from langchain_core.messages.utils import trim_messages, count_tokens_approximately
# from langchain_core.runnables import RunnableConfig

# from orchestration.state import OrchestrationState
# from orchestration.prompts.system_prompt_orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
# from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
# from utils.model import init_model, ainvoke_with_retry
# from utils.settings import settings
# from utils.logging import get_logger
# from utils.summarize import maybe_summarize

# logger = get_logger(__name__)

# _model = init_model(
#     model_name="deepseek-v4-flash",
#     temperature=0.3,
#     max_tokens=16384,
#     streaming=True,
# )
# _model_with_tools = _model.bind_tools(ORCHESTRATOR_TOOLS)


# async def orchestrator_node(state: OrchestrationState, config: RunnableConfig) -> dict:
#     """Invoke the LLM with the system prompt, plan status, and message history.

#     The current plan is injected into the system prompt so the orchestrator
#     always knows which phases are still pending — no tool call needed.
#     """

#     try:
#         plan = state.get("plan") or []
#         system_content = ORCHESTRATOR_SYSTEM_PROMPT.replace(
#             "<CURRENT_WORKSPACE>",
#             f"Your workspace root is: {settings.workspace_dir or '.'}",
#         )
#         if plan:
#             plan_lines = ["\n## CURRENT PLAN"]
#             for p in plan:
#                 icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(p.get("phase_status", ""), "?")
#                 plan_lines.append(
#                     f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}"
#                 )
#             plan_lines.append(
#                 "\nBefore ending, verify ALL phases are ●. If any are ○ or ◐, you MUST act on them first."
#             )
#             system_content += "\n".join(plan_lines)

#         system_msg = SystemMessage(content=system_content)
#         history = state["messages"]

#         # Summarize old messages when history grows too long.
#         summary = state.get("context_summary", "")
#         new_summary, history = await maybe_summarize(history, summary, config=config)

#         # Inject conversation summary into system prompt.
#         if new_summary:
#             system_content += f"\n\n## CONVERSATION SUMMARY\n{new_summary}"
#             system_msg = SystemMessage(content=system_content)

#         trimmed_history = trim_messages(
#             history,
#             strategy="last",
#             token_counter=count_tokens_approximately,
#             max_tokens=50000,
#             start_on="human",
#             end_on=("human", "tool"),
#             include_system=False,  # we prepend our own system_msg below
#         )

#         # Ensure the first user message (the task) is never trimmed away.
#         if history and trimmed_history:
#             first_msg = history[0]
#             if first_msg.id not in {m.id for m in trimmed_history}:
#                 trimmed_history = [first_msg] + trimmed_history

#         messages = [system_msg] + trimmed_history

#         response = await ainvoke_with_retry(_model_with_tools, messages, config=config)

#         return {"messages": [response], "context_summary": new_summary}

#     except Exception as e:
#         logger.error(f"Orchestrator invocation failed: {e}", exc_info=True)
#         return {
#             "messages": [AIMessage(content="An internal error occurred. Please try again.")],
#             "error_message": str(e),
#             "context_summary": state.get("context_summary", ""),
#         }


# async def interrupt_node(state: OrchestrationState) -> dict:
#     """
#     No-op node that pauses the graph for human input.
#     """
#     return {}
