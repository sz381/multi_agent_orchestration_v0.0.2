def make_plan(phases: list[dict]) -> list[dict]:
    return phases


def edit_plan(phase_id: str, update: dict, plan: list[dict]) -> list[dict]:
    for p in plan:
        if p.get("phase_id") == phase_id:
            p.update(update)
            return plan
    return plan


def delete_plan(phase_id: str, plan: list[dict]) -> list[dict]:
    return [p for p in plan if p.get("phase_id") != phase_id]
