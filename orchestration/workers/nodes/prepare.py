"""Prepare node factory — inject system prompt + task description.

Context Isolation: each worker invocation gets clean [SystemMessage, HumanMessage],
not inheriting the main orchestrator's conversation history.
"""

import time

from langchain_core.messages import SystemMessage, HumanMessage

from utils.logging import get_logger


def make_prepare(name: str, system_prompt: str):
    """Create a prepare node that initializes worker context.

    Args:
        name:          worker name for logging (e.g. "programmer")
        system_prompt: the worker's system prompt string

    Returns:
        Callable[[dict], dict] — LangGraph node function
    """
    def prepare_node(state: dict) -> dict:
        task_id = state["task_id"]
        description = state["task_description"]

        t_start = time.time()
        logger = get_logger(__name__)
        logger.info("worker_start", worker=name, task_id=task_id)

        return {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=description),
            ],
            "start_at": str(t_start),
        }

    return prepare_node
