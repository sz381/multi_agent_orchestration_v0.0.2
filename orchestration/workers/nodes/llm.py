"""
LLM node factory — model invocation with optional tool binding.
"""

from langchain_core.runnables import RunnableConfig
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langchain_core.messages import SystemMessage

from utils.model import init_model, ainvoke_with_retry
from utils.settings import settings
from utils.logging import get_logger
from utils.summarize import maybe_summarize

logger = get_logger(__name__)

MAX_ITERATIONS = 50  # hard cap: after this many ReAct loops, force final answer


def make_llm(tools: list | None = None):
    """Create an LLM node function.

    Args:
        tools: @tool-decorated functions. None/[] → direct LLM call.

    Returns:
        Callable[[dict, RunnableConfig], dict] — async LangGraph node function
    """
    async def llm_node(state: dict, config: RunnableConfig) -> dict:

        # Get required fields from state with defensive error context.
        try:
            sub_agent_name = state["sub_agent_name"]
            sub_agent_id = state["sub_agent_id"]
            task_id = state["task_id"]
            task_name = state["task_name"]
            all_messages = state["sub_agent_messages"]
        except KeyError as e:
            logger.error(
                "sub_agent_identity_missing",
                missing_key=str(e),
                available_keys=list(state.keys()),
            )
            raise

        # Summarize old messages when history grows too long.
        summary = state.get("summary", "")
        new_summary, all_messages = await maybe_summarize(all_messages, summary)

        # Inject sub-agent identity into metadata so callbacks can resolve it.
        identity = {
            "sub_agent_name": sub_agent_name,
            "sub_agent_id": sub_agent_id,
            "task_id": task_id,
            "task_name": task_name,
        }
        merged_metadata = {**(config.get("metadata") or {}), **identity}
        config = {**config, "metadata": merged_metadata}

        # Check iteration limit — if exceeded, force final answer without tools.
        iteration = state.get("sub_agent_iteration", 0) + 1
        hit_limit = iteration >= MAX_ITERATIONS
        if hit_limit:
            logger.warning(
                "sub_agent_iteration_limit",
                sub_agent_id=sub_agent_id,
                iteration=iteration,
            )

        # Initialize model and bind tools if needed.
        model = init_model(
            model_name=settings.deepseek_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )

        if tools and not hit_limit:
            model = model.bind_tools(tools)

        # Trim message history to prevent O(n²) token growth in ReAct loops.
        # Keeps system prompt + task description (via start_on="human"),
        # and only the most recent ~6000 tokens of conversation.
        # State retains full history for token accounting / artifact extraction.
        messages = trim_messages(
            all_messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=50000,
            start_on="human",
            end_on=("human", "tool"),
            include_system=True,
        )

        # Ensure the first HumanMessage (the task description) is never trimmed away.
        # all_messages[0] is a SystemMessage preserved by include_system=True;
        # the task is always the first HumanMessage.
        if all_messages and messages:
            for m in all_messages:
                if m.type == "human":
                    if m.id not in {msg.id for msg in messages}:
                        messages = [m] + messages
                    break

        # Inject the latest sub_agent_plan and conversation summary into
        # SystemMessage so the LLM always sees real-time state.
        sub_agent_plan = state.get("sub_agent_plan") or []
        if messages and isinstance(messages[0], SystemMessage):
            sys_content = messages[0].content
            # Strip old markers to avoid duplication on re-injection.
            for marker in ("\n## CURRENT PLAN", "\n## CONVERSATION SUMMARY"):
                if marker in sys_content:
                    sys_content = sys_content[:sys_content.index(marker)]
            # Inject conversation summary first (historical context).
            if new_summary:
                sys_content += f"\n\n## CONVERSATION SUMMARY\n{new_summary}"
            # Inject current plan (execution state).
            if sub_agent_plan:
                plan_lines = ["\n## CURRENT PLAN"]
                for p in sub_agent_plan:
                    status = p.get("phase_status", "pending")
                    icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(status, "?")
                    plan_lines.append(
                        f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}"
                    )
                sys_content += "\n".join(plan_lines)
            messages[0] = SystemMessage(content=sys_content)

        response = await ainvoke_with_retry(model, messages, config=config)
        return {"sub_agent_messages": [response], "sub_agent_iteration": iteration, "summary": new_summary}

    return llm_node
