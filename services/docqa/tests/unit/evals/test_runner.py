import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from docqa import cli
from docqa.domain.retrieval import Strategy
from docqa.evals.dataset import Evidence, GoldenItem
from docqa.evals.judge import RagasJudge
from docqa.evals.report import render_report
from docqa.evals.runner import EvalRecord, run_eval, summarize
from docqa.pipelines.qa import QAService
from tests.fakes import FakeEmbedder, FakeGenerator, FakeReranker, FakeSearcher, retrieved

ITEMS = [
    GoldenItem(
        id="f1",
        category="fact",
        question="What GPA?",
        answer="3.86",
        must_contain=["3.86"],
        evidence=[Evidence(docs=["d1.pdf"], text="GPA 3.86")],
    ),
    GoldenItem(id="u1", category="unanswerable", question="Passport?", answer="none"),
]


def qa(generator: FakeGenerator | None = None) -> QAService:
    return QAService(
        searcher=FakeSearcher(
            dense=[retrieved("a", "GPA 3.86", doc_id="d1"), retrieved("b", "other", doc_id="d2")],
            bm25=[retrieved("b", "other", doc_id="d2")],
        ),
        embedder=FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=generator or FakeGenerator("GPA 3.86 [1]."),
        reranker=FakeReranker(),
    )


class RecordingJudge:
    model_id = "judge"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def score(
        self, question: str, answer: str, contexts: Sequence[str], reference: str
    ) -> dict[str, float | None]:
        self.calls.append(question)
        return {"faithfulness": 1.0, "answer_relevancy": None}


def test_run_eval_is_strategy_major_and_scores_everything() -> None:
    judge = RecordingJudge()
    seen: list[tuple[int, int, str]] = []
    records = run_eval(
        qa(),
        ITEMS,
        [Strategy.DENSE, Strategy.BM25],
        judge=judge,
        progress=lambda done, total, r: seen.append((done, total, r.item_id)),
    )
    assert [(r.strategy, r.item_id) for r in records] == [
        ("dense", "f1"), ("dense", "u1"), ("bm25", "f1"), ("bm25", "u1")
    ]  # fmt: skip
    assert seen[-1] == (4, 4, "u1")
    dense_f1 = records[0]
    assert dense_f1.retrieval["hit@1"] == 1.0
    assert dense_f1.answer_metrics["correct"] == 1.0
    assert dense_f1.retrieved[0] == "d1.pdf#a"
    assert dense_f1.judge == {"faithfulness": 1.0, "answer_relevancy": None}
    assert records[1].retrieval == {}  # unanswerable: no retrieval metrics
    assert judge.calls == ["What GPA?", "What GPA?"]  # answerable only, once per strategy
    assert records[2].retrieval["hit@5"] == 0.0  # bm25 only finds d2


def test_errors_are_recorded_not_raised() -> None:
    class Boom(FakeGenerator):
        def generate(  # type: ignore[no-untyped-def]
            self, system: str, prompt: str, max_tokens: int, json_mode: bool = False
        ):
            raise RuntimeError("quota")

    records = run_eval(qa(Boom()), ITEMS[:1], [Strategy.DENSE])
    assert records[0].error == "RuntimeError: quota"
    summary = summarize(records)[0]
    assert summary.errors == 1
    assert summary.retrieval["mrr"] is None


def test_retrieval_only_skips_generation_and_judge() -> None:
    generator, judge = FakeGenerator(), RecordingJudge()
    records = run_eval(qa(generator), ITEMS, [Strategy.HYBRID], generate=False, judge=judge)
    assert generator.prompts == []
    assert judge.calls == []
    assert records[0].answer is None
    # b is found by both retrievers, so RRF ranks it above a (the relevant chunk).
    assert records[0].retrieved == ["d2.pdf#b", "d1.pdf#a"]
    assert records[0].retrieval["mrr"] == 0.5
    assert summarize(records)[0].answers == {}


