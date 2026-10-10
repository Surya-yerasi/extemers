"""Conversations: each question is a turn with its own trace, stored per user.

The turn ID is also the trace ID ("c_<16 hex>.<n>"), so a trace can be opened from its ID
alone, and every log line and metric for that question carries it.
"""

import builtins  # ChatService.list shadows the built-in inside the class
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from docqa.domain.conversation import (
    MAX_TURNS,
    ConversationSummary,
    Exchange,
    make_title,
    new_conversation_id,
    parse_turn_id,
    turn_id,
)
from docqa.domain.retrieval import Strategy
from docqa.domain.tracing import Trace, Tracer
from docqa.pipelines.qa import AskResult, QAService, validate_question
from docqa.ports import ConversationStore

Feedback = Literal["up", "down"]


class Turn(BaseModel):
    turn_id: str  # also the trace ID
    created_at: datetime
    question: str
    strategy: Strategy
    result: AskResult | None = None  # None when the turn failed
    error: str | None = None
    trace: Trace
    feedback: Feedback | None = None  # the user's thumbs up/down, if given
    feedback_at: datetime | None = None


class Conversation(BaseModel):
    conversation_id: str
    owner: str  # the Cognito sub (an opaque ID), never the email
    title: str
    created_at: datetime
    updated_at: datetime
    turns: list[Turn] = Field(default_factory=list)

    def summary(self) -> ConversationSummary:
        return ConversationSummary(
            conversation_id=self.conversation_id,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            turns=len(self.turns),
        )

    def history(self) -> list[Exchange]:
        return [
            Exchange(question=t.question, answer=t.result.answer)
            for t in self.turns
            if t.result is not None
        ]


class ConversationNotFoundError(Exception):
    """Unknown ID, or a conversation that belongs to someone else."""


class ConversationFullError(Exception):
    pass


class TurnFailedError(Exception):
    """The question failed after its turn was created; the turn (with its error trace) is
    saved, so the failure can be inspected in the Traces tab. The cause is chained."""

    def __init__(self, conversation: "Conversation", turn: "Turn") -> None:
        super().__init__(turn.error)
        self.conversation = conversation
        self.turn = turn


class ChatService:
    def __init__(self, qa: QAService, store: "ConversationStore[Conversation]") -> None:
        self._qa = qa
        self._store = store

    def ask(
        self,
        owner: str,
        question: str,
        strategy: Strategy,
        conversation_id: str | None = None,
        trace_attributes: dict[str, Any] | None = None,
    ) -> tuple[Conversation, Turn]:
        question = validate_question(question)  # invalid input never creates a turn
        now = datetime.now(UTC)
        if conversation_id:
            conversation = self.get(owner, conversation_id)
            if len(conversation.turns) >= MAX_TURNS:
                raise ConversationFullError(f"conversation has {MAX_TURNS} turns; start a new one")
        else:
            conversation = Conversation(
                conversation_id=new_conversation_id(),
                owner=owner,
                title=make_title(question),
                created_at=now,
                updated_at=now,
            )
        tid = turn_id(conversation.conversation_id, len(conversation.turns) + 1)
        tracer = Tracer(
            tid,
            {"conversation_id": conversation.conversation_id, **(trace_attributes or {})},
        )
        conversation.updated_at = now
        try:
            result = self._qa.ask(question, strategy, conversation.history(), tracer)
        except Exception as exc:
            tracer.set(error=f"{type(exc).__name__}: {exc}")
            failed = Turn(
                turn_id=tid,
                created_at=now,
                question=question,
                strategy=strategy,
                error=f"{type(exc).__name__}: {exc}",
                trace=tracer.finish(),
            )
            conversation.turns.append(failed)
            self._store.save(conversation)
            raise TurnFailedError(conversation, failed) from exc

        trace = result.trace or tracer.finish()
        turn = Turn(
            turn_id=tid,
            created_at=now,
            question=question,
            strategy=strategy,
            result=result.model_copy(update={"trace": None}),  # stored once, on the turn
            trace=trace,
        )
        conversation.turns.append(turn)
        self._store.save(conversation)
        return conversation, turn

    def list(self, owner: str) -> Sequence[ConversationSummary]:
        return self._store.list(owner)

    def get(self, owner: str, conversation_id: str) -> Conversation:
        conversation = self._store.get(owner, conversation_id)
        if conversation is None or conversation.owner != owner:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    def delete(self, owner: str, conversation_id: str) -> None:
        self.get(owner, conversation_id)  # 404 for unknown or foreign IDs
        self._store.delete(owner, conversation_id)

    def set_feedback(self, owner: str, trace_id: str, rating: Feedback | None) -> Turn:
        """Record (or clear, with None) the user's rating of one answer."""
        conversation, turn = self.turn(owner, trace_id)
        if turn.result is None:
            raise ValueError("a failed question cannot be rated")
        turn.feedback = rating
        turn.feedback_at = datetime.now(UTC) if rating else None
        self._store.save(conversation)
        return turn

    def turns(self, owner: str, since: datetime | None = None) -> builtins.list[Turn]:
        """Every turn of every conversation (newest conversations first), for metrics.
        One read per conversation: fine for one person's history; a production system
        would aggregate as it writes instead (see the metrics guide)."""
        found: builtins.list[Turn] = []
        for summary in self._store.list(owner):
            if since and summary.updated_at < since:
                continue  # no turn in it can be newer than its last update
            conversation = self._store.get(owner, summary.conversation_id)
            if conversation is not None:
                found.extend(
                    t for t in conversation.turns if since is None or t.created_at >= since
                )
        return found

    def turn(self, owner: str, trace_id: str) -> tuple[Conversation, Turn]:
        parsed = parse_turn_id(trace_id)
        if parsed is None:
            raise ConversationNotFoundError(trace_id)
        conversation = self.get(owner, parsed[0])
        number = parsed[1]
        if number > len(conversation.turns):
            raise ConversationNotFoundError(trace_id)
        return conversation, conversation.turns[number - 1]
