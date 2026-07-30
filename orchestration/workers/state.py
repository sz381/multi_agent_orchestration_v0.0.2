import operator
from typing import TypedDict, Annotated

from langgraph.graph.message import add_messages


class Plan(TypedDict):
    phase_id: str
    phase_name: str
    phase_status: str
    phase_description: str


class SubAgentState(TypedDict):
    sub_agent_id: str
    sub_agent_name: str
    task_id: str
    task_name: str
    task_description: str
    sub_agent_messages: Annotated[list, add_messages]
    output_artifacts: Annotated[list, operator.add]
    sub_agent_outputs: Annotated[dict, lambda left, right: {**left, **right}]
    total_tokens: int
    sub_agent_iteration: int
    sub_agent_start_at: str
    sub_agent_time_elapsed: float
    sub_agent_error_message: str
    summary: str


class ProgrammerSubAgentState(SubAgentState):
    sub_agent_plan: Annotated[list[Plan] | None, lambda _left, right: right]


class ResearcherSubAgentState(SubAgentState):
    pass


class ReviewerSubAgentState(SubAgentState):
    pass
