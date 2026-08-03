"""
Summarize node factory — extract final response, artifacts, and metadata.
"""

import time

import structlog
from langchain_core.messages import AIMessage

from utils.logging import get_logger
from utils.model import count_tokens
from utils.common import validate_identity

logger = get_logger(__name__)

_FILE_PRODUCING_TOOLS: dict[str, str] = {
    "write_file": "file_path",
    "str_replace": "file_path",
}

_IDENTITY_FIELDS = ("task_id", "sub_agent_messages", "sub_agent_id", "sub_agent_name", "task_name")

_BORDER = "=" * 108

MAX_FILES_SHOWN = 20


def _log_sub_agent_done(
    name: str,
    sub_agent_id: str,
    sub_agent_name: str,
    task_id: str,
    task_name: str,
    elapsed: float,
    artifacts: list[str],
    total_tokens: int,
    total_prompt_tokens: int,
    total_completion_tokens: int,
    final_text: str,
) -> None:
    """Log sub-agent completion with full metadata.

    Args:
        name:                   Sub-agent type name (e.g. "programmer").
        sub_agent_id:           Unique sub-agent invocation ID.
        sub_agent_name:         Display name of the sub-agent.
        task_id:                Parent task ID.
        task_name:              Parent task name.
        elapsed:                Total elapsed seconds.
        artifacts:              List of file paths produced.
        total_tokens:           Total token usage.
        total_prompt_tokens:    Prompt tokens consumed.
        total_completion_tokens: Completion tokens generated.
        final_text:             Final response text (empty → "empty_output").
    """
    logger.info(
        "sub_agent_done",
        sub_agent=name,
        sub_agent_id=sub_agent_id,
        sub_agent_name=sub_agent_name,
        task_id=task_id,
        task_name=task_name,
        elapsed=round(elapsed, 1),
        artifacts_count=len(artifacts),
        total_tokens=total_tokens,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        status="success" if final_text else "empty_output",
    )


def _print_sub_agent_done(
    name: str,
    sub_agent_id: str,
    sub_agent_name: str,
    task_id: str,
    task_name: str,
    elapsed: float,
    iteration: int,
    total_tokens: int,
    total_prompt_tokens: int,
    total_completion_tokens: int,
    artifacts: list[str],
) -> None:
    """
    Print a bordered summary block when a sub-agent completes.
    """
    if total_completion_tokens > 0:
        ratio = f"{total_prompt_tokens / total_completion_tokens:.1f}:1"
    else:
        ratio = "N/A"

    print(_BORDER)
    print(f"  ✅ [{name}] SUBAGENT DONE  {task_id}  ({sub_agent_name})")
    print(_BORDER)
    print(f"  agent:    {sub_agent_id} | {sub_agent_name} | {task_name}")
    print(f"  elapsed:  {elapsed:.1f}s    iters={iteration}")
    print(f"  tokens:   total={total_tokens}  prompt={total_prompt_tokens}  completion={total_completion_tokens}  ratio={ratio}")
    print(f"  files:    {len(artifacts)}")
    for i, path in enumerate(artifacts[:MAX_FILES_SHOWN], 1):
        print(f"            {i}. {path},")
    if len(artifacts) > MAX_FILES_SHOWN:
        print(f"            ... 共 {len(artifacts)} 个文件，仅显示前 {MAX_FILES_SHOWN} 个")
    print(_BORDER)


def _extract_artifacts(messages: list) -> list[str]:
    """Discover file paths produced by write tools during the session.

    Scans every AIMessage for tool calls whose name matches a known 
    file-producing tool. Extracts the target file path from each matched
    call's arguments and returns a deduplicated, order-preserving list.

    Args:
        messages: The sub-agent's full message history.

    Returns:
        List of unique absolute file paths written or modified by the agent,
        in first-seen order.
    """
    seen: set[str] = set()
    artifacts: list[str] = []

    for msg in messages:
        if not isinstance(msg, AIMessage) or not msg.tool_calls:
            continue

        for tc in msg.tool_calls:
            param_name = _FILE_PRODUCING_TOOLS.get(tc["name"])
            
            if param_name:
                path = tc.get("args", {}).get(param_name, "")

                if path and path not in seen:
                    seen.add(path)
                    artifacts.append(path)

    return artifacts


