"""
LLM node factory — model invocation with optional tool binding.
"""

from langchain_core.runnables import RunnableConfig
from langchain_core.messages import SystemMessage

from utils.model import init_model, ainvoke_with_content_guard
from utils.settings import settings
from utils.logging import get_logger
from utils.common import validate_identity
from orchestration.contexts.pipeline import (
    run_pre_request_pipeline,
    SUB_AGENT_BUDGET,
    SUB_AGENT_SUMMARY_OUTPUT_BUDGET,
)

logger = get_logger(__name__)

MAX_ITERATIONS = 42
ITERATION_BUDGET = 37

_IDENTITY_FIELDS = ("sub_agent_id", "sub_agent_name", "task_id", "task_name", "sub_agent_messages")


def _build_identity_config(
    config: RunnableConfig,
    sub_agent_name: str,
    sub_agent_id: str,
    task_id: str,
    task_name: str,
) -> dict:
    """Merge sub-agent identity into the RunnableConfig metadata.

    Ensures every LLM call within this node (including summarization)
    carries correct sub-agent identity for callback resolution.

    Args:
        config:          The incoming RunnableConfig.
        sub_agent_name:  Display name of the sub-agent.
        sub_agent_id:    Unique sub-agent invocation ID.
        task_id:         Parent task ID.
        task_name:       Parent task name.

    Returns:
        New config dict with identity merged into metadata.
    """
    identity = {
        "sub_agent_name": sub_agent_name,
        "sub_agent_id": sub_agent_id,
        "task_id": task_id,
        "task_name": task_name,
    }
    merged_metadata = {**(config.get("metadata") or {}), **identity}
    return {**config, "metadata": merged_metadata}


def _check_iteration_limit(state: dict, sub_agent_id: str) -> bool:
    """Check if the sub-agent has reached the maximum iteration count.

    Args:
        state:         The sub-agent state.
        sub_agent_id:  Unique sub-agent invocation ID for logging.

    Returns:
        True if the iteration limit has been reached.
    """
    iteration_cnt = state["sub_agent_iteration"] + 1
    hit_limit = iteration_cnt >= MAX_ITERATIONS

    if hit_limit:
        logger.warning(
            "sub_agent_iteration_limit",
            sub_agent_id=sub_agent_id,
            iteration=iteration_cnt,
        )

    return hit_limit


def _inject_sub_agent_plan(messages: list, state: dict) -> list:
    """Inject sub-agent plan status into the leading SystemMessage.

    Args:
        messages: The current message list (SystemMessage + conversation).
        state:    The sub-agent state dict.

    Returns:
        Message list with updated SystemMessage. Unchanged if no plan.
    """
    sub_agent_plan = state.get("sub_agent_plan") or []
    if not sub_agent_plan or not messages or not isinstance(messages[0], SystemMessage):
        return messages

    sys_content: str = messages[0].content

    # Strip old plan marker to avoid duplication on re-injection.
    marker = "\n## CURRENT PLAN"
    if marker in sys_content:
        sys_content = sys_content[:sys_content.index(marker)]

    # Build plan status lines.
    plan_lines = ["\n## CURRENT PLAN"]
    for p in sub_agent_plan:
        icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(
            p.get("phase_status", ""), "?"
        )
        plan_lines.append(
            f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}"
        )

    sys_content += "\n".join(plan_lines)
    messages[0] = SystemMessage(content=sys_content)
    return messages


def _inject_iteration_budget(messages: list, iteration: int) -> list:
    """Inject the remaining iteration budget into the leading SystemMessage.

    Args:
        messages:  The current message list (SystemMessage + conversation).
        iteration: Iterations already consumed (0-based count).

    Returns:
        Message list with a live budget line injected. Idempotent — any
        previous budget line is replaced, never appended twice.
    """
    if not messages or not isinstance(messages[0], SystemMessage):
        return messages

    sys_content: str = messages[0].content
    marker = "\n## LIVE BUDGET"
    if marker in sys_content:
        sys_content = sys_content[:sys_content.index(marker)].rstrip()

    remaining = max(0, ITERATION_BUDGET - iteration)
    if remaining <= 0:
        budget_line = (
            f"{marker}\nYou are PAST your ~{ITERATION_BUDGET}-iteration budget. "
            "Do NOT start new work — summarize results and finish immediately."
        )
    else:
        budget_line = (
            f"{marker}\nIterations consumed: {iteration} / ~{ITERATION_BUDGET}. "
            f"Remaining: ~{remaining}. Budget your turns — finish within budget."
        )

    messages[0] = SystemMessage(content=sys_content + budget_line)
    return messages


