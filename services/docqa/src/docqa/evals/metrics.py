"""Deterministic metrics: retrieval (IR) and answer checks. Pure, no model calls.

Retrieval metrics count only answerable questions. A chunk is relevant when it comes from an
evidence document and contains the evidence text. Gains count each evidence item once, so a
question needing two facts scores full nDCG only when both facts are retrieved.
"""

import math
import re
from collections.abc import Sequence

from docqa.domain.retrieval import RetrievedChunk
from docqa.domain.stats import mean, percentile
from docqa.evals.dataset import Evidence, GoldenItem
from docqa.pipelines.qa import AskResult

__all__ = ["mean", "percentile"]  # re-exported for the runner and older imports

KS = (1, 3, 5)
_MARKDOWN = re.compile(r"[#*_`>]")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, drop Markdown emphasis/heading marks, collapse whitespace (PDF line wraps)."""
    return _SPACE.sub(" ", _MARKDOWN.sub(" ", text.lower())).strip()


def file_name(source_key: str) -> str:
    return source_key.rsplit("/", 1)[-1]


def chunk_matches(evidence: Evidence, chunk: RetrievedChunk) -> bool:
    return file_name(chunk.source_key) in evidence.docs and normalize(evidence.text) in normalize(
        chunk.text
    )


def retrieval_scores(item: GoldenItem, chunks: Sequence[RetrievedChunk]) -> dict[str, float]:
    """hit@k, recall@k, precision@k, nDCG@k for k in KS, and MRR, over best-first chunks."""
    covered: set[int] = set()
    gains: list[float] = []  # 1.0 when the chunk covers a not-yet-covered evidence item
    first_relevant: int | None = None
    for rank, chunk in enumerate(chunks, 1):
        matched = {i for i, ev in enumerate(item.evidence) if chunk_matches(ev, chunk)}
        if matched and first_relevant is None:
            first_relevant = rank
        new = matched - covered
        covered |= matched
        gains.append(1.0 if new else 0.0)

    scores: dict[str, float] = {"mrr": 1.0 / first_relevant if first_relevant else 0.0}
    needed = len(item.evidence)
    for k in KS:
        covered_k: set[int] = set()
        for chunk in chunks[:k]:
            covered_k |= {i for i, ev in enumerate(item.evidence) if chunk_matches(ev, chunk)}
        relevant_k = sum(1 for c in chunks[:k] if any(chunk_matches(ev, c) for ev in item.evidence))
        scores[f"precision@{k}"] = relevant_k / k
        scores[f"hit@{k}"] = 1.0 if first_relevant and first_relevant <= k else 0.0
        scores[f"recall@{k}"] = len(covered_k) / needed
        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))
        ideal = sum(1 / math.log2(i + 2) for i in range(min(needed, k)))
        scores[f"ndcg@{k}"] = dcg / ideal
    return scores


def contains(answer: str, expected: str) -> bool:
    """Whole-token match. Case-insensitive, except short strings like the grade "A", which
    would otherwise match the article "a". Commas are ignored so "$4,000" equals "$4000"."""
    haystack = answer.replace(",", "")
    needle = expected.replace(",", "")
    flags = 0 if len(needle) <= 2 else re.IGNORECASE
    pattern = rf"(?<![\w+-]){re.escape(needle)}(?![\w+-])"
    return re.search(pattern, haystack, flags) is not None


def answer_scores(item: GoldenItem, result: AskResult) -> dict[str, float | None]:
    """correct, refusal_correct, citation_hit, citation_precision (None when not applicable)."""
    if not item.answerable:
        return {
            "correct": float(result.not_found),
            "refusal_correct": float(result.not_found),
            "citation_hit": None,
            "citation_precision": None,
        }
    has_all = all(
        any(contains(result.answer, alt) for alt in ([e] if isinstance(e, str) else e))
        for e in item.must_contain
    )
    has_forbidden = any(contains(result.answer, x) for x in item.must_not_contain)
    evidence_docs = {doc for ev in item.evidence for doc in ev.docs}
    cited = [file_name(c.source_key) for c in result.citations]
    good = sum(1 for doc in cited if doc in evidence_docs)
    return {
        "correct": float(not result.not_found and has_all and not has_forbidden),
        "refusal_correct": float(not result.not_found),
        "citation_hit": float(good > 0),
        "citation_precision": good / len(cited) if cited else None,
    }
