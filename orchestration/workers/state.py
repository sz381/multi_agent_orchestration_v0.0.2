"""
LangGraph state definitions for the workers subgraph.
"""

import operator
from typing import TypedDict, Annotated, NotRequired

from langgraph.graph.message import add_messages

from orchestration.contexts.auto_compact import CompactionCheckpoint


class Plan(TypedDict):
    """A single phase in the subagent's execution plan.

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


class SubAgentState(TypedDict):
    """Top-level state carried through the workers subgraph.

    Attributes:
        sub_agent_id:                     Unique identifier for this sub-agent.
        sub_agent_name:                   Short human-readable label for sub-agent.
        task_id:                          Unique identifier for sub-agent's task.
        task_name:                        Short human-readable label for task.
        task_description:                 What the sub-agent should do in this task.
        sub_agent_messages:               Full message history.
        sub_agent_outputs:                Merged outputs from completed sub-agents.
        total_tokens:                     Running token usage counter.
        sub_agent_iteration:              ReAct loop iteration count.
        sub_agent_start_at:               ISO timestamp when sub-agent started.
        sub_agent_time_elapsed:           Total elapsed time in seconds.
        sub_agent_error_message:          Last error message, if any.
        compaction_checkpoint:            Auto-Compact compression cursor (T2 incremental compression state).
        file_changes:                     Real-time record of files written/modified
    """

    sub_agent_id: str
    sub_agent_name: str
    task_id: str
    task_name: str
    task_description: str
    sub_agent_messages: Annotated[list, add_messages]
    sub_agent_outputs: Annotated[dict, lambda left, right: {**left, **right}]
    file_changes: Annotated[list, operator.add]
    total_tokens: int
    sub_agent_iteration: int
    sub_agent_start_at: str
    sub_agent_time_elapsed: float
    sub_agent_error_message: str
    compaction_checkpoint: NotRequired[CompactionCheckpoint | None]


class ProgrammerSubAgentState(SubAgentState):
    """
    SubAgentState extended with an execution plan for the programmer.

    Attributes:
        sub_agent_plan:                 Execution plan for the programmer.
    """

    sub_agent_plan: Annotated[list[Plan] | None, lambda _left, right: right]


class ResearcherSubAgentState(SubAgentState):
    """
    SubAgentState specialised for the researcher sub-agent.
    """

    pass


class ReviewerSubAgentState(SubAgentState):
    """
    SubAgentState specialised for the reviewer sub-agent.
    """

    pass
