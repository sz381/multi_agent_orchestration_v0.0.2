# !! building !!, soemthings need to be changed here! 
import operator
from typing import TypedDict, Annotated

from langgraph.graph.message import add_messages


class CurrentWorkingTaskItem(TypedDict):
    task_id: str
    task_name: str
    task_description: str
    task_completion_status: bool
    

class Plan(TypedDict):
    phase_id: str
    phase_name: str
    phase_status: str
    phase_description: str
    

class SubAgentState(TypedDict):
    conversation_id: str
    orchestration_id: str
    sub_agent_id: str
    sub_agent_name: str
    messages: Annotated[list, add_messages]
    orchestrator_query: str
    current_working_status: str
    curernt_working_task: CurrentWorkingTaskItem
    response: Annotated[str, lambda _left, right: right]
    output_artifacts: Annotated[list, lambda left, right: left + right]
    total_tokens: Annotated[int, operator.add]
    start_at: str
    time_elapsed: float
    error_message: str
    
    
class ProgrammerSubAgentState(SubAgentState):
    plan: Annotated[list[Plan] | None, lambda _left, right: right]
    # should_pause (以后遇到一些危险命令或者沙箱通过不了的或其他需求需要 human-in-the-loop 支持，先留个钩子？)


class ResearcherSubAgentState(SubAgentState):
    pass


class ReviewerSubAgentState(SubAgentState):
    pass
