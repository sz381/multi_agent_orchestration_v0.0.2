"""
LangGraph state definitions for the orchestrator graph.
"""

import operator
from typing import TypedDict, Annotated, NotRequired

from langgraph.graph.message import add_messages

from orchestration.contexts.auto_compact import CompactionCheckpoint


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


def _merge_round_tasks(
    left: list[SubAgentRoundTaskItem] | None,
    right: list[SubAgentRoundTaskItem] | None,
) -> list[SubAgentRoundTaskItem]:
    """Merge fanout round tasks by task_id (parallel-fanout fallback).

    Two write semantics must be supported:
    - Dispatch (right is a non-empty list): merge with the current list by
      task_id so parallel fanout_subagents calls in one round all survive
      (8_03_006: 3 reviewer tasks vanished under an overwrite reducer,
      counter inflated 3+2=5 while only 2 branches ran → collect never
      reached zero → graph ENDed early). A repeated task_id keeps the
      later write.
    - Reset (right is an empty list): the collect barrier clears the round
      tasks after all branches finish; an empty write must actually empty
      the list, otherwise old tasks linger and get re-dispatched forever
      (8_03_007: 3 tasks re-ran 4x rewriting the same files, and new
      fanout_subagents calls were rejected as "duplicate" against the
      stale list).
    """
    if right is None:
        return list(left or [])
    if not right:
        return []
    if not left:
        return list(right)
    merged: dict[str, SubAgentRoundTaskItem] = {t["task_id"]: t for t in left}
    for t in right:
        merged[t["task_id"]] = t
    return list(merged.values())


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
        response:                       Final response to deliver to the user.
        total_tokens:                   Running token usage counter.
        start_at:                       ISO timestamp when orchestration started.
        time_elapsed:                   Total elapsed time in seconds.
        error_message:                  Last error message, if any.
        compaction_checkpoint:          Auto-Compact compression cursor (T2 incremental compression state).
    """

    conversation_id: str
    orchestration_id: str
    messages: Annotated[list, add_messages]
    user_query: str
    plan: Annotated[list[Plan] | None, lambda _left, right: right]
    active_sub_agent_count: Annotated[int, operator.add]
    sub_agent_round_tasks: Annotated[list[SubAgentRoundTaskItem], _merge_round_tasks]
    sub_agent_outputs: Annotated[dict, lambda left, right: {**left, **right}]
    orchestration_status: str
    orchestration_iteration: int
    should_orchestration_pause: bool        # 保留：预留给 HITL 中断，尚未接入
    should_orchestration_stop: bool         # 保留：预留给显式停止，尚未接入
    response: Annotated[str, lambda _left, right: right]
    total_tokens: Annotated[int, operator.add]
    start_at: str
    time_elapsed: float
    error_message: str
    compaction_checkpoint: NotRequired[CompactionCheckpoint | None]
