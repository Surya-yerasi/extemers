"""Runs a golden set through every chosen strategy and aggregates the results."""

from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, Field

from docqa.domain.retrieval import RetrievedChunk, Strategy
from docqa.evals.dataset import GoldenItem
from docqa.evals.judge import Judge
from docqa.evals.metrics import KS, answer_scores, file_name, mean, percentile, retrieval_scores
from docqa.pipelines.qa import QAService

RETRIEVAL_STAGES = ("embed", "dense_search", "bm25_search", "fuse", "rerank")


class EvalRecord(BaseModel):
    item_id: str
    category: str
    strategy: Strategy
    answerable: bool
    retrieved: list[str] = Field(default_factory=list)  # "file#chunk_id", best-first
    retrieval: dict[str, float] = Field(default_factory=dict)  # answerable only
    answer: str | None = None
    answer_metrics: dict[str, float | None] = Field(default_factory=dict)
    judge: dict[str, float | None] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def retrieval_ms(self) -> float:
        return sum(self.timings_ms.get(stage, 0.0) for stage in RETRIEVAL_STAGES)


Progress = Callable[[int, int, EvalRecord], None]


def _label(chunk: RetrievedChunk) -> str:
    return f"{file_name(chunk.source_key)}#{chunk.chunk_id}"


def run_eval(  # noqa: PLR0913 - every option is a deliberate experiment knob
    qa: QAService,
    items: Sequence[GoldenItem],
    strategies: Sequence[Strategy],
    *,
    generate: bool = True,
    judge: Judge | None = None,
    progress: Progress | None = None,
) -> list[EvalRecord]:
    """Ask every item with every strategy. Errors are recorded per question, not raised.

    One untimed warm-up call per strategy first, so model loading (local) or a cold start
    does not land in the latency percentiles.
    """
    if items:
        for strategy in strategies:
            with suppress(Exception):  # the timed run reports any error properly
                (qa.ask if generate else qa.retrieve)(items[0].question, strategy)

    # Strategy-major order: consecutive prompts differ. Item-major order let a local model
    # reuse its cache when two strategies retrieved the same chunks, skewing latency.
    records: list[EvalRecord] = []
    total = len(items) * len(strategies)
    for strategy in strategies:
        for item in items:
            record = _evaluate(qa, item, strategy, generate=generate, judge=judge)
            records.append(record)
            if progress:
                progress(len(records), total, record)
    return records


def _evaluate(
    qa: QAService, item: GoldenItem, strategy: Strategy, *, generate: bool, judge: Judge | None
) -> EvalRecord:
    record = EvalRecord(
        item_id=item.id, category=item.category, strategy=strategy, answerable=item.answerable
    )
    try:
        if generate:
            result = qa.ask(item.question, strategy)
            chunks, timings, cost = result.chunks, result.timings_ms, result.cost.usd
            record.answer = result.answer
            record.answer_metrics = answer_scores(item, result)
        else:
            retrieval = qa.retrieve(item.question, strategy)
            chunks, timings, cost = retrieval.chunks, retrieval.timings_ms, retrieval.cost.usd
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        return record

    record.retrieved = [_label(c) for c in chunks]
    record.timings_ms = timings
    record.cost_usd = cost
    if item.answerable:
        record.retrieval = retrieval_scores(item, chunks)
        if judge and generate and record.answer is not None:
            record.judge = judge.score(
                item.question, record.answer, [c.text for c in chunks], item.answer
            )
    return record


class StrategySummary(BaseModel):
    strategy: Strategy
    questions: int
    errors: int
    retrieval: dict[str, float | None]
    answers: dict[str, float | None]
    judge: dict[str, float | None]
    latency_ms: dict[str, float | None]
    cost_usd: dict[str, float]
    by_category: dict[str, dict[str, float | None]]


def summarize(records: Sequence[EvalRecord]) -> list[StrategySummary]:
    summaries = []
    for strategy in dict.fromkeys(r.strategy for r in records):
        rows = [r for r in records if r.strategy == strategy]
        ok = [r for r in rows if r.error is None]
        answerable = [r for r in ok if r.answerable]
        unanswerable = [r for r in ok if not r.answerable]
        totals = [r.timings_ms["total"] for r in ok if "total" in r.timings_ms]
        retrieval_times = [r.retrieval_ms for r in ok]
        summaries.append(
            StrategySummary(
                strategy=strategy,
                questions=len(rows),
                errors=len(rows) - len(ok),
                retrieval={
                    key: mean([r.retrieval.get(key) for r in answerable])
                    for key in ["mrr", *(f"{m}@{k}" for m in ("hit", "recall", "ndcg") for k in KS)]
                },
                answers=_answer_summary(answerable, unanswerable),
                judge=_judge_summary(answerable),
                latency_ms={
                    "retrieval_p50": percentile(retrieval_times, 50),
                    "retrieval_p95": percentile(retrieval_times, 95),
                    "total_p50": percentile(totals, 50),
                    "total_p95": percentile(totals, 95),
                },
                cost_usd={
                    "total": sum(r.cost_usd for r in ok),
                    "per_question": sum(r.cost_usd for r in ok) / len(ok) if ok else 0.0,
                },
                by_category=_by_category(ok),
            )
        )
    return summaries


def _answer_summary(
    answerable: Sequence[EvalRecord], unanswerable: Sequence[EvalRecord]
) -> dict[str, float | None]:
    if not any(r.answer_metrics for r in [*answerable, *unanswerable]):
        return {}  # retrieval-only run

    def metric(rows: Sequence[EvalRecord], key: str) -> float | None:
        return mean([r.answer_metrics.get(key) for r in rows])

    return {
        "correct": metric([*answerable, *unanswerable], "correct"),
        "correct_answerable": metric(answerable, "correct"),
        "refusal_accuracy": metric(unanswerable, "refusal_correct"),  # said "not found"
        "false_refusal_rate": _complement(metric(answerable, "refusal_correct")),
        "citation_hit": metric(answerable, "citation_hit"),
        "citation_precision": metric(answerable, "citation_precision"),
    }


def _complement(value: float | None) -> float | None:
    return None if value is None else 1.0 - value


def _judge_summary(answerable: Sequence[EvalRecord]) -> dict[str, float | None]:
    names: dict[str, None] = {}
    for r in answerable:
        names.update(dict.fromkeys(r.judge))
    return {name: mean([r.judge.get(name) for r in answerable]) for name in names}


def _by_category(rows: Sequence[EvalRecord]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for category in sorted({r.category for r in rows}):
        in_category = [r for r in rows if r.category == category]
        stats: dict[str, Any] = {"questions": float(len(in_category))}
        if any(r.answerable for r in in_category):
            stats["hit@5"] = mean([r.retrieval.get("hit@5") for r in in_category])
        if any(r.answer_metrics for r in in_category):
            stats["correct"] = mean([r.answer_metrics.get("correct") for r in in_category])
        out[category] = stats
    return out
