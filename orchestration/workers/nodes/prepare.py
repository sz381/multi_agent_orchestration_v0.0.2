"""Prepare node factory — inject system prompt + task description.

each sub-agent invocation gets clean [SystemMessage, HumanMessage],
not inheriting the main orchestrator's conversation history.
"""

import time

import structlog
from langchain_core.messages import SystemMessage, HumanMessage

from utils.logging import get_logger
from utils.settings import settings

logger = get_logger(__name__)


def make_prepare(name: str, system_prompt: str):
    """Create a prepare node that initializes sub-agent context.

    Args:
        name:          sub-agent type for logging (e.g. "programmer")
        system_prompt: the sub-agent's system prompt string

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    async def prepare_node(state: dict) -> dict:
        # Get required identity fields with defensive error context.
        try:
            task_id = state["task_id"]
            task_name = state["task_name"]
            task_description = state["task_description"]
            sub_agent_name = state["sub_agent_name"]
            sub_agent_id = state["sub_agent_id"]
        except KeyError as e:
            logger.error(
                "sub_agent_identity_missing",
                missing_key=str(e),
                available_keys=list(state.keys()),
            )
            raise

        # Check for empty task description.
        if not task_description or not task_description.strip():
            logger.warning(
                "sub_agent_empty_task_description",
                sub_agent_id=sub_agent_id,
                task_id=task_id,
            )

        # Log sub-agent start.
        t_start = time.time()
        logger.info(
            "sub_agent_start",
            sub_agent=name,
            sub_agent_id=sub_agent_id,
            sub_agent_name=sub_agent_name,
            task_id=task_id,
            task_name=task_name,
        )

        # Pre-clean any leaked contextvars from a previous crashed sub-agent,
        # then bind current identity as fallback for on_tool_start when the
        # tool_call_id main path is unavailable.
        structlog.contextvars.unbind_contextvars(
            "sub_agent_name", "sub_agent_id", "task_id", "task_name",
        )
        structlog.contextvars.bind_contextvars(
            sub_agent_name=sub_agent_name,
            sub_agent_id=sub_agent_id,
            task_id=task_id,
            task_name=task_name,
        )

        # Prepare system content; inject workspace and project dir if available.
        workspace_info = f"Your workspace root is: {settings.workspace_dir or '.'}"
        project_dir = state.get("project_dir", "").strip()
        if project_dir:
            workspace_info += f"\nYour project directory is: {project_dir}\nAll file operations should be scoped to this directory."

        system_content = system_prompt.replace(
            "<CURRENT_WORKSPACE>",
            workspace_info,
        )

        # return prepared messages
        return {
            "sub_agent_messages": [
                SystemMessage(content=system_content),
                HumanMessage(content=task_description),
            ],
            "sub_agent_start_at": str(t_start),
            "sub_agent_iteration": 0,
            "summary": "",
        }

    return prepare_node
