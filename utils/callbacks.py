"""Streaming-event collection layer bridging LLM/tool callbacks to the event bridge.

- on_llm_new_token        -> push_stream (token deltas)
- on_tool_start / on_tool_end -> tool_status / tool_output /
                                 file_changes / plan
- on_llm_end without tool calls -> worker_done (sub-agents only)

Terminal done/error events are owned by orch_manager and never emitted
here. Every push is fail-open: no-ops with debug logging outside HTTP
mode (CLI runs unchanged).
"""

import json
import structlog
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult, GenerationChunk, ChatGenerationChunk
from langchain_core.runnables import RunnableConfig

from utils.logging import get_logger
from utils.event import push_event, push_stream

logger = get_logger(__name__)

# Identity keys shared between llm_node metadata and callback mapping.
_IDENTITY_KEYS = ("sub_agent_name", "sub_agent_id", "task_id", "task_name")

# Safety limit — clear all mapping dicts if any one exceeds this size.
# Prevents unbounded growth from leaked entries in long-running orchestrations.
_MAX_CTX_SIZE = 1000

# Tools that produce file paths; their inputs carry the produced path.
_WRITE_TOOLS = ("write_file", "str_replace", "clean_dir")

# Plan-management tools; their outputs carry a full plan snapshot.
_PLAN_TOOLS = ("make_plan", "edit_plan", "delete_plan")


