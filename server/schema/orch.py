"""Request/response models for orchestration HTTP routes."""

from pydantic import BaseModel, Field


class CreateOrchestrationRequest(BaseModel):
    """Body of POST /api/orchestrations.

    Attributes:
        user_query:       The user's request text (non-empty).
        conversation_id:  Multi-turn anchor for Phase 7 memory bucketing;
                          the router rejects blank values (required).
    """

    user_query: str = Field(..., min_length=1, max_length=20000)
    conversation_id: str | None = None
