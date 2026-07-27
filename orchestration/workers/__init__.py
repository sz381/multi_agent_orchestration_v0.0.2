"""Workers package — sub-agent graph factory + registry."""

from orchestration.workers.graph import build_react_agent
from orchestration.workers.worker_registry import (
    PROGRAMMER_GRAPH,
    RESEARCHER_GRAPH,
    REVIEWER_GRAPH,
)

__all__ = [
    "PROGRAMMER_GRAPH",
    "RESEARCHER_GRAPH",
    "REVIEWER_GRAPH",
    "build_react_agent",
]
