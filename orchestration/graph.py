"""
Orchestrator main graph: nodes, routing, and sub-agent fanout/collect.
"""

import json

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import Send
from langchain_core.messages import AIMessage

from orchestration.state import OrchestrationState
from utils.logging import get_logger

from orchestration.orchestrator import make_orchestrator_node, make_interrupt_node
from orchestration.tools.bundles.orchestrator import ORCHESTRATOR_TOOLS
from orchestration.workers import (
    PROGRAMMER_GRAPH,
    RESEARCHER_GRAPH,
    REVIEWER_GRAPH,
)

logger = get_logger(__name__)

SUB_AGENT_RESULT_PREFIX = "[SUB-AGENT RESULT]"          # sub_agent's prefix when injected into orchestrator's context
MAX_RESULT_SUMMARY_CHARS = 1500                         # max number of chars that can be injected into orchestrator's context 

# Continuous parsing failure rollback: If the result of continuous parsing fails to be parsed successfully for a consecutive number 
# of times reaching the threshold, it is regarded as a tool contract change (systemic failure), and the route will directly raise an error 
# to shut down the process instead of continuing it silently.
_end_parse_failures = 0
_END_PARSE_FAILURES_LIMIT = 3


def _has_tool_calls(state: OrchestrationState) -> bool:
    """Whether the latest message requests tool calls.

    Used to decide if the orchestrator should continue into the tool node.

    Returns:
        True if the last message carries tool calls; False on empty history.
    """
    messages = state["messages"]
    if not messages:
        return False
    return bool(getattr(messages[-1], "tool_calls", None))


def _end_orchestration_succeeded(state: OrchestrationState) -> bool:
    """Determine whether `end_orchestration` succeeded in the most recent tool result.

    The `end_orchestration` tool returns only a JSON string and does not update the
    state. Therefore, the termination signal can only be identified from the tool
    execution result: scan the tail of the message sequence (the ToolMessage block
    after the last AI message). If `end_orchestration` is found with `status == "ok"`,
    terminate immediately. This prevents "ghost continuation" where the system
    continues running after declaring end (e.g., issue 8_02_005: after end, it wrote
    "hello world" outside the project directory).
    """
    global _end_parse_failures
    for m in reversed(state.get("messages") or []):
        if m.type == "ai":
            break
        if m.type == "tool" and getattr(m, "name", "") == "end_orchestration":
            try:
                parsed = json.loads(str(m.content))
                _end_parse_failures = 0
                return parsed.get("status") == "ok"
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                _end_parse_failures += 1

                # Result parsing for #end_orchestration failed: #end was successful but #content was damaged.
                # Silently returning False will cause the orchestrator to be reawakened (ghost continuation), 
                # and consecutive failures reaching the threshold will be regarded as a contract change, 
                # and raising a shutdown will expose the problem.
                logger.warning(
                    "end_orchestration 结果解析失败, ⚠️请注意排查",
                    message_id=getattr(m, "id", "N/A"),
                    content_preview=str(m.content)[:200],
                    consecutive_failures=_end_parse_failures,
                )

                if _end_parse_failures >= _END_PARSE_FAILURES_LIMIT:
                    raise RuntimeError(
                        "end_orchestration 结果连续 "
                        f"{_END_PARSE_FAILURES_LIMIT} 次解析失败, 疑似工具契约变更"
                    ) from exc
                    
                return False

    return False


def _route_after_orchestrator(state: OrchestrationState) -> str:
    """Route the orchestrator after an LLM step.

    Priority: human-in-the-loop pause, explicit stop, then tool-call
    continuation; otherwise the orchestration is complete.

    Returns:
        "interrupt", "tools", or END.
    """
    if state.get("should_orchestration_pause"):
        return "interrupt"

    if state.get("should_orchestration_stop"):
        return END

    if _has_tool_calls(state):
        return "tools"

    return END


def _route_after_tools(state: OrchestrationState):
    """Route after the tool node runs.

    Terminates immediately when end_orchestration succeeded (the tool does
    not update state, so the signal is read from the tool result); otherwise
    fans out sub-agents when tasks are pending, or returns to the orchestrator
    for the next round.

    Returns:
        END, a list of Send for parallel sub-agent dispatch, or "orchestrator".
    """
    # End_orchestration successfully executed → Forced termination, and the orchestrator will no longer be awakened unconditionally.
    # (The end tool does not update the state; the termination signal can only be identified from the tool's result.) 
    if _end_orchestration_succeeded(state):
        return END

    tasks = state.get("sub_agent_round_tasks") or []
    if tasks:
        return [
            Send(
                node=t["subagent_id"].split("_", 1)[0],
                arg={
                    "sub_agent_id": t["subagent_id"],
                    "sub_agent_name": t["subagent_name"],
                    "task_id": t["task_id"],
                    "task_name": t["task_name"],
                    "task_description": t["task_description"],
                    **(
                        {"project_dir": t["project_dir"]}
                        if t.get("project_dir", "").strip()
                        else {}
                    ),
                },
            )
            for t in tasks
        ]

    return "orchestrator"


