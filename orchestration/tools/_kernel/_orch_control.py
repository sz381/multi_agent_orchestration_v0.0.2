"""
Orchestration control tools: end a run or fan out to sub-agents.
"""

import asyncio
import json

MAX_RESPONSE_LENGTH = 100_000
MAX_TASKS = 50
AVAILABLE_SUBAGENT_PREFIXES = ["programmer", "reviewer", "researcher"]
REQUIRED_TASK_FIELDS = {
    "task_id", "task_name", "task_description",
    "task_completion_status", "subagent_id", "subagent_name",
}

_lock_end = asyncio.Lock()
_lock_fanout = asyncio.Lock()


async def end_orchestration(
    response: any, 
    current_response: str = "",
    plan: list | None = None,
) -> str:
    """Deliver the final response and end the orchestration.

    Only one call per turn is allowed. Refuses to end if the plan
    has unfinished phases.

    Args:
        response: The final answer string.
        current_response: Whether end_orchestration was already called.
        plan: Current plan phases (optional). If provided, all must be
            ``done`` before ending.

    Returns:
        JSON with status and message.
    """

    async with _lock_end:
        try:
            if plan:
                pending = [p for p in plan if p.get("phase_status") != "done"]

                if pending:
                    pending_ids = [p["phase_id"] for p in pending]

                    return json.dumps({
                        "status": "error",
                        "message": (
                            f"Cannot end orchestration — {len(pending)} phase(s) "
                            f"still pending: {pending_ids}. Complete them or "
                            f"delete them before calling end_orchestration."
                        ),
                    }, ensure_ascii=False)

            if current_response:
                return json.dumps({
                    "status": "error",
                    "message": "end_orchestration already called in this turn. Ignoring duplicate call."
                }, ensure_ascii=False)

            if not isinstance(response, str):
                return json.dumps({
                    "status": "error",
                    "message": "response must be a string."
                }, ensure_ascii=False)

            if not response.strip():
                return json.dumps({
                    "status": "error",
                    "message": "response must be a non-empty string."
                }, ensure_ascii=False)

            response = response.strip()

            if len(response) > MAX_RESPONSE_LENGTH:
                return json.dumps({
                    "status": "error",
                    "message": f"response too long ({len(response)} chars). Max {MAX_RESPONSE_LENGTH}."
                }, ensure_ascii=False)

            return json.dumps({
                "status": "ok",
                "message": f"Orchestration ended.",
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": f"Unexpected error in end_orchestration: {exc}"
            }, ensure_ascii=False)


async def fanout_subagents(
    tasks: any, 
    current_tasks: list = None
) -> str:
    """Dispatch tasks to sub-agents for parallel execution.

    Only one fanout per turn is allowed. 

    Args:
        tasks: List of task dicts with required fields.
        current_tasks: Whether fanout was already called this turn.

    Returns:
        JSON with status and the validated task list.
    """
    
    async with _lock_fanout:
        try:
            if current_tasks:
                return json.dumps({
                    "status": "error",
                    "message": "fanout_subagents already called in this turn. Ignoring duplicate call."
                }, ensure_ascii=False)

            if not isinstance(tasks, list):
                return json.dumps({
                    "status": "error",
                    "message": "tasks must be a list."
                }, ensure_ascii=False)

            if not tasks:
                return json.dumps({
                    "status": "error",
                    "message": "tasks must be a non-empty list."
                }, ensure_ascii=False)

            if len(tasks) > MAX_TASKS:
                return json.dumps({
                    "status": "error",
                    "message": f"Too many tasks ({len(tasks)}). Max {MAX_TASKS}."
                }, ensure_ascii=False)

            seen_ids: set[str] = set()
            seen_subagent_ids: set[str] = set()
            clean_tasks: list[dict] = []

            for i, t in enumerate(tasks):
                if not isinstance(t, dict):
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] must be a dict, got {type(t).__name__}."
                    }, ensure_ascii=False)

                extra = set(t.keys()) - REQUIRED_TASK_FIELDS
                
                if extra:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] unknown fields: {sorted(extra)}. Allowed: {sorted(REQUIRED_TASK_FIELDS)}."
                    }, ensure_ascii=False)

                missing = REQUIRED_TASK_FIELDS - t.keys()
                
                if missing:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] missing required fields: {sorted(missing)}."
                    }, ensure_ascii=False)

                tid = t["task_id"]
                
                if not isinstance(tid, str) or not tid.strip():
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] task_id must be a non-empty string."
                    }, ensure_ascii=False)
                    
                tid = tid.strip()

                if tid in seen_ids:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] duplicate task_id: '{tid}'."
                    }, ensure_ascii=False)
                    
                seen_ids.add(tid)

                if not isinstance(t["task_name"], str) or not t["task_name"].strip():
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] task_name must be a non-empty string."
                    }, ensure_ascii=False)

                if not isinstance(t["task_description"], str) or not t["task_description"].strip():
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] task_description must be a non-empty string."
                    }, ensure_ascii=False)

                if t["task_completion_status"] is not False:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] task_completion_status must be false."
                    }, ensure_ascii=False)

                sid = t["subagent_id"]
                
                if not isinstance(sid, str) or not sid.strip():
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] subagent_id must be a non-empty string."
                    }, ensure_ascii=False)
                    
                sid = sid.strip()
                prefix = sid.split("_", 1)[0]
                
                if prefix not in AVAILABLE_SUBAGENT_PREFIXES:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] subagent_id '{sid}' has invalid prefix '{prefix}'. Available: {AVAILABLE_SUBAGENT_PREFIXES}."
                    }, ensure_ascii=False)

                if not isinstance(t["subagent_name"], str) or not t["subagent_name"].strip():
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] subagent_name must be a non-empty string."
                    }, ensure_ascii=False)

                sid_stripped = sid.strip()
                if sid_stripped in seen_subagent_ids:
                    return json.dumps({
                        "status": "error",
                        "message": f"task[{i}] duplicate subagent_id: '{sid_stripped}'. Each sub-agent can only handle one task per fanout."
                    }, ensure_ascii=False)
                seen_subagent_ids.add(sid_stripped)

                clean_tasks.append({
                    "task_id": tid,
                    "task_name": t["task_name"].strip(),
                    "task_description": t["task_description"].strip(),
                    "task_completion_status": False,
                    "subagent_id": sid,
                    "subagent_name": t["subagent_name"].strip(),
                })

            return json.dumps({
                "status": "ok",
                "message": f"Dispatched {len(clean_tasks)} task(s) to subagents.",
                "tasks": clean_tasks,
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": f"Unexpected error in fanout_subagents: {exc}"
            }, ensure_ascii=False)
