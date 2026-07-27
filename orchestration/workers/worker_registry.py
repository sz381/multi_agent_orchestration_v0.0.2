"""
Worker Graph Registry — one compiled sub-graph per sub-agent type.

Provides:
    PROGRAMMER_GRAPH  — full coding toolkit
    RESEARCHER_GRAPH  — web search + fetch + filesystem
    REVIEWER_GRAPH    — review code & content + verification

All compiled on import.
"""

from orchestration.workers.graph import build_react_agent
from orchestration.workers.state import (
    ProgrammerSubAgentState,
    ResearcherSubAgentState,
    ReviewerSubAgentState,
)
from orchestration.prompts.system_prompt_programmer import PROGRAMMER_SYSTEM_PROMPT
from orchestration.prompts.system_prompt_researcher import RESEARCHER_SYSTEM_PROMPT
from orchestration.prompts.system_prompt_reviewer import REVIEWER_SYSTEM_PROMPT
from orchestration.tools.bundles.programmer import PROGRAMMER_TOOLS
from orchestration.tools.bundles.researcher import RESEARCHER_TOOLS
from orchestration.tools.bundles.reviewer import REVIEWER_TOOLS


PROGRAMMER_GRAPH = build_react_agent(
    name="programmer",
    tools=PROGRAMMER_TOOLS,
    system_prompt=PROGRAMMER_SYSTEM_PROMPT,
    state_cls=ProgrammerSubAgentState,
)

RESEARCHER_GRAPH = build_react_agent(
    name="researcher",
    tools=RESEARCHER_TOOLS,
    system_prompt=RESEARCHER_SYSTEM_PROMPT,
    state_cls=ResearcherSubAgentState,
)

REVIEWER_GRAPH = build_react_agent(
    name="reviewer",
    tools=REVIEWER_TOOLS,
    system_prompt=REVIEWER_SYSTEM_PROMPT,
    state_cls=ReviewerSubAgentState,
)

__all__ = [
    "PROGRAMMER_GRAPH",
    "RESEARCHER_GRAPH",
    "REVIEWER_GRAPH",
]
