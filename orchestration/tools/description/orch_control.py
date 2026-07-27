"""
LLM-facing descriptions for orchestration control tools.
"""

TOOL_DESCRIPTION = {
    "end_orchestration": (
        "Deliver the final response to the user. MUST be the last tool you call "
        "every turn. Do NOT call any other tool after end_orchestration.\n"
        "Parameters:\n"
        "- response: the final answer (string)\n"
    ),
    "fanout_subagents": (
        "Fan out sub-tasks to specialist agents for parallel execution. "
        "Only use for multi-domain, parallelizable work. Handle simple tasks yourself.\n"
        "Parameters:\n"
        "- tasks: list of dicts, each with:\n"
        "    task_id, task_name, task_description, task_completion_status: false,\n"
        "    subagent_id: 'programmer' | 'reviewer' | 'researcher', subagent_name\n"
    ),
}