def make_llm(tools: list | None = None):
    """Create an LLM node function.

    Args:
        tools: @tool-decorated functions. None/[] → direct LLM call.

    Returns:
        Callable[[dict, RunnableConfig], dict] — async LangGraph node function
    """
    async def llm_node(state: dict, config: RunnableConfig) -> dict:
        # Log the call to the sub-agent node with identity + iteration.
        logger.info(
            "sub_agent_node_called",
            agent_name=state.get("sub_agent_name", "N/A"),
            agent_id=state.get("sub_agent_id", "N/A"),
            task_id=state.get("task_id", "N/A"),
            task_name=state.get("task_name", "N/A"),
            iteration=state.get("sub_agent_iteration", 0),
        )

        # Get required identity fields with defensive error context.
        identity = validate_identity(state, _IDENTITY_FIELDS, logger)
        sub_agent_name = identity["sub_agent_name"]
        sub_agent_id = identity["sub_agent_id"]
        task_id = identity["task_id"]
        task_name = identity["task_name"]
        all_messages = identity["sub_agent_messages"]

        # Build config with sub-agent identity for callback resolution.
        config_with_id = _build_identity_config(
            config, sub_agent_name, sub_agent_id, task_id, task_name,
        )

        # Initialize model and bind tools if needed and not exceed the iteration_cnt
        model = init_model(
            model_name=settings.deepseek_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )
        model = model.bind_tools(tools) if tools and not _check_iteration_limit(state, sub_agent_id) else model

        # Inject sub-agent plan into SystemMessage for real-time plan awareness.
        all_messages = _inject_sub_agent_plan(list(all_messages), state)

        # Inject live iteration budget into SystemMessage (idempotent).
        all_messages = _inject_iteration_budget(
            all_messages, state["sub_agent_iteration"]
        )

        # Request pre-context pipeline: T0 Snip → T1 Microcompact → Budget check → T2 Auto-Compact.
        # Fail-open: Any exception is downgraded to the original message and does not disrupt the main orchestration process.
        try:
            pipeline_result = await run_pre_request_pipeline(
                all_messages,
                state.get("compaction_checkpoint"),
                budget=SUB_AGENT_BUDGET,
                summary_output_budget=SUB_AGENT_SUMMARY_OUTPUT_BUDGET,
                config=config_with_id,
            )
            all_messages = pipeline_result.messages_for_llm
        except Exception as e:
            logger.warning("context_pipeline_sub_agent_failed", error=str(e)[:200])
            pipeline_result = None

        # Invoke the LLM with the system prompt and message history
        try:
            response = await ainvoke_with_content_guard(
                model, all_messages, config=config_with_id, role="sub_agent"
            )
            removals = pipeline_result.removals if pipeline_result else []
            replacements = pipeline_result.replacements if pipeline_result else []
            checkpoint = pipeline_result.checkpoint if pipeline_result else state.get("compaction_checkpoint")
            if removals or replacements:
                # 观测日志：确认 T2 的删除/替换真实写回 state（add_messages 生效）。
                logger.info(
                    "sub_agent_state_writeback",
                    removals=len(removals),
                    replacements=len(replacements),
                )
            return {
                "sub_agent_messages": [response] + removals + replacements,
                "sub_agent_iteration": state["sub_agent_iteration"] + 1,
                "compaction_checkpoint": checkpoint,
            }
        except Exception as e:
            logger.error("sub_agent_llm_invocation_failed", error=str(e))
            return {
                "sub_agent_messages": [AIMessage(content="An internal error occurred. Please try again.")],
                "sub_agent_error_message": str(e),
            }

    return llm_node
