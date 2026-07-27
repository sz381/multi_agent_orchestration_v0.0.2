"""Summarize node factory — extract final response, artifacts, and metadata."""

import time

from langchain_core.messages import AIMessage

from utils.logging import get_logger


_FILE_PRODUCING_TOOLS: dict[str, str] = {
    "write_file": "file_path",
    "str_replace": "file_path",
}


def _extract_artifacts(messages: list) -> list[str]:
    """Extract file paths produced by write_file/str_replace tool calls (deduplicated)."""
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
    """Create a summarize node that extracts worker results.

    1. Find last AIMessage.content → response
    2. Extract file artifacts from tool calls
    3. Elapsed time + log completion

    Args:
        name: worker name for logging (e.g. "programmer")

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    def summarize_node(state: dict) -> dict:
        task_id = state["task_id"]

        final_text = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                final_text = msg.content
                break

        artifacts = _extract_artifacts(state["messages"])

        start_at = float(state.get("worker_start_at", 0))
        elapsed = time.time() - start_at if start_at else 0

        logger = get_logger(__name__)
        logger.info(
            "worker_done",
            worker=name,
            task_id=task_id,
            elapsed=round(elapsed, 1),
            artifacts_count=len(artifacts),
            status="success" if final_text else "empty_output",
        )

        print(f"  [{name}] {task_id}  done  {elapsed:.1f}s")

        token_used = state.get("total_tokens", 0)

        return {
            "output_artifacts": artifacts,
            "sub_agent_outputs": {
                task_id: {
                    "task_id": task_id,
                    "worker": name,
                    "result_summary": final_text,
                    "artifacts": artifacts,
                    "token_used": token_used,
                    "status": "success" if final_text else "empty_output",
                    "elapsed_seconds": round(elapsed, 1),
                }
            },
            "total_tokens": token_used,
            "worker_time_elapsed": round(elapsed, 1),
            "worker_error_message": "" if final_text else "empty output",
        }

    return summarize_node
