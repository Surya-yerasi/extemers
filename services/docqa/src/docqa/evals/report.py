"""Markdown comparison report: one row per strategy, so variants are easy to compare."""

from collections.abc import Sequence

from docqa.evals.runner import StrategySummary


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def render_report(summaries: Sequence[StrategySummary], meta: dict[str, str]) -> str:
    lines = ["# docqa evaluation", ""]
    lines += [f"- **{key}:** {value}" for key, value in meta.items()]

    lines += ["", "## Retrieval (answerable questions)", ""]
    keys = ["hit@1", "hit@3", "hit@5", "recall@5", "mrr", "ndcg@5"]
    lines += _table(
        ["strategy", *keys],
        [[s.strategy, *(_fmt(s.retrieval.get(k)) for k in keys)] for s in summaries],
    )

    if any(s.answers for s in summaries):
        lines += ["", "## Answers", ""]
        keys = [
            "correct",
            "correct_answerable",
            "refusal_accuracy",
            "false_refusal_rate",
            "citation_hit",
            "citation_precision",
        ]
        lines += _table(
            ["strategy", *keys],
            [[s.strategy, *(_fmt(s.answers.get(k)) for k in keys)] for s in summaries],
        )

    judge_keys = list(dict.fromkeys(k for s in summaries for k in s.judge))
    if judge_keys:
        lines += ["", "## LLM judge (RAGAS, answerable questions)", ""]
        lines += _table(
            ["strategy", *judge_keys],
            [[s.strategy, *(_fmt(s.judge.get(k)) for k in judge_keys)] for s in summaries],
        )

    lines += ["", "## Latency and cost", ""]
    lines += _table(
        [
            "strategy",
            "retrieval P50 ms",
            "retrieval P95 ms",
            "total P50 ms",
            "total P95 ms",
            "$ / question",
            "$ total",
            "errors",
        ],
        [
            [
                s.strategy,
                _ms(s.latency_ms["retrieval_p50"]),
                _ms(s.latency_ms["retrieval_p95"]),
                _ms(s.latency_ms["total_p50"]),
                _ms(s.latency_ms["total_p95"]),
                f"{s.cost_usd['per_question']:.6f}",
                f"{s.cost_usd['total']:.4f}",
                str(s.errors),
            ]
            for s in summaries
        ],
    )

    categories = sorted({c for s in summaries for c in s.by_category})
    lines += ["", "## By category (hit@5 / correct)", ""]
    lines += _table(
        ["strategy", *categories],
        [
            [
                s.strategy,
                *(
                    f"{_fmt(s.by_category.get(c, {}).get('hit@5'), 2)} / "
                    f"{_fmt(s.by_category.get(c, {}).get('correct'), 2)}"
                    for c in categories
                ),
            ]
            for s in summaries
        ],
    )
    return "\n".join(lines) + "\n"
