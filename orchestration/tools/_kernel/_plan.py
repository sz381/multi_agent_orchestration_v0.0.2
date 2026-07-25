import json

VALID_STATUSES = {"pending", "in_progress", "done"}
REQUIRED_FIELDS = {"phase_id", "phase_name", "phase_status", "phase_description"}
ALLOWED_UPDATE_FIELDS = {"phase_name", "phase_status", "phase_description"}


def make_plan(
    phases: list[dict]
) -> str:
    if not isinstance(phases, list) or not phases:
        return json.dumps({
            "status": "error",
            "message": "phases must be a non-empty list."
        }, ensure_ascii=False)

    MAX_PHASES = 12
    if len(phases) > MAX_PHASES:
        return json.dumps({
            "status": "error",
            "message": f"Too many phases ({len(phases)}). Max {MAX_PHASES}."
        }, ensure_ascii=False)

    seen_ids: set[str] = set()
    clean_phases: list[dict] = []

    for i, p in enumerate(phases):
        if not isinstance(p, dict):
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] must be a dict, got {type(p).__name__}."
            }, ensure_ascii=False)

        extra = set(p.keys()) - REQUIRED_FIELDS
        if extra:
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] unknown fields: {sorted(extra)}. Allowed: {sorted(REQUIRED_FIELDS)}."
            }, ensure_ascii=False)

        missing = REQUIRED_FIELDS - p.keys()
        if missing:
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] missing required fields: {sorted(missing)}."
            }, ensure_ascii=False)

        pid = p["phase_id"]
        if not isinstance(pid, str) or not pid.strip():
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] phase_id must be a non-empty string."
            }, ensure_ascii=False)
        pid = pid.strip()

        if pid in seen_ids:
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] duplicate phase_id: '{pid}'."
            }, ensure_ascii=False)
        seen_ids.add(pid)

        if not isinstance(p["phase_name"], str) or not p["phase_name"].strip():
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] phase_name must be a non-empty string."
            }, ensure_ascii=False)

        status = p["phase_status"]
        if status not in VALID_STATUSES:
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] phase_status must be one of {sorted(VALID_STATUSES)}, got '{status}'."
            }, ensure_ascii=False)

        if not isinstance(p["phase_description"], str) or not p["phase_description"].strip():
            return json.dumps({
                "status": "error",
                "message": f"phase[{i}] phase_description must be a non-empty string."
            }, ensure_ascii=False)

        clean_phases.append({
            "phase_id": pid,
            "phase_name": p["phase_name"].strip(),
            "phase_status": status,
            "phase_description": p["phase_description"].strip(),
        })

    return json.dumps({
        "status": "ok",
        "message": f"Plan created with {len(clean_phases)} phases.",
        "plan": clean_phases,
    }, ensure_ascii=False)


def edit_plan(
    updates: list[dict], 
    plan: list[dict]
) -> str:
    if not isinstance(updates, list) or not updates:
        return json.dumps({
            "status": "error",
            "message": "updates must be a non-empty list."
        }, ensure_ascii=False)

    if not plan:
        return json.dumps({
            "status": "error",
            "message": "No plan exists. Use make_plan first."
        }, ensure_ascii=False)

    for i, u in enumerate(updates):
        if not isinstance(u, dict):
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] must be a dict."
            }, ensure_ascii=False)

        if "phase_id" not in u:
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] missing 'phase_id'."
            }, ensure_ascii=False)

        pid = u["phase_id"]
        if not isinstance(pid, str) or not pid.strip():
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] phase_id must be a non-empty string."
            }, ensure_ascii=False)

        update_fields = {k: v for k, v in u.items() if k != "phase_id"}
        if not update_fields:
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] has no fields to update. Allowed: {sorted(ALLOWED_UPDATE_FIELDS)}."
            }, ensure_ascii=False)

        extra = set(update_fields.keys()) - ALLOWED_UPDATE_FIELDS
        if extra:
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] unknown fields: {sorted(extra)}. Allowed: {sorted(ALLOWED_UPDATE_FIELDS)}."
            }, ensure_ascii=False)

        if "phase_status" in update_fields:
            if update_fields["phase_status"] not in VALID_STATUSES:
                return json.dumps({
                    "status": "error",
                    "message": f"updates[{i}] phase_status must be one of {sorted(VALID_STATUSES)}, got '{update_fields['phase_status']}'."
                }, ensure_ascii=False)

        if "phase_name" in update_fields:
            if not isinstance(update_fields["phase_name"], str) or not update_fields["phase_name"].strip():
                return json.dumps({
                    "status": "error",
                    "message": f"updates[{i}] phase_name must be a non-empty string."
                }, ensure_ascii=False)

        if "phase_description" in update_fields:
            if not isinstance(update_fields["phase_description"], str) or not update_fields["phase_description"].strip():
                return json.dumps({
                    "status": "error",
                    "message": f"updates[{i}] phase_description must be a non-empty string."
                }, ensure_ascii=False)

    plan_ids = {p["phase_id"] for p in plan}
    for i, u in enumerate(updates):
        pid = u["phase_id"].strip()
        if pid not in plan_ids:
            return json.dumps({
                "status": "error",
                "message": f"updates[{i}] phase_id '{pid}' not found in plan."
            }, ensure_ascii=False)

    new_plan = [dict(p) for p in plan]
    updated_ids = []
    for u in updates:
        pid = u["phase_id"].strip()
        for p in new_plan:
            if p["phase_id"] == pid:
                if "phase_name" in u:
                    p["phase_name"] = u["phase_name"].strip()
                if "phase_status" in u:
                    p["phase_status"] = u["phase_status"]
                if "phase_description" in u:
                    p["phase_description"] = u["phase_description"].strip()
                updated_ids.append(pid)
                break

    return json.dumps({
        "status": "ok",
        "message": f"Updated {len(updated_ids)} phase(s): {', '.join(updated_ids)}.",
        "plan": new_plan,
    }, ensure_ascii=False)


def delete_plan(
    phase_id: str, 
    plan: list[dict],
    delete_all: bool = False,
) -> str:
    if delete_all:
        return json.dumps({
            "status": "ok",
            "message": "All plans deleted.",
            "plan": [],
        }, ensure_ascii=False)

    if not isinstance(phase_id, str) or not phase_id.strip():
        return json.dumps({
            "status": "error",
            "message": "phase_id must be a non-empty string."
        }, ensure_ascii=False)
    phase_id = phase_id.strip()

    if not plan:
        return json.dumps({
            "status": "error",
            "message": "No plan exists. Use make_plan first."
        }, ensure_ascii=False)

    new_plan = [p for p in plan if p.get("phase_id") != phase_id]

    if len(new_plan) == len(plan):
        return json.dumps({
            "status": "error",
            "message": f"phase_id '{phase_id}' not found in plan."
        }, ensure_ascii=False)

    return json.dumps({
        "status": "ok",
        "message": f"Phase '{phase_id}' deleted.",
        "plan": new_plan,
    }, ensure_ascii=False)
