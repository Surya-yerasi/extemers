"""Conversations: IDs, titles and the follow-up rewrite prompt. Pure."""

import re
import secrets
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

MAX_TURNS = 50  # per conversation; start a new one after that
HISTORY_TURNS = 4  # earlier exchanges used to rewrite a follow-up question
HISTORY_ANSWER_CHARS = 500
TITLE_CHARS = 80

_CONVERSATION_ID = re.compile(r"^c_[0-9a-f]{16}$")
_TURN_ID = re.compile(r"^(c_[0-9a-f]{16})\.([1-9][0-9]{0,3})$")

REWRITE_SYSTEM = """You rewrite follow-up questions about the user's personal documents.
Given the conversation so far and a follow-up question, return the follow-up as one
standalone question that can be understood without the conversation.
Resolve words like "it", "that", "there" and "my master's one" using the conversation.
If the follow-up is already standalone, return it unchanged.
Return only the question: no answer, no explanation, no quotes."""


class Exchange(BaseModel):
    question: str
    answer: str


def new_conversation_id() -> str:
    return f"c_{secrets.token_hex(8)}"


def is_conversation_id(value: str) -> bool:
    return _CONVERSATION_ID.match(value) is not None


def turn_id(conversation_id: str, number: int) -> str:
    """Trace IDs embed their conversation, so a trace can be found from its ID alone."""
    return f"{conversation_id}.{number}"


def parse_turn_id(value: str) -> tuple[str, int] | None:
    match = _TURN_ID.match(value)
    return (match.group(1), int(match.group(2))) if match else None


def build_rewrite_prompt(history: Sequence[Exchange], question: str) -> str:
    lines = ["Conversation:"]
    for exchange in history[-HISTORY_TURNS:]:
        lines.append(f"User: {exchange.question}")
        lines.append(f"Assistant: {exchange.answer[:HISTORY_ANSWER_CHARS]}")
    lines += ["", f"Follow-up question: {question}", "", "Standalone question:"]
    return "\n".join(lines)


def parse_rewrite(raw: str, fallback: str, max_chars: int) -> str:
    """First non-empty line, unquoted. Falls back to the original on anything odd."""
    for line in raw.strip().splitlines():
        candidate = line.strip().strip("\"'").removeprefix("Standalone question:").strip()
        if candidate:
            return candidate if len(candidate) <= max_chars else fallback
    return fallback


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    turns: int


def make_title(question: str) -> str:
    title = " ".join(question.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"