def _format_sub_agent_result(task_id: str, out: dict) -> str:
    """Format the result of a sub-agent into a report text that is injected into the parent context.

    Args:
        task_id:    Sub-agent task ID (key of sub_agent_outputs).
        out:        The result dictionary of this task in sub_agent_outputs. 

    Returns:
        Individual report text.
    """
    summary = (out.get("result_summary") or "").strip()

    if not summary:
        logger.warning(
            "no sub_agent_result_summary, ⚠️请注意排查",
            task_id=task_id,
            sub_agent=out.get("sub_agent", "N/A"),
            sub_agent_id=out.get("sub_agent_id", "N/A"),
            sub_agent_name=out.get("sub_agent_name", "N/A"),
            status=out.get("status", "unknown"),
        )

    if len(summary) > MAX_RESULT_SUMMARY_CHARS:
        summary = summary[:MAX_RESULT_SUMMARY_CHARS] + "... [truncated]"

    status = out.get("status", "unknown")
    elapsed = out.get("elapsed_seconds", 0)
    artifacts = out.get("artifacts") or []
    
    return (
        f"{SUB_AGENT_RESULT_PREFIX} task_id={task_id} "
        f"status={status} elapsed={elapsed}s artifacts={len(artifacts)}\n"
        f"summary: {summary}"
    )


def _build_result_injections(state: OrchestrationState) -> list:
    """Construct an un-injected sub-agent to complete the reporting message. 

    Traverse the accumulated sub_agent_outputs, and construct an AIMessage for each task;
    If there is already a report with the same task_id in the parent messages (matched by prefix), skip it;
    To avoid redundant injection during multiple rounds of fanout. 

    Returns:
        The AIMessage list (which is empty if no new messages are added). 
    """
    outputs = state.get("sub_agent_outputs") or {}
    if not outputs:
        tasks = state.get("sub_agent_round_tasks") or []
        logger.warning(
            "no sub_agent_outputs, ⚠️请注意排查",
            round_tasks_count=len(tasks),
            sub_agent_ids=[t.get("subagent_id", "N/A") for t in tasks],
            task_ids=[t.get("task_id", "N/A") for t in tasks],
            active_sub_agent_count=state.get("active_sub_agent_count", "N/A"),
            orchestration_iteration=state.get("orchestration_iteration", "N/A"),
            user_query=(state.get("user_query") or "")[:120],
        )
        return []

    # Find the task_id in the parent message that says "The report has already been injected."
    # The sub_agent_outputs are accumulated across rounds (the reducer combines **left and right**, and the key is task_id). 
    # And the _collect_sub_agent_results function is called by this function at the end of each round to 
    # collect the sub-agent results - if no deduplication is performed, when collecting in the second round, 
    # the old reports already injected in the first round will be injected again, resulting in duplicate task results
    #  in the orchestrator's context.
    injected_ids = set()
    for m in state.get("messages") or []:
        content = str(getattr(m, "content", "") or "")
        for tid in outputs:
            if f"{SUB_AGENT_RESULT_PREFIX} task_id={tid} " in content:
                injected_ids.add(tid)

    # Skip the injected ones, and for each remaining tid, call _format_sub_agent_result to 
    # generate the report text (status / elapsed / artifact count / summary, summary), and package it as AIMessage.
    injections = []
    for tid, out in outputs.items():
        if tid in injected_ids:
            continue

        # Assign a stable ID to the message. (Idempotent)
        # The add_messages reducer of LangGraph replaces messages with the same ID instead of 
        # appending them - even if the same report is injected twice, it will replace the original 
        # one instead of generating duplicate messages.
        injections.append(
            AIMessage(
                content=_format_sub_agent_result(tid, out),
                id=f"collect-result-{tid}",
            )
        )

        # Log which sub-agent (id/name) and task (id/name) result is being injected into the orchestrator context
        task_name = out.get("task_name", "N/A")
        logger.info(
            "inject_sub_agent_result",
            sub_agent=out.get("sub_agent", "N/A"),
            sub_agent_id=out.get("sub_agent_id", "N/A"),
            sub_agent_name=out.get("sub_agent_name", "N/A"),
            task_id=tid,
            task_name=task_name,
            status=out.get("status", "unknown"),
            artifacts_count=len(out.get("artifacts") or []),
            elapsed_seconds=out.get("elapsed_seconds", 0),
            summary_preview=(out.get("result_summary") or "")[:200],
        )

    return injections


