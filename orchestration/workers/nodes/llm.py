"""
LLM node factory — model invocation with optional tool binding.
"""

from langchain_core.runnables import RunnableConfig

from utils.model import init_model


def make_llm(tools: list | None = None):
    """Create an LLM node function.

    Args:
        tools: @tool-decorated functions. None/[] → direct LLM call (no tools).

    Returns:
        Callable[[dict, RunnableConfig], dict] — async LangGraph node function
    """
    async def llm_node(state: dict, config: RunnableConfig) -> dict:
        model = init_model(
            model_name="deepseek-v4-flash",
            temperature=0.3,
            max_tokens=16384,
            streaming=True,
        )
        if tools:
            model = model.bind_tools(tools)
        response = await model.ainvoke(state["messages"], config=config)
        return {"messages": [response]}

    return llm_node
