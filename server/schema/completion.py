"""
Streaming response data models.
"""

from pydantic import BaseModel, Field


class PlanPhase(BaseModel):
    """A single phase in the orchestrator's execution plan.

    Mirrors ``OrchestrationState.plan`` entries so the frontend can render
    a live plan board.

    Attributes:
        phase_id:          Unique identifier for this phase.
        phase_name:        Short human-readable label.
        phase_status:      One of ``pending``, ``in_progress``, or ``done``.
        phase_description: What this phase should accomplish.
    """

    phase_id: str
    phase_name: str
    phase_status: str
    phase_description: str | None = None


class FileChange(BaseModel):
    """File produced by a sub-agent.

    Mirrors ``SubAgentState.file_changes`` — the core layer collects
    produced file paths (deduplicated); the streaming layer only relays
    them. No action/summary: the core layer records neither today.

    Attributes:
        path: File path written or modified by the sub-agent.
    """

    path: str


class WorkerDone(BaseModel):
    """Sub-agent completion event.

    Pushed when a sub-agent finishes its task (success or failure).

    Attributes:
        sub_agent_id:    Unique identifier of the sub-agent.
        sub_agent_name:  Short human-readable label.
        status:          ``done`` or ``failed``.
        token_used:      Tokens consumed by this sub-agent.
        elapsed:         Seconds the sub-agent ran.
        summary:         Final conclusion excerpt for display.
    """

    sub_agent_id: str
    sub_agent_name: str
    status: str
    token_used: int | None = None
    elapsed: float | None = None
    summary: str | None = None


class ToolStatus(BaseModel):
    """Tool call status event.

    Inserted into the streaming response to inform the frontend about the
    lifecycle of a tool call.

    Attributes:
        action:          "executing" (start execution) or "done" (finished).
        name:            Tool name, e.g. "glob", "grep".
        params:          Tool parameters, only present when action="executing".
        tool_call_id:    Unique ID for the tool call.
    """

    action: str
    name: str
    params: dict | None = None
    tool_call_id: str | None = None


class Delta(BaseModel):
    """Streaming incremental content block.

    Every delta is type-tagged; the frontend branches on ``type`` instead
    of probing which payload field is set.

    Attributes:
        type:           Event type: "stream" / "plan" / "tool_status" /
                        "tool_output" / "worker_done" / "file_changes".
                        Terminal states "done"/"error" live on Choice
                        (status / finish_reason / error_message) instead.
        content:        Incremental text, may be None or empty string.
        tool_status:    Tool call lifecycle event, only for action="executing" or "done".
        tool_output:    Tool execution result content, sent after tool_status action="done".
                        Frontend may truncate display as needed; no backend length limit.
        plan:           Live plan snapshot (all phases), pushed on plan changes.
        file_changes:   File write/modify events produced by sub-agents.
        worker_done:    Sub-agent completion event.
    """

    type: str = "stream"
    content: str | None = None
    tool_status: ToolStatus | None = None
    tool_output: str | None = None
    plan: list[PlanPhase] | None = None
    file_changes: list[FileChange] | None = None
    worker_done: WorkerDone | None = None


class Choice(BaseModel):
    """Single candidate reply frame.

    Attributes:
        delta:                  Streaming content or structured event payload.
        index:                  Frame index within the response.
        finish_reason:          "stop" when the orchestration finished.
        orchestration_id:       Identifier of this orchestration run (HTTP-level task key).
        conversation_id:        Identifier of the conversation thread (multi-turn anchor).
        sub_agent_id:           Sub-agent emitting this frame (None for orchestrator).
        sub_agent_name:         Human-readable sub-agent label.
        task_id:                Sub-agent task id (fanout internal concept).
        task_name:              Human-readable task label.
        task_description:       What the task asked the sub-agent to do.
        active_sub_agent_count: Number of sub-agents currently running (fanout progress).
        token_used:             Tokens consumed (per sub-agent on worker_done, or total at end).
        elapsed:                Seconds elapsed (per sub-agent on worker_done, or total at end).
        status:                 Orchestration status: "pending" / "running" / "done" / "failed".
        error_message:          Failure reason when status="failed".
    """

    # None only on terminal frames (done / error): the delta would
    # otherwise serialize as an empty {"type": "stream"} block.
    delta: Delta | None = Field(default_factory=Delta)
    index: int = 0
    finish_reason: str | None = None
    orchestration_id: str | None = None
    conversation_id: str | None = None
    sub_agent_id: str | None = None
    sub_agent_name: str | None = None
    task_id: str | None = None
    task_name: str | None = None
    task_description: str | None = None
    active_sub_agent_count: int | None = None
    token_used: int | None = None
    elapsed: float | None = None
    status: str | None = None
    error_message: str | None = None


class StreamChunk(BaseModel):
    """SSE event payload.

    Each push is a JSON object with a top-level `choices` list.

    Attributes:
        choices: List of candidate replies (usually only one element).
    """

    choices: list[Choice] = Field(default_factory=list)
