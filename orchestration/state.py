import operator
from typing import TypedDict, Annotated

from langgraph.graph.message import add_messages


class Plan(TypedDict):
    phase_id: str
    phase_name: str
    phase_status: str
    phase_description: str
    
    
class SubAgentRoundTaskItem(TypedDict):
    task_id: str
    task_name: str
    task_description: str
    task_completion_status: bool
    subagent_id: str
    subagent_name: str
    

class OrchestrationState(TypedDict):
    conversation_id: str
    orchestration_id: str
    messages: Annotated[list, add_messages]
    user_query: str
    plan: list[Plan] | None
    sub_agent_round_tasks: list[SubAgentRoundTaskItem]
    sub_agent_task: Annotated[SubAgentRoundTaskItem, lambda _left, right: right]
    sub_agent_outputs: Annotated[dict, lambda left, right: {**left, **right}]
    orchestration_status: str
    should_orchestration_pause: bool
    should_orchestration_stop: bool
    response: str
    output_artifacts: Annotated[list, lambda left, right: left + right]
    total_tokens: Annotated[int, operator.add]
    start_at: str
    time_elapsed: float
    error_message: str
    