def _collect_sub_agent_results(state: OrchestrationState) -> dict:
    """Collect barrier for a fanout round.

    Clears the round tasks and decrements the active sub-agent counter
    (operator.add); when the counter reaches zero, all sub-agents are done
    and the accumulated results are injected into the parent messages so the
    awakened orchestrator sees them.

    Returns:
        State updates dict (round tasks reset, counter decrement, and
        optionally the injected result messages).
    """
    tasks = state.get("sub_agent_round_tasks") or []

    logger.debug(
        "collect_sub_agent_results",
        task_count=len(tasks),
        counter_before=state.get("active_sub_agent_count", "N/A"),
    )

    updates = {
        "sub_agent_round_tasks": [],
        "active_sub_agent_count": -len(tasks),
    }
    
    # When all the child agents have completed their tasks, 
    # the summary results are injected into the parent messages 
    # (when the orchestrator is awakened, the completion signals 
    # and results can be seen, and it is no longer mistakenly believed 
    # that the task is still executing asynchronously).
    if state.get("active_sub_agent_count", 0) - len(tasks) <= 0:
        injections = _build_result_injections(state)
        if injections:
            logger.info("collect_inject_sub_agent_results", count=len(injections))
            updates["messages"] = injections

    return updates


def _wrap_subgraph(graph):
    """Wrap a compiled sub-graph so its output never pollutes the parent state.

    LangGraph applies a sub-graph's final state (its node output) to the parent
    state for every key that exists in both schemas. ``compaction_checkpoint``
    is a per-sub-agent incremental-compaction cursor that is meaningless to the
    parent; worse, two parallel sub-agents finishing in the same superstep each
    write a value for that key, raising ``InvalidUpdateError`` (one value per
    step limit on keys without a reducer). Stripping it here keeps the parent
    state clean and prevents the crash.
    """

    async def run(state, config=None):
        result = await graph.ainvoke(state, config=config)
        result.pop("compaction_checkpoint", None)
        return result

    return run


def _route_after_collect(state: OrchestrationState) -> str:
    """Route after the collect barrier.

    If the active sub-agent counter is still positive, more sub-agents are
    still running in this fanout round, so the chain ends and waits for the
    remaining branches; otherwise all results are in and the orchestrator is
    awakened.

    Returns:
        "orchestrator" when all sub-agents are done, else END.
    """
    if state.get("active_sub_agent_count", 0) > 0:
        logger.debug("route_after_collect", counter=state["active_sub_agent_count"], decision=END)
        return END

    logger.debug("route_after_collect", counter=state["active_sub_agent_count"], decision="orchestrator")
    return "orchestrator"


def build_graph():
    """Build and compile the orchestrator state graph.

    Nodes: orchestrator (LLM), tools, interrupt (HITL placeholder),
    collect_sub_agent_results (fanout barrier), and one wrapped sub-graph
    per sub-agent type (programmer / researcher / reviewer). Conditional
    routing: orchestrator ↔ tools loop, fanout via Send, collect barrier
    back to orchestrator, and forced termination on end_orchestration.

    Returns:
        The compiled LangGraph ready for execution.
    """
    builder = StateGraph(OrchestrationState)

    orchestrator_node = make_orchestrator_node()
    interrupt_node = make_interrupt_node()

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("tools", ToolNode(list(ORCHESTRATOR_TOOLS)))
    builder.add_node("interrupt", interrupt_node)
    builder.add_node("collect_sub_agent_results", _collect_sub_agent_results)

    SUBAGENT_MAP = {
        "programmer": PROGRAMMER_GRAPH,
        "researcher": RESEARCHER_GRAPH,
        "reviewer": REVIEWER_GRAPH,
    }
    for name, graph in SUBAGENT_MAP.items():
        builder.add_node(name, _wrap_subgraph(graph))

    builder.add_edge(START, "orchestrator")
    builder.add_edge("interrupt", "orchestrator")

    for name in SUBAGENT_MAP:
        builder.add_edge(name, "collect_sub_agent_results")

    builder.add_conditional_edges(
        "collect_sub_agent_results",
        _route_after_collect,
        {
            "orchestrator": "orchestrator",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {
            "tools": "tools",
            "interrupt": "interrupt",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "tools",
        _route_after_tools,
        {
            "orchestrator": "orchestrator",
            END: END,
        },
    )

    return builder.compile()
