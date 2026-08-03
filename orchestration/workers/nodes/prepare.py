"""Prepare node factory — inject original system prompt + task description.

each sub-agent invocation gets clean [SystemMessage, HumanMessage],
not inheriting the main orchestrator's conversation history.
"""
import time

import structlog
from langchain_core.messages import SystemMessage, HumanMessage

from utils.logging import get_logger
from utils.settings import settings
from utils.common import validate_identity

logger = get_logger(__name__)

_IDENTITY_FIELDS = ("sub_agent_id", "sub_agent_name", "task_id", "task_name", "task_description")


def _log_sub_agent_start(
    name: str,
    sub_agent_id: str,
    sub_agent_name: str,
    task_id: str,
    task_name: str,
) -> None:
    """Log sub-agent start event with full identity context.

    Args:
        name:            Sub-agent type name (e.g. "programmer").
        sub_agent_id:    Unique sub-agent invocation ID.
        sub_agent_name:  Display name of the sub-agent.
        task_id:         Parent task ID.
        task_name:       Parent task name.
    """
    logger.info(
        "sub_agent_start",
        sub_agent=name,
        sub_agent_id=sub_agent_id,
        sub_agent_name=sub_agent_name,
        task_id=task_id,
        task_name=task_name,
    )


def _bind_identity_context(sub_agent_name: str, sub_agent_id: str, task_id: str, task_name: str) -> None:
    """Bind sub-agent identity to structlog contextvars for callback fallback.

    Cleans any leaked contextvars from a previous crashed sub-agent first,
    then binds current identity so that on_tool_start can resolve sub-agent
    identity when the tool_call_id main path is unavailable.

    Args:
        sub_agent_name:  Display name of the sub-agent.
        sub_agent_id:    Unique sub-agent invocation ID.
        task_id:         Parent task ID.
        task_name:       Parent task name.
    """
    structlog.contextvars.unbind_contextvars(
        "sub_agent_name", "sub_agent_id", "task_id", "task_name",
    )
    structlog.contextvars.bind_contextvars(
        sub_agent_name=sub_agent_name,
        sub_agent_id=sub_agent_id,
        task_id=task_id,
        task_name=task_name,
    )


def _inject_workspace_dir(system_prompt: str, state: dict) -> str:
    """Inject workspace and project directory into the system prompt.

    Args:
        system_prompt: The raw system prompt template.
        state:         The sub-agent state dict, may contain project_dir.

    Returns:
        System prompt with <CURRENT_WORKSPACE> replaced.
    """
    workspace_info = f"Your workspace root is: {settings.workspace_dir}"
    project_dir = state.get("project_dir", "").strip()

    if project_dir:
        workspace_info += f"\nYour project directory is: {project_dir}\nAll file operations should be scoped to this directory."

    return system_prompt.replace("<CURRENT_WORKSPACE>", workspace_info)


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
        identity = validate_identity(state, _IDENTITY_FIELDS, logger)
        task_description = identity["task_description"]
        sub_agent_name = identity["sub_agent_name"]
        sub_agent_id = identity["sub_agent_id"]
        task_id = identity["task_id"]
        task_name = identity["task_name"]

        # Log sub-agent start.
        t_start = time.time()
        _log_sub_agent_start(name, sub_agent_id, sub_agent_name, task_id, task_name)

        # Bind identity context for callback.
        try:
            _bind_identity_context(sub_agent_name, sub_agent_id, task_id, task_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to bind identity context: {e.__class__.__name__}: {e}"
            ) from e

        # Prepare system prompt with workspace injected.
        try:
            system_content = _inject_workspace_dir(system_prompt, state)
        except Exception as e:
            raise RuntimeError(
                f"Failed to inject workspace: {e.__class__.__name__}: {e}"
            ) from e

        return {
            "sub_agent_messages": [
                SystemMessage(content=system_content),
                HumanMessage(content=task_description),
            ],
            "sub_agent_start_at": str(t_start),
            "sub_agent_iteration": 0,
            "file_changes": [],
        }

    return prepare_node
