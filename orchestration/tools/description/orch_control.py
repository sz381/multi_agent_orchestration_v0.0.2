"""
LLM-facing descriptions for orchestration control tools.
"""

TOOL_DESCRIPTION = {
    "end_orchestration": (
        "Deliver the final response to the user. MUST be the last tool you call. "
        "Do NOT call any other tool after end_orchestration.\n"
        "Parameters:\n"
        "- response: the final answer (string)\n"
    ),
    "fanout_subagents": (
        "Delegate independent tasks to specialist agents for parallel execution. "
        "Use for: multiple components/files, multiple research topics, mixed work types.\n"
        "Do NOT use when task B depends on task A's output — run those sequentially.\n"
        "Parameters:\n"
        "- tasks: list of dicts, each with ALL required fields:\n"
        "    task_id (unique string),\n"
        "    task_name (the task itself, e.g. 'Implement auth module'),\n"
        "    task_description (detailed instructions),\n"
        "    task_completion_status: false,\n"
        "    subagent_id: 'programmer_1' | 'researcher_1' | 'reviewer_1',\n"
        "    subagent_name (the agent's ROLE, e.g. 'Auth Developer' — NOT the same as task_name)\n"
        "Each subagent_id can only appear ONCE per fanout.\n"
        "Available subagents: programmer_1, researcher_1, reviewer_1\n"
    ),
}