class OrchestrationCallBack(AsyncCallbackHandler):
    """Shared callback that resolves sub-agent identity via run-id mapping.

    Relay chain (no contextvars dependency for the main path):
        on_llm_start  →  _llm_ctx[run_id] = identity (from metadata)
        on_llm_end    →  _tool_call_ctx[tool_call_id] = _llm_ctx[run_id]
        on_tool_start →  identity = _tool_call_ctx[tool_call_id]  (pop)
                        →  _tool_run_map[run_id] = identity
        on_tool_end   →  pop _tool_run_map[run_id], pop _llm_ctx[parent_run_id]
    """

    def __init__(self):
        self._llm_ctx: dict[str, dict[str, str]] = {}
        self._tool_call_ctx: dict[str, dict[str, str]] = {}
        self._tool_run_map: dict[str, dict[str, str]] = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs
    ) -> None:
        """Register identity from metadata into _llm_ctx keyed by run_id."""
        try:
            if metadata:
                identity = {k: str(metadata.get(k, "")) for k in _IDENTITY_KEYS}

                # if we cannot get the sub_agent_id, then it means it's orchestator
                # register a fake identity for orchestrator for the token stat
                if not identity.get("sub_agent_id"):
                    identity = {
                        "sub_agent_name": "orchestrator",
                        "sub_agent_id": "orchestrator",
                        "task_id": "orchestrator",
                        "task_name": "orchestrator",
                    }

                if any(identity.values()):
                    identity["llm_role"] = str(metadata.get("llm_role", "agent"))
                    self._llm_ctx[str(run_id)] = identity
                    logger.info(
                        "LLM Start",
                        sub_agent_name=identity.get("sub_agent_name", ""),
                        sub_agent_id=identity.get("sub_agent_id", ""),
                        task_id=identity.get("task_id", ""),
                        prompt_count=len(prompts),
                        run_id=run_id,
                        tags=tags,
                        llm_role=identity["llm_role"],
                    )
            # Safety net: warn if dicts grow beyond limit, but do NOT clear
            # in-flight entries — clearing would break identity resolution for
            # concurrent sub-agents currently executing tool calls.
            if len(self._llm_ctx) > _MAX_CTX_SIZE:
                logger.warning(
                    "callback_ctx_size_exceeded",
                    llm_ctx=len(self._llm_ctx),
                    tool_call_ctx=len(self._tool_call_ctx),
                    tool_run_map=len(self._tool_run_map),
                )
        except Exception:
            logger.warning("callback_error", handler="on_llm_start", exc_info=True)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        """Map tool_call_ids to identity, then emit worker_done for tool-less ends.

        Tool-call ids are recorded so on_tool_start can resolve identity.
        An LLM end without tool calls closes the sub-agent's ReAct loop and
        emits worker_done (skipped for the orchestrator). Terminal
        done/error events are owned by orch_manager.

        Returns:
            None.
        """
        try:
            identity = self._llm_ctx.get(str(run_id))
            if not identity:
                return

            has_tool_calls = False
            for gen_list in (response.generations if hasattr(response, "generations") else []):
                for gen in gen_list:
                    msg = getattr(gen, "message", None)
                    if not msg:
                        continue
                    for tc in (getattr(msg, "tool_calls", None) or []):
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if tc_id:
                            has_tool_calls = True
                            self._tool_call_ctx[tc_id] = identity
                            
            # LLM call without tools → cleanup now (no on_tool_end will follow).
            if not has_tool_calls:
                self._llm_ctx.pop(str(run_id), None)
            # Log completion.
            logger.info(
                "LLM End",
                sub_agent_name=identity.get("sub_agent_name", ""),
                sub_agent_id=identity.get("sub_agent_id", ""),
                task_id=identity.get("task_id", ""),
                has_tool_calls=has_tool_calls,
                run_id=run_id,
                llm_role=identity.get("llm_role", "agent"),
            )

            # LLM call without tools ends the ReAct loop: emit worker_done
            # for the streaming layer. Terminal done/error events are owned
            # by orch_manager (published after the graph fully returns).
            if not has_tool_calls and identity.get("sub_agent_id") != "orchestrator":
                push_event("worker_done", {
                    "worker_done": {
                        "sub_agent_id": identity.get("sub_agent_id", ""),
                        "sub_agent_name": identity.get("sub_agent_name", ""),
                        "status": "done",
                        "token_used": _extract_token_used(response),
                    },
                    "sub_agent_id": identity.get("sub_agent_id", ""),
                    "sub_agent_name": identity.get("sub_agent_name", ""),
                })
        except Exception:
            logger.warning("callback_error", handler="on_llm_end", exc_info=True)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        """Clean up _llm_ctx on error to prevent memory leak."""
        try:
            logger.error(
                "LLM Error",
                error=str(error)[:300],
                run_id=run_id,
            )
            self._llm_ctx.pop(str(run_id), None)
        except Exception:
            pass
    
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs
    ) -> None:
        """Record the tool run and emit a tool_status(executing) event.

        Stores {ctx, name, file_path} for on_tool_end, logs the call with
        truncated input, and pushes a streaming tool_status event
        (fail-open outside HTTP mode).

        Returns:
            None.
        """
        ctx = self._resolve_identity(run_id, parent_run_id, **kwargs)
        try:
            self._tool_run_map[str(run_id)] = {
                "ctx": ctx,
                "name": serialized["name"],
                "file_path": _extract_file_path(serialized["name"], inputs),
            }
            truncated_input = input_str[:200] + ("...[truncated]" if len(input_str) > 200 else "")
            logger.info(
                "Tool Start",
                tool_name=serialized["name"],
                input_str=truncated_input,
                sub_agent_name=ctx.get("sub_agent_name", ""),
                sub_agent_id=ctx.get("sub_agent_id", ""),
                task_id=ctx.get("task_id", ""),
                task_name=ctx.get("task_name", ""),
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
            )
            push_event("tool_status", {
                "tool_status": {
                    "action": "executing",
                    "name": serialized["name"],
                    "params": _compact_params(inputs),
                    "tool_call_id": kwargs.get("tool_call_id"),
                },
                "sub_agent_id": ctx.get("sub_agent_id", ""),
                "sub_agent_name": ctx.get("sub_agent_name", ""),
            })
        except Exception:
            logger.warning("callback_error", handler="on_tool_start", exc_info=True)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        """Clean up mappings and emit tool_status/tool_output/file_changes/plan.

        Consumes the entry recorded by on_tool_start, logs the result with
        truncated output, and pushes the streaming events: tool_status
        (done), tool_output, file_changes (write tools), and plan (plan
        tools, when a plan snapshot is present). Fail-open outside HTTP
        mode.

        Returns:
            None.
        """
        try:
            entry = self._tool_run_map.pop(str(run_id), None) or {}
            ctx = entry.get("ctx") or {}
            tool_name = entry.get("name") or ""
            file_path = entry.get("file_path")
            if parent_run_id:
                self._llm_ctx.pop(str(parent_run_id), None)

            output_str = str(output)[:200] + ("...[truncated]" if len(str(output)) > 200 else "")
            logger.info(
                "Tool End",
                sub_agent_name=ctx.get("sub_agent_name", ""),
                sub_agent_id=ctx.get("sub_agent_id", ""),
                task_id=ctx.get("task_id", ""),
                task_name=ctx.get("task_name", ""),
                output=output_str,
                run_id=run_id,
            )

            # Streaming events for the HTTP layer (fail-open, no-ops in CLI).
            tool_call_id = kwargs.get("tool_call_id")
            push_event("tool_status", {
                "tool_status": {
                    "action": "done",
                    "name": tool_name,
                    "tool_call_id": tool_call_id,
                },
                "sub_agent_id": ctx.get("sub_agent_id", ""),
                "sub_agent_name": ctx.get("sub_agent_name", ""),
            })
            push_event("tool_output", {
                "tool_output": _output_text(output),
                "sub_agent_id": ctx.get("sub_agent_id", ""),
                "sub_agent_name": ctx.get("sub_agent_name", ""),
            })
            if file_path:
                push_event("file_changes", {"file_changes": [{"path": file_path}]})
            if tool_name in _PLAN_TOOLS:
                plan = _try_extract_plan(output)
                if plan is not None:
                    push_event("plan", {"plan": plan})
        except Exception:
            logger.warning("callback_error", handler="on_tool_end", exc_info=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        """Clean up mapping dictionaries on tool error."""
        try:
            logger.error(
                "Tool Error",
                error=str(error)[:300],
                run_id=run_id,
            )
            self._tool_run_map.pop(str(run_id), None)
            if parent_run_id:
                self._llm_ctx.pop(str(parent_run_id), None)
        except Exception:
            pass

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs,
    ) -> None:
        """Push each token delta to the orchestration channel (stream events).

        Identity comes from _llm_ctx (peek, not popped), falling back to
        _resolve_identity. Empty tokens or empty extracted text are
        skipped. Fail-open: push_stream no-ops outside HTTP mode.

        Returns:
            None.
        """
        # 空内容不传
        if not token:
            return
        try:
            identity = self._llm_ctx.get(str(run_id)) or self._resolve_identity(
                run_id, parent_run_id, **kwargs
            )
            content = _token_to_text(token)
            if not content:
                return
            # Fail-open: no-ops with debug logging outside HTTP mode.
            push_stream(
                content,
                identity.get("sub_agent_id") or "orchestrator",
                identity.get("sub_agent_name") or "orchestrator",
            )
        except Exception:
            logger.warning("callback_error", handler="on_llm_new_token", exc_info=True)

    def _resolve_identity(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        **kwargs
    ) -> dict[str, str]:
        """Resolve sub-agent identity for a tool event via three-level lookup.

        1. _tool_call_ctx[tool_call_id]  — main path (set by on_llm_end)
        2. structlog contextvars          — fallback (set by prepare_node)
        3. _llm_ctx[parent_run_id]        — last resort

        Args:
            run_id:        Id of the tool run.
            parent_run_id: Id of the parent LLM run, when nested.

        Returns:
            Identity dict (sub_agent_* / task_* / llm_role keys); an
            orchestrator placeholder when nothing resolves (expected for
            orchestrator tools, not an error).
        """
        # tool_call_id lookup
        tc_id = kwargs.get("tool_call_id", "")
        if tc_id:
            ctx = self._tool_call_ctx.pop(tc_id, None)
            if ctx:
                return ctx

        # structlog contextvars fallback
        try:
            cv = structlog.contextvars.get_contextvars()
            identity = {k: cv.get(k, "") for k in _IDENTITY_KEYS}
            if any(identity.values()):
                return identity
        except Exception:
            pass

        # parent_run_id → _llm_ctx fallback (sub-agent may still be in llm_ctx)
        if parent_run_id:
            ctx = self._llm_ctx.get(str(parent_run_id))
            if ctx:
                return ctx

        # orchestrator tools have no identity — expected, not an error.
        logger.warning(
            "callback_identity_unresolved",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
        )
        return {
            "sub_agent_name": "orchestrator",
            "sub_agent_id": "orchestrator",
            "task_id": "",
            "task_name": "",
        }


