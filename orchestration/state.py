"""
LangGraph state definitions for the orchestrator graph.
"""

import operator
from typing import TypedDict, Annotated

from langgraph.graph.message import add_messages


class Plan(TypedDict):
    """A single phase in the orchestrator's execution plan.

    Attributes:
        phase_id:           Unique identifier for this phase.
        phase_name:         Short human-readable label.
        phase_status:       One of ``pending``, ``in_progress``, or ``done``.
        phase_description:  What this phase should accomplish.
    """
    phase_id: str
    phase_name: str
    phase_status: str
    phase_description: str


class SubAgentRoundTaskItem(TypedDict):
    """One task dispatched to a sub-agent during a fanout round.

    Attributes:
        task_id:                Unique identifier for this task.
        task_name:              Short human-readable label.
        task_description:       What the sub-agent should do.
        task_completion_status: Whether the task has finished.
        subagent_id:            unique identifier for the sub-agent, e.g. `programmer_id_xxx`
        subagent_name:          The sub-agent's name, short human-readable label.
    """
    task_id: str
    task_name: str
    task_description: str
    task_completion_status: bool
    subagent_id: str
    subagent_name: str


class OrchestrationState(TypedDict):
    """Top-level state carried through the orchestrator graph.

    Attributes:
        conversation_id:                Identifier for the conversation thread.
        orchestration_id:               Identifier for this orchestration run.
        messages:                       Full message history.
        user_query:                     The original user request.
        plan:                           Execution plan phases. Replaced wholesale on update.
        active_sub_agent_count:          Number of sub-agents currently executing in this fanout round.
        sub_agent_round_tasks:          Tasks dispatched in the current fanout round.
        sub_agent_outputs:              Merged outputs from completed sub-agents.
        orchestration_status:           Current status string.
        orchestration_iteration:        Current iteration number.
        should_orchestration_pause:     Flag to pause and wait for human input.
        should_orchestration_stop:      Flag to terminate the orchestration.
        context_summary:                Summary of the orchestration context.
        response:                       Final response to deliver to the user.
        output_artifacts:               Accumulated output artifacts (files, etc.).
        total_tokens:                   Running token usage counter.
        start_at:                       ISO timestamp when orchestration started.
        time_elapsed:                   Total elapsed time in seconds.
        error_message:                  Last error message, if any.
    """
    conversation_id: str
    orchestration_id: str
    messages: Annotated[list, add_messages]
    user_query: str
    plan: Annotated[list[Plan] | None, lambda _left, right: right]
    active_sub_agent_count: Annotated[int, operator.add]
    sub_agent_round_tasks: Annotated[list[SubAgentRoundTaskItem], lambda _left, right: right]
    sub_agent_outputs: Annotated[dict, lambda left, right: {**left, **right}]
    orchestration_status: str
    orchestration_iteration: int
    should_orchestration_pause: bool        # 保留：预留给 HITL 中断，尚未接入
    should_orchestration_stop: bool         # 保留：预留给显式停止，尚未接入
    context_summary: str
    response: Annotated[str, lambda _left, right: right]
    output_artifacts: Annotated[list, operator.add]
    total_tokens: Annotated[int, operator.add]
    start_at: str
    time_elapsed: float
    error_message: str
