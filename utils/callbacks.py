import structlog
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult, GenerationChunk, ChatGenerationChunk
from langchain_core.runnables import RunnableConfig

from utils.logging import get_logger

logger = get_logger(__name__)


class OrchestrationCallBack(AsyncCallbackHandler):

    def __init__(self):
        pass

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
        pass
        # try:
        #     logger.info(
        #         "LLM Start", 
        #         serialized=serialized, 
        #         prompts=prompts, 
        #         run_id=run_id, 
        #         parent_run_id=parent_run_id, 
        #         tags=tags, 
        #         metadata=metadata, 
        #         **kwargs
        #     )
        # except Exception:
        #     pass

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        pass
        # try:
        #     logger.info(
        #         "LLM End", 
        #         response=response, 
        #         run_id=run_id, 
        #         parent_run_id=parent_run_id, 
        #         tags=tags, 
        #         **kwargs
        #     )
        # except Exception:
        #     pass
    
    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        pass
        # try:
        #     logger.error(
        #         "LLM Error", 
        #         error=error, 
        #         run_id=run_id, 
        #         parent_run_id=parent_run_id, 
        #         tags=tags, 
        #         **kwargs
        #     )
        # except Exception:
        #     pass
    
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
        """Log tool start and push event to SSE bridge.

        Args:
            serialized: Serialized tool definition (contains ``name`` key).
            input_str: The tool input as a string.
            run_id: Unique ID of this run.
            parent_run_id: ID of the parent run, if any.
            tags: Arbitrary tags attached to this run.
            metadata: LangGraph node / trigger metadata.
            inputs: The tool input as a dict.

        Returns:
            None
        """
        
        try:
            pass
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
        pass
        # try:
        #     logger.info(
        #         "Tool End",
        #         output=repr(output)[:200],
        #         run_id=run_id,
        #         parent_run_id=parent_run_id,
        #         tags=tags,
        #     )
        # except Exception:
        #     pass

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs
    ) -> None:
        pass
        # try:
        #     logger.error(
        #         "Tool Error", 
        #         error=error, 
        #         run_id=run_id, 
        #         parent_run_id=parent_run_id, 
        #         tags=tags, 
        #         **kwargs
        #     )
        # except Exception:
        #     pass

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
        # try:
        #     logger.info(
        #         "LLM New Token", 
        #         token=token, 
        #         chunk=chunk, 
        #         run_id=run_id, 
        #         parent_run_id=parent_run_id, 
        #         tags=tags, 
        #         **kwargs
        #     )
        # except Exception:
        #     pass


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
