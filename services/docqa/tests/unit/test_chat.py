import pytest

from docqa.adapters.conversation_store import BlobConversationStore
from docqa.domain.conversation import ConversationSummary
from docqa.domain.retrieval import Strategy
from docqa.pipelines.chat import (
    ConversationFullError,
    ConversationNotFoundError,
    TurnFailedError,
)
from docqa.ports import Generation
from tests.fakes import FakeGenerator, MemoryBlobStore, chat_service


def test_first_question_skips_the_rewrite() -> None:
    generator = FakeGenerator()
    conversation, turn = chat_service(generator).ask("u1", "What was my GPA?", Strategy.HYBRID)
    assert generator.rewrite_prompts == []
    assert turn.turn_id == f"{conversation.conversation_id}.1"
    assert turn.result is not None
    assert turn.result.standalone_question == "What was my GPA?"
    assert turn.trace.attributes["strategy"] == "hybrid"


def test_follow_up_is_rewritten_with_history_and_searched_as_standalone() -> None:
    generator = FakeGenerator()
    chat = chat_service(generator)
    conversation, _ = chat.ask("u1", "What was my GPA?", Strategy.HYBRID)
    _, turn = chat.ask("u1", "And my master's?", Strategy.DENSE, conversation.conversation_id)
    assert turn.turn_id.endswith(".2")
    assert "User: What was my GPA?" in generator.rewrite_prompts[0]
    assert "Assistant: Your GPA was 3.86 [1]." in generator.rewrite_prompts[0]
    assert turn.result is not None
    assert turn.result.standalone_question == "What was my master's GPA?"
    assert generator.prompts[-1].endswith("Question: What was my master's GPA?")
    rewrite = turn.trace.spans[0]
    assert rewrite.name == "rewrite"
    assert rewrite.attributes["standalone_question"] == "What was my master's GPA?"
    assert turn.result.tokens["rewrite_input"] == 200


def test_failed_turns_are_saved_and_skipped_in_history() -> None:
    class Flaky(FakeGenerator):
        fail = True

        def generate(
            self, system: str, prompt: str, max_tokens: int, json_mode: bool = False
        ) -> Generation:
            if self.fail:
                raise RuntimeError("quota")
            return super().generate(system, prompt, max_tokens)

    generator = Flaky()
    chat = chat_service(generator)
    with pytest.raises(TurnFailedError) as failed:
        chat.ask("u1", "What was my GPA?", Strategy.DENSE)
    conversation = failed.value.conversation
    assert failed.value.turn.error == "RuntimeError: quota"
    assert isinstance(failed.value.__cause__, RuntimeError)
    stored = chat.get("u1", conversation.conversation_id)
    assert stored.turns[0].result is None
    assert stored.turns[0].trace.attributes["error"] == "RuntimeError: quota"
    assert stored.history() == []

    generator.fail = False
    _, turn = chat.ask("u1", "Try again", Strategy.DENSE, conversation.conversation_id)
    assert turn.turn_id.endswith(".2")
    assert generator.rewrite_prompts == []  # the failed turn is not history


def test_invalid_question_creates_nothing() -> None:
    blobs = MemoryBlobStore()
    with pytest.raises(ValueError, match="empty"):
        chat_service(blobs=blobs).ask("u1", "   ", Strategy.DENSE)
    assert blobs.objects == {}


def test_turn_lookup_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = chat_service()
    conversation, turn = chat.ask("u1", "q", Strategy.BM25)
    assert chat.turn("u1", turn.turn_id)[1].question == "q"
    for bad in (f"{conversation.conversation_id}.2", "nope", f"{conversation.conversation_id}"):
        with pytest.raises(ConversationNotFoundError):
            chat.turn("u1", bad)
    monkeypatch.setattr("docqa.pipelines.chat.MAX_TURNS", 1)
    with pytest.raises(ConversationFullError):
        chat.ask("u1", "again", Strategy.BM25, conversation.conversation_id)


def test_store_keeps_an_index_per_owner_newest_first() -> None:
    blobs = MemoryBlobStore()
    chat = chat_service(blobs=blobs)
    first, _ = chat.ask("u1", "first", Strategy.BM25)
    second, _ = chat.ask("u1", "second", Strategy.BM25)
    chat.ask("u2", "other user", Strategy.BM25)
    chat.ask("u1", "again", Strategy.BM25, first.conversation_id)  # first becomes newest
    assert [s.title for s in chat.list("u1")] == ["first", "second"]
    assert [s.turns for s in chat.list("u1")] == [2, 1]
    assert [s.title for s in chat.list("u2")] == ["other user"]
    assert all("u1" not in key for key in blobs.objects)  # owner IDs are hashed in keys

    chat.delete("u1", second.conversation_id)
    assert [s.title for s in chat.list("u1")] == ["first"]
    with pytest.raises(ConversationNotFoundError):
        chat.get("u1", second.conversation_id)


def test_store_rejects_malformed_ids() -> None:
    store = BlobConversationStore(MemoryBlobStore())
    assert store.get("u1", "../../etc/passwd") is None
    assert store.list("nobody") == []
    with pytest.raises(ValueError, match="invalid conversation id"):
        store.delete("u1", "../x")


def test_summary_type() -> None:
    conversation, _ = chat_service().ask("u1", "q", Strategy.BM25)
    assert isinstance(conversation.summary(), ConversationSummary)
