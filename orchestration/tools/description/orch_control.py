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
        "- tasks: list of dicts, each with:\n"
        "    task_id, task_name, task_description, task_completion_status: false,\n"
        "    subagent_id: 'programmer_1' | 'researcher_1' | 'reviewer_1',\n"
        "    subagent_name (string)\n"
        "Available subagents: programmer_1, researcher_1, reviewer_1\n"
    ),
}
