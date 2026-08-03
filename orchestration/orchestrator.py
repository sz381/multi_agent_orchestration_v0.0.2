"""Orchestrator node factory — the central LLM decision-maker in the graph.

exposes orchestrator_node and interrupt_node.
"""

from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.runnables import RunnableConfig

from orchestration.state import OrchestrationState
from orchestration.prompts.system_prompt_orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from orchestration.contexts.pipeline import (
    run_pre_request_pipeline,
    ORCHESTRATOR_BUDGET,
    ORCHESTRATOR_SUMMARY_OUTPUT_BUDGET,
)
from utils.model import init_model, ainvoke_with_content_guard
from utils.settings import settings
from utils.logging import get_logger

logger = get_logger(__name__)

MAX_ITERATIONS = 45
ITERATION_BUDGET = 41


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


def _inject_iteration_budget(system_content: str, iteration: int) -> str:
    """Inject the remaining iteration budget into the system prompt.

    Args:
        system_content: The system prompt (after plan injection).
        iteration:      Iterations already consumed (0-based count).

    Returns:
        System prompt with a live budget line appended, so the model can
        sense scarcity and close out before hitting the hard cap.
    """
    remaining = max(0, ITERATION_BUDGET - iteration)
    if remaining <= 0:
        return system_content + (
            f"\n\n## LIVE BUDGET\nYou are PAST your ~{ITERATION_BUDGET}-iteration budget. "
            "Do NOT start new work — wrap up with end_orchestration immediately."
        )
    return system_content + (
        f"\n\n## LIVE BUDGET\nIterations consumed: {iteration} / ~{ITERATION_BUDGET}. "
        f"Remaining: ~{remaining}. Verify with tests, then close out."
    )


def make_orchestrator_node():
    """Create the orchestrator LLM decision-maker node.
    
    Returns:
        Callable[[OrchestrationState, RunnableConfig], dict] — async LangGraph node
    """
    async def orchestrator_node(state: OrchestrationState, config: RunnableConfig) -> dict:
        # Log the call to the orchestrator node
        logger.info(
            "orchestrator_node_called",
            agent_name="orchestrator",
            agent_id="orchestrator",
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

        # Inject live iteration budget into system prompt
        try:
            system_content = _inject_iteration_budget(
                system_content, state["orchestration_iteration"]
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to inject iteration budget: {e.__class__.__name__}: {e}"
            ) from e
        
        # Concatenate system message with existing messages
        messages = [SystemMessage(content=system_content)] + list(state["messages"])

        # Request pre-context pipeline: T0 Snip → T1 Microcompact → Budget check → T2 Auto-Compact.
        # Fail-open: Any exception is downgraded to the original message and does not disrupt the main orchestration process.
        try:
            pipeline_result = await run_pre_request_pipeline(
                messages,
                state.get("compaction_checkpoint"),
                budget=ORCHESTRATOR_BUDGET,
                summary_output_budget=ORCHESTRATOR_SUMMARY_OUTPUT_BUDGET,
                config=config,
            )
            messages = pipeline_result.messages_for_llm
        except Exception as e:
            logger.warning("context_pipeline_orchestrator_failed", error=str(e)[:200])
            pipeline_result = None

        # Initialize model and bind tools if needed
        model = init_model(
            model_name=settings.deepseek_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )
        model = model.bind_tools(ORCHESTRATOR_TOOLS) if not _check_iteration_limit(state) else model
        
        # Invoke the LLM with the system prompt and message history
        try:
            response = await ainvoke_with_content_guard(
                model, messages, config=config, role="orchestrator"
            )
            removals = pipeline_result.removals if pipeline_result else []
            replacements = pipeline_result.replacements if pipeline_result else []
            checkpoint = pipeline_result.checkpoint if pipeline_result else state.get("compaction_checkpoint")
            if removals or replacements:
                # 观测日志：确认 T2 的删除/替换真实写回 state（add_messages 生效）。
                logger.info(
                    "orchestrator_state_writeback",
                    removals=len(removals),
                    replacements=len(replacements),
                )
            return {
                "messages": [response] + removals + replacements,
                "compaction_checkpoint": checkpoint,
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

    ⚠️ this is currently a placeholder, TO BE IMPLEMENTED

    Returns:
        Callable[[OrchestrationState], dict] — async LangGraph node
    """
    async def interrupt_node(state: OrchestrationState) -> dict:
        pass

    return interrupt_node
    