import structlog
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult, GenerationChunk, ChatGenerationChunk
from langchain_core.runnables import RunnableConfig

from utils.logging import get_logger

logger = get_logger(__name__)

# Identity keys shared between llm_node metadata and callback mapping.
_IDENTITY_KEYS = ("sub_agent_name", "sub_agent_id", "task_id", "task_name")

# Safety limit — clear all mapping dicts if any one exceeds this size.
# Prevents unbounded growth from leaked entries in long-running orchestrations.
_MAX_CTX_SIZE = 1000


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

                if not identity.get("sub_agent_id"):
                    return  # orchestrator LLM call — no sub-agent identity to track

                if any(identity.values()):
                    self._llm_ctx[str(run_id)] = identity
                    logger.info(
                        "LLM Start",
                        sub_agent_name=identity.get("sub_agent_name", ""),
                        sub_agent_id=identity.get("sub_agent_id", ""),
                        task_id=identity.get("task_id", ""),
                        prompt_count=len(prompts),
                        run_id=run_id,
                        tags=tags,
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
        """Extract tool_call_ids from LLM response and map them to identity."""
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
            token_usage = {}
            if response.llm_output and isinstance(response.llm_output, dict):
                token_usage = response.llm_output.get("token_usage", {})
            logger.info(
                "LLM End",
                sub_agent_name=identity.get("sub_agent_name", ""),
                sub_agent_id=identity.get("sub_agent_id", ""),
                task_id=identity.get("task_id", ""),
                has_tool_calls=has_tool_calls,
                token_usage=token_usage,
                run_id=run_id,
            )
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
        """Log tool start with resolved sub-agent identity."""
        ctx = self._resolve_identity(run_id, parent_run_id, **kwargs)
        try:
            self._tool_run_map[str(run_id)] = ctx
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
        """Clean up mapping dictionaries and log tool completion."""
        try:
            ctx = self._tool_run_map.pop(str(run_id), None) or {}
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
        pass

    def _resolve_identity(self, run_id: UUID, parent_run_id: UUID | None, **kwargs) -> dict[str, str]:
        """Resolve sub-agent identity for a tool event via three-level lookup.

        1. _tool_call_ctx[tool_call_id]  — main path (set by on_llm_end)
        2. structlog contextvars          — fallback (set by prepare_node)
        3. _llm_ctx[parent_run_id]        — last resort
        """
        # 1. tool_call_id lookup
        tc_id = kwargs.get("tool_call_id", "")
        if tc_id:
            ctx = self._tool_call_ctx.pop(tc_id, None)
            if ctx:
                return ctx

        # 2. structlog contextvars fallback
        try:
            cv = structlog.contextvars.get_contextvars()
            identity = {k: cv.get(k, "") for k in _IDENTITY_KEYS}
            if any(identity.values()):
                return identity
        except Exception:
            pass

        # 3. parent_run_id → _llm_ctx fallback (sub-agent may still be in llm_ctx)
        if parent_run_id:
            ctx = self._llm_ctx.get(str(parent_run_id))
            if ctx:
                return ctx

        # 4. orchestrator tools have no identity — expected, not an error.
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
