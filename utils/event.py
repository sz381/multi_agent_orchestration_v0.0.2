import asyncio

from utils.logging import get_logger

logger = get_logger(__name__)


def push_event(
    event: str, 
    data: dict
) -> bool:
    return False


def push_stream(
    content: str, 
    sub_agent_id: str, 
    sub_agent_name: str
) -> bool:
    return False
