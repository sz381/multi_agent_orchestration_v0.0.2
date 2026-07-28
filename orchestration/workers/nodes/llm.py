"""
LLM node factory — model invocation with optional tool binding.
"""

from langchain_core.runnables import RunnableConfig

from utils.model import init_model
from utils.retry import ainvoke_with_retry


def make_llm(tools: list | None = None):
    """Create an LLM node function.

    Args:
        tools: @tool-decorated functions. None/[] → direct LLM call.

    Returns:
        Callable[[dict, RunnableConfig], dict] — async LangGraph node function
    """
    async def llm_node(state: dict, config: RunnableConfig) -> dict:
        # Inject sub-agent identity into metadata so callbacks can resolve it.
        identity = {
            "sub_agent_name": state["sub_agent_name"],
            "sub_agent_id": state["sub_agent_id"],
            "task_id": state["task_id"],
            "task_name": state["task_name"],
        }
        merged_metadata = {**(config.get("metadata") or {}), **identity}
        config = {**config, "metadata": merged_metadata}

        model = init_model(
            model_name="deepseek-v4-flash",
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )

        if tools:
            model = model.bind_tools(tools)

        response = await ainvoke_with_retry(model, state["messages"], config=config)
        return {"messages": [response]}

    return llm_node
