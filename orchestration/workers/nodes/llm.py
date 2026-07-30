"""
LLM node factory — model invocation with optional tool binding.
"""

from langchain_core.runnables import RunnableConfig

from utils.model import init_model, ainvoke_with_retry
from utils.settings import settings
from utils.logging import get_logger

logger = get_logger(__name__)


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
            messages = state["sub_agent_messages"]
        except KeyError as e:
            logger.error(
                "sub_agent_identity_missing",
                missing_key=str(e),
                available_keys=list(state.keys()),
            )
            raise

        # Inject sub-agent identity into metadata so callbacks can resolve it.
        identity = {
            "sub_agent_name": sub_agent_name,
            "sub_agent_id": sub_agent_id,
            "task_id": task_id,
            "task_name": task_name,
        }
        merged_metadata = {**(config.get("metadata") or {}), **identity}
        config = {**config, "metadata": merged_metadata}

        # Initialize model and bind tools if needed.
        model = init_model(
            model_name=settings.deepseek_model_name,
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )

        if tools:
            model = model.bind_tools(tools)

        response = await ainvoke_with_retry(model, messages, config=config)
        return {"sub_agent_messages": [response]}

    return llm_node