def _get_final_response(messages: list) -> tuple[str, AIMessage]:
    """Find the last AIMessage with content in the message history.

    Args:
        messages: The sub-agent's full message history.

    Returns:
        Tuple of (content, message).

    Raises:
        ValueError: If no AIMessage with content is found in the history.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content, msg

    raise ValueError(
        f"No AIMessage with content found in sub-agent message history "
        f"({len(messages)} messages total). The sub-agent produced no "
        f"usable response, which would cause the Orchestrator to hallucinate."
    )


def _unbind_identity_context() -> None:
    """
    Clean up sub-agent identity contextvars bound in prepare_node.
    """
    structlog.contextvars.unbind_contextvars(
        "sub_agent_name", "sub_agent_id", "task_id", "task_name",
    )


def make_summarize(name: str):
    """Create a summarize node that extracts sub-agent results.

    Args:
        name: sub-agent type for logging (e.g. "programmer")

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    async def summarize_node(state: dict) -> dict:
        try:
            # Get required fields with defensive error context.
            identity = validate_identity(state, _IDENTITY_FIELDS, logger)
            task_id = identity["task_id"]
            messages = identity["sub_agent_messages"]
            sub_agent_id = identity["sub_agent_id"]
            sub_agent_name = identity["sub_agent_name"]
            task_name = identity["task_name"]

            # Extract last AIMessage as sub-agent's final response.
            final_text, last_aimessage = _get_final_response(messages)

            # extract output artifacts from subagent's response
            # Prioritize real-time recording (file_changes is written during tool execution, and T2 compression does not affect it);
            # Support for old paths: When the real-time recording is empty, revert to extracting from the message history.
            artifacts = state.get("file_changes") or []
            if not artifacts:
                artifacts = _extract_artifacts(messages)

            # calculate the time elapse
            start_at = float(state.get("sub_agent_start_at", 0))
            elapsed = time.time() - start_at if start_at else 0

            # calculate the total tokens, prompts tokens, completion tokens
            try:
                token_counts = count_tokens(messages)
                total_prompt_tokens = token_counts["prompt_tokens"]
                total_completion_tokens = token_counts["completion_tokens"]
                total_tokens = token_counts["total_tokens"]
            except Exception:
                total_prompt_tokens = 0
                total_completion_tokens = 0
                total_tokens = 0

            # Log completion and print summary.
            _log_sub_agent_done(
                name, sub_agent_id, sub_agent_name, task_id, task_name,
                elapsed, artifacts, total_tokens, total_prompt_tokens, total_completion_tokens,
                final_text,
            )

            # print sub_agent done with specfic format
            iteration = state.get("sub_agent_iteration", 0)
            _print_sub_agent_done(
                name, sub_agent_id, sub_agent_name, task_id, task_name,
                elapsed, iteration, total_tokens, total_prompt_tokens,
                total_completion_tokens, artifacts,
            )

            return {
                "messages": [last_aimessage],
                "sub_agent_outputs": {
                    task_id: {
                        "task_id": task_id,
                        "task_name": task_name,
                        "sub_agent": name,
                        "sub_agent_id": sub_agent_id,
                        "sub_agent_name": sub_agent_name,
                        "result_summary": final_text,
                        "artifacts": artifacts,
                        "token_used": total_tokens,
                        "status": "success" if final_text else "empty_output",
                        "elapsed_seconds": round(elapsed, 1),
                    }
                },
                "total_tokens": total_tokens,
                "sub_agent_time_elapsed": round(elapsed, 1),
                "sub_agent_error_message": "" if final_text else "empty output",
            }
        finally:
            # Clean up contextvars bound in prepare_node, even on failure.
            _unbind_identity_context()

    return summarize_node
