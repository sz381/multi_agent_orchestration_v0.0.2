"""Prepare node factory — inject system prompt + task description.

each sub-agent invocation gets clean [SystemMessage, HumanMessage],
not inheriting the main orchestrator's conversation history.
"""

import time

import structlog
from langchain_core.messages import SystemMessage, HumanMessage

from utils.logging import get_logger


def make_prepare(name: str, system_prompt: str):
    """Create a prepare node that initializes sub-agent context.

    Args:
        name:          sub-agent type for logging (e.g. "programmer")
        system_prompt: the sub-agent's system prompt string

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    async def prepare_node(state: dict) -> dict:
        task_id = state["task_id"]
        description = state["task_description"]

        t_start = time.time()
        logger = get_logger(__name__)
        logger.info(
            "sub_agent_start",
            sub_agent=name,
            sub_agent_id=state["sub_agent_id"],
            sub_agent_name=state["sub_agent_name"],
            task_id=task_id,
            task_name=state["task_name"],
        )

        # Bind identity to structlog contextvars — fallback for on_tool_start
        # when the tool_call_id main path is unavailable.
        structlog.contextvars.bind_contextvars(
            sub_agent_name=state["sub_agent_name"],
            sub_agent_id=state["sub_agent_id"],
            task_id=task_id,
            task_name=state["task_name"],
        )

        system_content = system_prompt
        sub_agent_plan = state.get("sub_agent_plan") or []
        if sub_agent_plan:
            plan_lines = ["\n## CURRENT PLAN"]
            for p in sub_agent_plan:
                icon = {"pending": "○", "in_progress": "◐", "done": "●"}.get(p["phase_status"], "?")
                plan_lines.append(f"  {icon} [{p['phase_id']}] {p['phase_name']}")
            system_content += "\n".join(plan_lines)

        return {
            "messages": [
                SystemMessage(content=system_content),
                HumanMessage(content=description),
            ],
            "sub_agent_start_at": str(t_start),
        }

    return prepare_node
