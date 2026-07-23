from orchestration.state import OrchestrationState
from utils.model import init_model


async def orchestrator_node(state: OrchestrationState) -> dict:
    model = init_model(
        model_name="deepseek-v4-flash",
        temperature=0.3,
        max_tokens=16384,
        streaming=True,
    )
    response = await model.ainvoke(state["user_query"])

    return {
        "messages": [response],
        "response": response.content,
        "orchestration_status": "DONE",
    }


async def interrupt_node(state: OrchestrationState) -> dict:
    return {}
