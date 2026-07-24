from langchain_core.messages import SystemMessage, AIMessage

from orchestration.state import OrchestrationState
from orchestration.prompts.sys_prompt_orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from utils.model import init_model
from utils.logging import get_logger

logger = get_logger(__name__)

_model = init_model(
    model_name="deepseek-v4-flash",
    temperature=0.3,
    max_tokens=16384,
    streaming=True,
)
_model_with_tools = _model.bind_tools(ORCHESTRATOR_TOOLS)


async def orchestrator_node(state: OrchestrationState) -> dict:
    try:
        system_msg = SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT)
        history = state.get("messages", [])
        messages = [system_msg] + history

        response = await _model_with_tools.ainvoke(messages)

        return {"messages": [response]}

    except Exception as e:
        logger.error(f"Orchestrator invocation failed: {e}", exc_info=True)
        return {
            "messages": [AIMessage(content="An internal error occurred. Please try again.")],
            "error_message": str(e),
        }


async def interrupt_node(state: OrchestrationState) -> dict:
    return {}
