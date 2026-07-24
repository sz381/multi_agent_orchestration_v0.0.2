TOOL_DESCRIPTIONS = {
    "make_plan": (
        "Create an execution plan with phases before delegating complex work. "
        "Only use for tasks with 3+ distinct steps. Calling again overwrites the plan.\n"
        "Parameters:\n"
        "- phases: list of dicts, each with:\n"
        "    phase_id, phase_name, phase_status: 'pending'|'in_progress'|'done', phase_description\n"
    ),
    "edit_plan": (
        "Modify a plan phase in-place (e.g. mark as 'in_progress' or 'done').\n"
        "Parameters:\n"
        "- phase_id: the phase to modify\n"
        "- update: dict of fields to change (phase_name, phase_status, phase_description)\n"
    ),
    "delete_plan": (
        "Remove a phase or clear the entire plan.\n"
        "Parameters:\n"
        "- phase_id: the phase to remove (ignored if delete_all=True)\n"
        "- delete_all: clear all phases (default False)\n"
    ),
}
