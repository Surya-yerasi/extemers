import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from docqa.domain.answering import NOT_FOUND
from docqa.domain.metrics_glossary import GLOSSARY
from docqa.domain.retrieval import Strategy
from docqa.pipelines.chat import ChatService, Turn, TurnFailedError
from docqa.pipelines.metrics import compute_live_metrics, since_for
from docqa.ports import Generation
from tests.fakes import FakeGenerator, FakeSearcher, MemoryBlobStore, chat_service, retrieved

STATIC = Path(__file__).resolve().parents[2] / "src" / "docqa" / "web" / "static"


class Failing(FakeGenerator):
    def generate(
        self, system: str, prompt: str, max_tokens: int, json_mode: bool = False
    ) -> Generation:
        raise RuntimeError("throttled")


def traffic() -> tuple[ChatService, list[Turn]]:
    blobs = MemoryBlobStore()
    searcher = FakeSearcher(
        dense=[retrieved("a", "GPA 3.86", doc_id="t"), retrieved("b", "BSc", doc_id="d")],
        bm25=[retrieved("a", "GPA 3.86", doc_id="t")],
    )
    chat = chat_service(FakeGenerator("GPA 3.86 [1]."), searcher, blobs)
    conv, t1 = chat.ask("u1", "What was my GPA?", Strategy.HYBRID)
    chat.ask("u1", "And my master's?", Strategy.HYBRID, conv.conversation_id)  # a rewrite
    _, t3 = chat.ask("u1", "gpa", Strategy.BM25)
    chat.set_feedback("u1", t1.turn_id, "up")
    chat.set_feedback("u1", t3.turn_id, "down")
    nf = chat_service(FakeGenerator(NOT_FOUND), searcher, blobs)
    nf.ask("u1", "passport?", Strategy.DENSE)
    with pytest.raises(TurnFailedError):
        chat_service(Failing(), searcher, blobs).ask("u1", "boom", Strategy.DENSE)
    return chat, chat.turns("u1")


def test_kpis_and_rates() -> None:
    _, turns = traffic()
    m = compute_live_metrics(turns, "all", None)
    k = m.kpis
    assert k["questions"] == 5
    assert k["conversations"] == 4
    assert k["answer_rate"] == pytest.approx(3 / 5)
    assert k["not_found_rate"] == pytest.approx(1 / 5)
    assert k["error_rate"] == pytest.approx(1 / 5)
    assert k["helpful_rate"] == pytest.approx(1 / 2)
    assert k["feedback_coverage"] == pytest.approx(2 / 3)
    assert k["latency_p95"] is not None
    assert k["cost_per_question"] is not None
    assert k["cost_per_question"] > 0


def test_stages_tokens_and_signals() -> None:
    _, turns = traffic()
    for turn in turns:  # fake models take ~0 ms; give every step a measurable duration
        for span in turn.trace.spans:
            span.duration_ms = 10.0
    m = compute_live_metrics(turns, "all", None)
    shares = [s.share for s in m.stages if s.depth == 0 and s.share is not None]
    assert sum(shares) == pytest.approx(1.0)
    assert {s.name for s in m.stages} >= {
        "embed",
        "dense_search",
        "bm25_search",
        "generate",
        "rewrite",
    }
    steps = {t.step: t for t in m.tokens_by_step}
    assert steps["generate"].input_per_question == 1000
    assert steps["rewrite"].questions == 1
    assert m.generation["cited_answer_rate"] == 1.0
    assert m.generation["model_calls"] == pytest.approx((2 + 1 + 1 + 1) / 4)  # rewrite counts
    assert m.retrieval["rewrite_rate"] == pytest.approx(1 / 5)
    assert m.retrieval["top1_similarity"] == pytest.approx(0.9)  # FakeSearcher dense score
    assert m.models == {"us.amazon.nova-micro-v1:0": 4}
    rows = {r.strategy: r for r in m.by_strategy}
    assert rows[Strategy.HYBRID].questions == 2
    assert rows[Strategy.DENSE].error_rate == pytest.approx(1 / 2)
    assert m.daily[0].questions == 5


def test_window_and_strategy_filters() -> None:
    _, turns = traffic()
    later = datetime.now(UTC) + timedelta(days=2)
    assert compute_live_metrics(turns, "24h", None, now=later).kpis["questions"] == 0
    assert compute_live_metrics(turns, "7d", None, now=later).kpis["questions"] == 5
    only = compute_live_metrics(turns, "all", Strategy.BM25)
    assert only.kpis["questions"] == 1
    assert only.strategy == "bm25"
    with pytest.raises(ValueError, match="window must be one of"):
        since_for("1y", later)


def test_empty_traffic_has_no_rates() -> None:
    m = compute_live_metrics([], "7d", None)
    assert m.kpis["questions"] == 0
    assert m.kpis["answer_rate"] is None
    assert m.stages == []
    assert m.by_strategy == []


def test_turns_since_skips_old_conversations() -> None:
    chat, turns = traffic()
    assert len(chat.turns("u1", datetime.now(UTC) + timedelta(seconds=5))) == 0
    assert len(chat.turns("u1", datetime.now(UTC) - timedelta(minutes=5))) == len(turns)


def test_feedback_rules() -> None:
    chat, turns = traffic()
    answered = next(t for t in turns if t.result and t.feedback == "up")
    assert chat.set_feedback("u1", answered.turn_id, None).feedback is None
    failed = next(t for t in turns if t.result is None)
    with pytest.raises(ValueError, match="cannot be rated"):
        chat.set_feedback("u1", failed.turn_id, "up")


def test_every_metric_on_the_page_is_explained() -> None:
    """The dashboard's "what is this?" must never be missing for a metric it shows."""
    _, turns = traffic()
    m = compute_live_metrics(turns, "all", None)
    shown = {
        k for k in m.kpis if k not in {"conversations", "total_cost", "latency_p99", "tail_ratio"}
    }
    shown |= set(m.retrieval) | set(m.generation)
    js = (STATIC / "metrics.js").read_text()
    shown |= set(re.findall(r'tile\("([a-z0-9_@]+)"', js))
    for array in ("RETRIEVAL_KEYS", "ANSWER_KEYS", "JUDGE_KEYS"):
        match = re.search(rf"const {array} = \[([^\]]+)\]", js)
        assert match, array
        shown |= set(re.findall(r'"([a-z0-9_@]+)"', match.group(1)))
    missing = sorted(shown - set(GLOSSARY))
    assert missing == []
