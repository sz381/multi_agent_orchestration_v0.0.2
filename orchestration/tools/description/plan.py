"""
LLM-facing descriptions for the plan management tools.
"""

TOOL_DESCRIPTION = {
    "make_plan": (
        "Create an execution plan with phases. Use before fanout_subagents for "
        "complex multi-phase work (e.g. research → implement → review). "
        "After fanout results arrive, use edit_plan to update phase status.\n"
        "Params:\n"
        "- phases: list of dicts, each with exactly:\n"
        "    phase_id (unique string), phase_name (string),\n"
        "    phase_status ('pending'|'in_progress'|'done'), phase_description (string)\n"
        "Limits: max 12 phases. Calling again overwrites the plan. No extra fields allowed."
    ),
    "edit_plan": (
        "Modify one or more plan phases in a single call (e.g. mark phases as in_progress/done).\n"
        "Params:\n"
        "- updates: list of dicts, each with phase_id (string, must exist) plus any of:\n"
        "    phase_name, phase_status ('pending'|'in_progress'|'done'), phase_description\n"
        "Example: [{\"phase_id\":\"1\",\"phase_status\":\"done\"}, {\"phase_id\":\"2\",\"phase_status\":\"in_progress\"}]\n"
        "Do NOT call edit_plan multiple times in parallel — batch all updates in one call."
    ),
    "delete_plan": (
        "Remove a phase or clear the entire plan.\n"
        "Params:\n"
        "- phase_id: the phase to remove (must exist, ignored if delete_all=True)\n"
        "- delete_all: clear all phases (default False)"
    ),
}