def test_summarize_and_report() -> None:
    records = run_eval(qa(), ITEMS, [Strategy.DENSE, Strategy.HYBRID], judge=RecordingJudge())
    summaries = summarize(records)
    dense = summaries[0]
    assert dense.questions == 2
    assert dense.retrieval["hit@1"] == 1.0
    assert dense.answers["correct"] == 0.5  # u1 was answered instead of refused
    assert dense.answers["refusal_accuracy"] == 0.0
    assert dense.answers["false_refusal_rate"] == 0.0
    assert dense.judge == {"faithfulness": 1.0, "answer_relevancy": None}
    assert dense.latency_ms["total_p95"] is not None
    assert dense.cost_usd["per_question"] > 0
    assert dense.by_category["fact"] == {"questions": 1.0, "hit@5": 1.0, "correct": 1.0}
    assert dense.by_category["unanswerable"] == {"questions": 1.0, "correct": 0.0}

    report = render_report(summaries, {"provider": "test"})
    assert "- **provider:** test" in report
    assert "| dense | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |" in report
    assert "## LLM judge" in report
    assert "| hybrid |" in report
    assert "## By category" in report


def test_report_without_answers_or_judge() -> None:
    records = run_eval(qa(), ITEMS, [Strategy.BM25], generate=False)
    report = render_report(summarize(records), {})
    assert "## Answers" not in report
    assert "## LLM judge" not in report


def test_ragas_judge_scores_and_tolerates_failures() -> None:
    judge = RagasJudge(
        "http://localhost:1/v1", "m", "e", metrics=["faithfulness", "context_recall"]
    )

    class Value:
        value = 0.75

    class Good:
        async def ascore(self, **kwargs: object) -> Value:
            assert set(kwargs) == {"user_input", "response", "retrieved_contexts"}
            return Value()

    class Broken:
        async def ascore(self, **kwargs: object) -> Value:
            raise ValueError("judge returned invalid JSON")

    judge._metrics = {"faithfulness": Good(), "context_recall": Broken()}
    assert judge.score("q", "a", ["ctx"], "ref") == {"faithfulness": 0.75, "context_recall": None}


def test_ragas_judge_rejects_unknown_metrics() -> None:
    with pytest.raises(ValueError, match="unknown judge metrics: vibes"):
        RagasJudge("http://x/v1", "m", "e", metrics=["vibes"])


def test_cli_eval_writes_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")
    monkeypatch.setattr(cli, "build_qa_service", lambda _s, _b: qa())
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text("".join(i.model_dump_json() + "\n" for i in ITEMS))
    out = tmp_path / "run"
    code = cli.main(["eval", "--dataset", str(dataset), "--strategies", "dense", "--out", str(out)])
    assert code == 0
    assert "# docqa evaluation" in capsys.readouterr().out
    records = [
        EvalRecord.model_validate_json(x) for x in (out / "records.jsonl").read_text().splitlines()
    ]
    assert [r.item_id for r in records] == ["f1", "u1"]
    assert json.loads((out / "summary.json").read_text())[0]["strategy"] == "dense"
    assert "dataset" in (out / "report.md").read_text()


def test_cli_eval_with_local_judge_and_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")
    monkeypatch.setattr(cli, "build_qa_service", lambda _s, _b: qa(FakeGenerator("nope")))
    built: list[str] = []

    class StubJudge(RecordingJudge):
        def __init__(self, base_url: str, model: str, embedding_model: str, metrics: list[str]):
            super().__init__()
            built.append(f"{base_url} {model} {embedding_model} {','.join(metrics)}")

    monkeypatch.setattr(cli, "RagasJudge", StubJudge)
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text("".join(i.model_dump_json() + "\n" for i in ITEMS))
    args = ["eval", "--dataset", str(dataset), "--judge", "local", "--limit", "1"]
    args += ["--judge-metrics", "faithfulness", "--out", str(tmp_path / "r")]
    assert cli.main(args) == 0
    assert built == ["http://localhost:11434/v1 qwen2.5:7b bge-m3 faithfulness"]
    assert "- **judge:** judge" in capsys.readouterr().out