def _token_to_text(token: str | list[str | dict[str, Any]]) -> str:
    """Flatten a langchain token (str or chunk list) into plain text.

    Args:
        token: Raw token delta (str, or list of str/dict chunks).

    Returns:
        The concatenated plain text; "" for empty or unmappable tokens.
    """
    if isinstance(token, str):
        return token
    if isinstance(token, list):
        parts: list[str] = []
        for item in token:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(token) if token else ""


def _compact_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shrink tool inputs for SSE frames: 200 chars per value, 8 keys max.

    Args:
        params: Raw tool inputs dict.

    Returns:
        The compacted dict (values stringified), or None when params is
        empty. "_truncated": True marks truncated keys/values.
    """
    if not params:
        return None
    compact: dict[str, Any] = {}
    for i, (key, value) in enumerate(params.items()):
        if i >= 8:
            compact["_truncated"] = True
            break
        text = str(value)
        if len(text) > 200:
            text = text[:200] + f"...({len(text)} chars)"
            compact["_truncated"] = True
        compact[key] = text
    return compact


def _extract_file_path(name: str, inputs: dict[str, Any] | None) -> str | None:
    """Pull the produced file path from write-tool inputs.

    Args:
        name:   Tool name (must be in _WRITE_TOOLS).
        inputs: Tool inputs dict.

    Returns:
        The produced path as str, or None when unavailable or the tool
        is not a write tool.
    """
    if not inputs or name not in _WRITE_TOOLS:
        return None
    if name == "clean_dir":
        path = inputs.get("dir_path")
    else:
        path = inputs.get("file_path")
    return str(path) if path else None


def _try_extract_plan(output: Any) -> list[dict] | None:
    """Extract the plan snapshot from a plan-tool output.

    Output may be a plain JSON string, a dict, or an object with a
    content/update attribute (e.g. Command).

    Args:
        output: Raw tool output of any shape.

    Returns:
        The plan list of phases, or None when no valid plan is found.
    """
    candidates: list[Any] = []
    if isinstance(output, dict):
        candidates.append(output)
    elif isinstance(output, str):
        candidates.append(output)
    else:
        for attr in ("content", "update", "response"):
            value = getattr(output, attr, None)
            if isinstance(value, (dict, str)):
                candidates.append(value)
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(candidate, dict):
            plan = candidate.get("plan")
            if isinstance(plan, list):
                return plan
    return None


def _output_text(output: Any) -> str:
    """Render a tool output as display text (prefer .content over repr).

    Args:
        output: Raw tool output.

    Returns:
        Display text: the string itself, else .content, else str(output).
    """
    if isinstance(output, str):
        return output
    value = getattr(output, "content", None)
    if isinstance(value, str):
        return value
    return str(output)


def _extract_token_used(response: LLMResult) -> int | None:
    """Best-effort total token count from the LLM response metadata.

    Args:
        response: LLM result from on_llm_end.

    Returns:
        Total tokens as int, or None when the metadata is missing or
        malformed.
    """
    llm_output = getattr(response, "llm_output", None)
    if not isinstance(llm_output, dict):
        return None
    usage = llm_output.get("token_usage") or llm_output.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
    return None


def create_orchestration_config(
    base_config: RunnableConfig | None = None,
) -> RunnableConfig:
    """Create a RunnableConfig with OrchestrationCallBack injected.

    Merges callbacks from base_config if provided, and ensures
    OrchestrationCallBack is present (added only once if not already there).

    Args:
        base_config: optional existing config to merge with.

    Returns:
        RunnableConfig ready for graph.ainvoke / graph.astream.
    """
    config: RunnableConfig = dict(base_config) if base_config else {}

    callbacks = list(config.get("callbacks", []))
    if not any(isinstance(cb, OrchestrationCallBack) for cb in callbacks):
        callbacks.append(OrchestrationCallBack())
    config["callbacks"] = callbacks

    return config
