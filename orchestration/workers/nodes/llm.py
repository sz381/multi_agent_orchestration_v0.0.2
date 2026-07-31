"""
LLM node factory — model invocation with optional tool binding.

⚠️ 缺少 Trim Message + Summarize Message 逻辑，此逻辑不必现在添加，需要等到 全量注入稳定了之后在做 context engineering
"""

from langchain_core.runnables import RunnableConfig
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langchain_core.messages import SystemMessage

from utils.model import init_model, ainvoke_with_retry
from utils.settings import settings
from utils.logging import get_logger
from utils.summarize import maybe_summarize
from utils.common import validate_identity
from utils.settings import settings

logger = get_logger(__name__)

MAX_ITERATIONS = 42

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


def make_llm(tools: list | None = None):
    """Create an LLM node function.

    Args:
        tools: @tool-decorated functions. None/[] → direct LLM call.

    Returns:
        Callable[[dict, RunnableConfig], dict] — async LangGraph node function
    """
    async def llm_node(state: dict, config: RunnableConfig) -> dict:
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
            model_name=settings.xiaomi_mimo_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )
        model = model.bind_tools(tools) if tools and not _check_iteration_limit(state, sub_agent_id) else model

        # Inject sub-agent plan into SystemMessage for real-time plan awareness.
        all_messages = _inject_sub_agent_plan(list(all_messages), state)

        # Invoke the LLM with the system prompt and message history
        try:
            response = await ainvoke_with_retry(model, all_messages, config=config_with_id)
            return {
                "sub_agent_messages": [response], 
                "sub_agent_iteration": state["sub_agent_iteration"] + 1, 
                "sub_agent_context_summary": ""
            }
        except Exception as e:
            logger.error("sub_agent_llm_invocation_failed", error=str(e))
            return {
                "sub_agent_messages": [AIMessage(content="An internal error occurred. Please try again.")],
                "sub_agent_error_message": str(e),
            }

    return llm_node

























# """
# LLM node factory — model invocation with optional tool binding.
# """

# from langchain_core.runnables import RunnableConfig
# from langchain_core.messages.utils import trim_messages, count_tokens_approximately
# from langchain_core.messages import SystemMessage

# from utils.model import init_model, ainvoke_with_retry
# from utils.settings import settings
# from utils.logging import get_logger
# from utils.summarize import maybe_summarize

# logger = get_logger(__name__)

# MAX_ITERATIONS = 30  # hard cap: after this many ReAct loops, force final answer


# def make_llm(tools: list | None = None):
#     """Create an LLM node function.

#     Args:
#         tools: @tool-decorated functions. None/[] → direct LLM call.

#     Returns:
#         Callable[[dict, RunnableConfig], dict] — async LangGraph node function
#     """
#     async def llm_node(state: dict, config: RunnableConfig) -> dict:

#         # Get required fields from state with defensive error context.
#         try:
#             sub_agent_name = state["sub_agent_name"]
#             sub_agent_id = state["sub_agent_id"]
#             task_id = state["task_id"]
#             task_name = state["task_name"]
#             all_messages = state["sub_agent_messages"]
#         except KeyError as e:
#             logger.error(
#                 "sub_agent_identity_missing",
#                 missing_key=str(e),
#                 available_keys=list(state.keys()),
#             )
#             raise

#         # Build sub-agent identity metadata early so that any LLM call
#         # within this node (including summarization) carries correct identity.
#         identity = {
#             "sub_agent_name": sub_agent_name,
#             "sub_agent_id": sub_agent_id,
#             "task_id": task_id,
#             "task_name": task_name,
#         }
#         merged_metadata = {**(config.get("metadata") or {}), **identity}
#         config_with_id = {**config, "metadata": merged_metadata}

#         # Summarize old messages when history grows too long.
#         summary = state.get("sub_agent_context_summary", "")
#         new_summary, all_messages = await maybe_summarize(
#             all_messages, summary, config=config_with_id,
#         )

#         # Check iteration limit — if exceeded, force final answer without tools.
#         iteration = state.get("sub_agent_iteration", 0) + 1
#         hit_limit = iteration >= MAX_ITERATIONS
#         if hit_limit:
#             logger.warning(
#                 "sub_agent_iteration_limit",
#                 sub_agent_id=sub_agent_id,
#                 iteration=iteration,
#             )

#         # Initialize model and bind tools if needed.
#         model = init_model(
#             model_name=settings.deepseek_model_name,
#             temperature=0.3,
#             max_tokens=16384,
#             streaming=True,
#         )

#         if tools and not hit_limit:
#             model = model.bind_tools(tools)

#         # Trim message history to prevent O(n²) token growth in ReAct loops.
#         # Keeps system prompt + task description (via start_on="human"),
#         # and only the most recent ~6000 tokens of conversation.
#         # State retains full history for token accounting / artifact extraction.
#         messages = trim_messages(
#             all_messages,
#             strategy="last",
#             token_counter=count_tokens_approximately,
#             max_tokens=50000,
#             start_on="human",
#             end_on=("human", "tool"),
#             include_system=True,
#         )

#         # Ensure the first HumanMessage (the task description) is never trimmed away.
#         # all_messages[0] is a SystemMessage preserved by include_system=True;
#         # the task is always the first HumanMessage.
#         if all_messages and messages:
#             for m in all_messages:
#                 if m.type == "human":
#                     if m.id not in {msg.id for msg in messages}:
#                         messages = [m] + messages
#                     break

#         # Inject the latest sub_agent_plan and conversation summary into
#         # SystemMessage so the LLM always sees real-time state.
#         sub_agent_plan = state.get("sub_agent_plan") or []
#         if messages and isinstance(messages[0], SystemMessage):
#             sys_content = messages[0].content
#             # Strip old markers to avoid duplication on re-injection.
#             for marker in ("\n## CURRENT PLAN", "\n## CONVERSATION SUMMARY"):
#                 if marker in sys_content:
#                     sys_content = sys_content[:sys_content.index(marker)]
#             # Inject conversation summary first (historical context).
#             if new_summary:
#                 sys_content += f"\n\n## CONVERSATION SUMMARY\n{new_summary}"
#             # Inject current plan (execution state).
#             if sub_agent_plan:
#                 plan_lines = ["\n## CURRENT PLAN"]
#                 for p in sub_agent_plan:
#                     status = p.get("phase_status", "pending")
#                     icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(status, "?")
#                     plan_lines.append(
#                         f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}"
#                     )
#                 sys_content += "\n".join(plan_lines)
#             messages[0] = SystemMessage(content=sys_content)

#         response = await ainvoke_with_retry(model, messages, config=config_with_id)
#         return {"sub_agent_messages": [response], "sub_agent_iteration": iteration, "sub_agent_context_summary": new_summary}

#     return llm_node
