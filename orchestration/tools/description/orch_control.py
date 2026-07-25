ORCH_CONTROL_DESCRIPTIONS = {
    "finish": (
        "Deliver the final response to the user. MUST be the last tool you call "
        "every turn. Do NOT call any other tool after finish.\n"
        "Parameters:\n"
        "- response: the final answer (string)\n"
    ),
    "delegate": (
        "Delegate sub-tasks to specialist agents for parallel execution. "
        "Only use for multi-domain, parallelizable work. Handle simple tasks yourself.\n"
        "Parameters:\n"
        "- tasks: list of dicts, each with:\n"
        "    task_id, task_name, task_description, task_completion_status: false,\n"
        "    subagent_id: 'programmer' | 'reviewer' | 'search', subagent_name\n"
    ),
}
