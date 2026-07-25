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


async def end_orchestration(response: any, current_response: str = "") -> str:
    async with _lock_end:
        try:
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


async def fanout_subagents(tasks: any, current_tasks: list = None) -> str:
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
