""" ⚠️ Deprecated, this file is outdated, and will be deleted soon

Conversation summarization for long-running ReAct agent loops.

When message history grows beyond a threshold, the oldest messages
are compressed into a concise summary that preserves key context,
reducing O(n²) token growth to O(1).
"""

from langchain_core.messages import HumanMessage, SystemMessage

from utils.model import init_model, ainvoke_with_retry
from utils.settings import settings
from utils.logging import get_logger

logger = get_logger(__name__)

MAX_MESSAGES_BEFORE_SUMMARY = 30
KEEP_RECENT = 8

SUMMARY_SYSTEM_PROMPT = """You are a conversation summarizer for an AI agent system. Create a concise, structured summary of the conversation history.

Include ONLY information critical for continuing the task:
- Key decisions made and why
- Files created, modified, or inspected (with paths)
- Errors encountered and how they were resolved
- Current progress and what remains
- Any important context (config values, tool outputs, etc.)

Format as bullet points. Be brief — omit conversational fluff, greetings, and repeated information."""


def _build_summary_prompt(existing_summary: str, messages_to_compress: list) -> str:
    """Build the summarization prompt from messages to compress."""
    messages_text = []
    for m in messages_to_compress:
        content = str(m.content)
        if len(content) > 400:
            content = content[:400] + "..."
        messages_text.append(f"[{m.type}] {content}")

    history = "\n---\n".join(messages_text)

    if existing_summary:
        return (
            f"## Existing Summary\n{existing_summary}\n\n"
            f"## New Messages (extend the summary with these)\n{history}\n\n"
            f"Update the summary to incorporate the new messages. Keep total under 300 words."
        )
    else:
        return (
            f"## Conversation History\n{history}\n\n"
            f"Create a summary capturing key information. Keep under 300 words."
        )


async def maybe_summarize(
    messages: list,
    existing_summary: str = "",
    *,
    max_messages: int = MAX_MESSAGES_BEFORE_SUMMARY,
    keep_recent: int = KEEP_RECENT,
    config: dict | None = None,
) -> tuple[str, list]:
    """Conditionally compress old messages when history exceeds threshold.

    Args:
        messages:          Full message list (may include SystemMessage at [0])
        existing_summary:  Previous summary string for incremental update
        max_messages:      Trigger summarization when message count exceeds this
        keep_recent:       Number of most recent messages to keep verbatim

    Returns:
        (updated_summary, messages_for_llm) — summary string and reduced
        message list ready for the main LLM.
    """
    if len(messages) <= max_messages:
        return existing_summary, messages

    logger.info(
        "conversation_summarize_triggered",
        total_messages=len(messages),
        keep_recent=keep_recent,
    )

    # Identify messages to always preserve.
    sys_msg = next((m for m in messages if isinstance(m, SystemMessage)), None)
    first_human = next((m for m in messages if m.type == "human"), None)

    recent = messages[-keep_recent:]

    keep_ids = {m.id for m in recent}
    if sys_msg:
        keep_ids.add(sys_msg.id)
    if first_human:
        keep_ids.add(first_human.id)

    # ── Bidirectional AIMessage ↔ ToolMessage pairing protection ──────────
    # summarization splits the message list into "middle" (to compress) and
    # "recent" (to keep).  Without care, paired messages can land on opposite
    # sides of the boundary, breaking the OpenAI API invariant:
    #
    #   AIMessage(tool_calls)  ⇄  ToolMessage(tool_call_id)
    #
    # The two guards below cover both directions.

    # 1.  If the recent window *starts* with a ToolMessage, pull its paired
    #     AIMessage (with tool_calls) into keep_ids.
    if recent and recent[0].type == "tool":
        recent_start_idx = messages.index(recent[0])
        for m in reversed(messages[:recent_start_idx]):
            if m.type == "ai" and hasattr(m, "tool_calls") and m.tool_calls:
                keep_ids.add(m.id)
                break

    # 2.  For every AIMessage with tool_calls already in keep_ids, ensure
    #     **all** its paired ToolMessages are also preserved.  This prevents
    #     the "insufficient tool messages following tool_calls" 400 error.
    orphan_tool_call_ids: set[str] = set()
    for m in messages:
        if m.id not in keep_ids:
            continue
        if m.type == "ai" and hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id:
                    orphan_tool_call_ids.add(tc_id)

    if orphan_tool_call_ids:
        for m in messages:
            if m.type == "tool" and m.tool_call_id in orphan_tool_call_ids:
                keep_ids.add(m.id)

    middle = [m for m in messages if m.id not in keep_ids]

    if not middle:
        return existing_summary, messages

    # Call cheap summary model.
    prompt = _build_summary_prompt(existing_summary, middle)
    summary_model = init_model(
        model_name=settings.deepseek_model_name,
        temperature=0.0,
        max_tokens=512,
        streaming=False,
    )

    try:
        response = await ainvoke_with_retry(
            summary_model,
            [SystemMessage(content=SUMMARY_SYSTEM_PROMPT), HumanMessage(content=prompt)],
            config=config,
        )
        new_summary = str(response.content)
        logger.info(
            "conversation_summarized",
            compressed_count=len(middle),
            summary_length=len(new_summary),
        )
    except Exception as e:
        logger.error("conversation_summarize_failed", error=str(e))
        return existing_summary, messages  # fallback: use all messages with trim

    # Build result preserving original order.
    result = [m for m in messages if m.id in keep_ids]
    return new_summary, result
