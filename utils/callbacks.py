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
                if any(identity.values()):
                    self._llm_ctx[str(run_id)] = identity
        except Exception:
            pass

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
            for gen_list in (response.generations if hasattr(response, "generations") else []):
                for gen in gen_list:
                    msg = getattr(gen, "message", None)
                    if not msg:
                        continue
                    for tc in (getattr(msg, "tool_calls", None) or []):
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if tc_id:
                            self._tool_call_ctx[tc_id] = identity
        except Exception:
            pass
    
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
            self._llm_ctx.pop(str(run_id), None)
        except Exception:
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

        # 3. parent_run_id fallback
        if parent_run_id:
            ctx = self._llm_ctx.get(str(parent_run_id))
            if ctx:
                return ctx

        return {}

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
        try:
            ctx = self._resolve_identity(run_id, parent_run_id, **kwargs)
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
            pass
        

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        """Clean up mapping dictionaries to prevent memory leak."""
        try:
            self._tool_run_map.pop(str(run_id), None)
            if parent_run_id:
                self._llm_ctx.pop(str(parent_run_id), None)
        except Exception:
            pass

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
