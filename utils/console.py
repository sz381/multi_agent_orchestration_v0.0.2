"""
Terminal rendering helpers: PLAN / FANOUT rich-text blocks.
"""

import json

from langchain_core.messages import ToolMessage

_STATUS_ICONS = {"pending": "○", "in_progress": "◐", "done": "●"}
_PLAN_TOOLS = ("make_plan", "edit_plan", "delete_plan")


def plan_block(plan) -> str:
    """Render a plan snapshot as a terminal status block.

    Args:
        plan: List of phase dicts (phase_id / phase_name /
              phase_description / phase_status).

    Returns:
        Multi-line block with one status icon per phase.
    """
    lines = ["\n" + "=" * 60, f"  PLAN ({len(plan)} phases)", "=" * 60]
    for p in plan:
        icon = _STATUS_ICONS.get(p.get("phase_status", ""), "○")
        lines.append(f"  {icon} [{p.get('phase_id', '?')}] {p.get('phase_name', '?')}")
        desc = p.get("phase_description", "")
        if desc:
            lines.append(f"      {desc}")
    lines.append("=" * 60)
    return "\n".join(lines)


def fanout_block(tasks) -> str:
    """Render a sub-agent fanout dispatch as a terminal status block.

    Args:
        tasks: List of task dicts (task_id / task_name /
               task_description / subagent_id / subagent_name /
               task_completion_status).

    Returns:
        Multi-line block with one status icon per dispatched task.
    """
    lines = ["\n" + "=" * 60, f"  FANOUT — {len(tasks)} task(s) dispatched", "=" * 60]
    for t in tasks:
        icon = "●" if t.get("task_completion_status") else "○"
        lines.append(f"  {icon} [{t.get('task_id', '?')}] {t.get('task_name', '?')}")
        lines.append(f"      agent: {t.get('subagent_name', '?')} ({t.get('subagent_id', '?')})")
        desc = t.get("task_description", "")
        if desc:
            lines.append(f"      {desc}")
    lines.append("=" * 60)
    return "\n".join(lines)


def tool_summary(msg: ToolMessage) -> str:
    """Build the terminal summary for a tool message.

    Only plan mutations (make_plan / edit_plan / delete_plan) and
    sub-agent fanout dispatch are rendered as rich blocks; every other
    tool message yields an empty string so the caller can skip it.

    Args:
        msg: ToolMessage produced by a tool node.

    Returns:
        Rendered block, or "" when the message is not worth printing.
    """
    try:
        r = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return ""
    if msg.name in _PLAN_TOOLS:
        return plan_block(r["plan"]) if r.get("plan") else ""
    if msg.name == "fanout_subagents":
        return fanout_block(r["tasks"]) if r.get("tasks") else ""
    return ""
