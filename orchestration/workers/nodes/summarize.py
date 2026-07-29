"""
Summarize node factory — extract final response, artifacts, and metadata.
"""

import time

import structlog
from langchain_core.messages import AIMessage

from utils.logging import get_logger
from utils.model import count_tokens

logger = get_logger(__name__)


_FILE_PRODUCING_TOOLS: dict[str, str] = {
    "write_file": "file_path",
    "str_replace": "file_path",
}


def _extract_artifacts(messages: list) -> list[str]:
    """Discover file paths produced by write tools during the session.

    Scans every :class:`~langchain_core.messages.AIMessage` for tool
    calls whose name matches a known file-producing tool (``write_file``,
    ``str_replace``).  Extracts the target file path from each matched
    call's arguments and returns a deduplicated, order-preserving list.

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


def make_summarize(name: str):
    """Create a summarize node that extracts sub-agent results.

    1. Find last AIMessage.content → response
    2. Extract file artifacts from tool calls
    3. Elapsed time + log completion

    Args:
        name: sub-agent type for logging (e.g. "programmer")

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    async def summarize_node(state: dict) -> dict:
        # Get required fields with defensive error context.
        try:
            task_id = state["task_id"]
            messages = state["messages"]
            sub_agent_id = state["sub_agent_id"]
            sub_agent_name = state["sub_agent_name"]
            task_name = state["task_name"]
        except KeyError as e:
            logger.error(
                "summarize_state_field_missing",
                missing_key=str(e),
                available_keys=list(state.keys()),
            )
            raise

        try:
            # find the last message of subagent as response
            final_text = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    final_text = msg.content
                    break

            # extract output artifacts from subagent's response
            artifacts = _extract_artifacts(messages)

            # calculate the time elapse and log it out "subagent_done"
            start_at = float(state.get("sub_agent_start_at", 0))
            elapsed = time.time() - start_at if start_at else 0

            # calculate the total tokens, prompts tokens, completion tokens
            token_counts = count_tokens(messages)
            total_prompt_tokens = token_counts["prompt_tokens"]
            total_completion_tokens = token_counts["completion_tokens"]
            total_tokens = token_counts["total_tokens"]

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

            print(f"  ✅ [{name}] {task_id} done  {sub_agent_id} | {sub_agent_name} | {task_name}  {elapsed:.1f}s")
            print(f"     💰 total={total_tokens} tokens  completion={total_completion_tokens}  prompt={total_prompt_tokens}  [{sub_agent_id}] {sub_agent_name}")

            token_used = total_tokens

            # return the final output
            return {
                "output_artifacts": artifacts,
                "sub_agent_outputs": {
                    task_id: {
                        "task_id": task_id,
                        "sub_agent": name,
                        "sub_agent_id": sub_agent_id,
                        "sub_agent_name": sub_agent_name,
                        "result_summary": final_text,
                        "artifacts": artifacts,
                        "token_used": token_used,
                        "status": "success" if final_text else "empty_output",
                        "elapsed_seconds": round(elapsed, 1),
                    }
                },
                "total_tokens": token_used,
                "sub_agent_time_elapsed": round(elapsed, 1),
                "sub_agent_error_message": "" if final_text else "empty output",
            }
        finally:
            # Always clean up contextvars bound in prepare_node, even on failure.
            structlog.contextvars.unbind_contextvars(
                "sub_agent_name", "sub_agent_id", "task_id", "task_name",
            )

    return summarize_node
