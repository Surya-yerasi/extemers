"""Retrieval strategies and result fusion. Pure: no AWS or storage imports."""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

RRF_K = 60  # the constant from Cormack et al. (2009); dampens the weight of top ranks


class Strategy(StrEnum):
    DENSE = "dense"  # embedding similarity only
    BM25 = "bm25"  # keyword (full-text) only
    HYBRID = "hybrid"  # dense + BM25, fused with reciprocal rank fusion
    HYBRID_RERANK = "hybrid_rerank"  # hybrid candidates re-ordered by a cross-encoder


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    page_start: int
    page_end: int
    source_key: str
    title: str = ""
    doc_type: str = ""
    # Score per stage, e.g. {"dense": 0.82, "bm25": 7.1, "rrf": 0.032, "rerank": 0.91}.
    scores: dict[str, float] = Field(default_factory=dict)
    # 1-based rank per stage, for the debug panel and for eval metrics.
    ranks: dict[str, int] = Field(default_factory=dict)


def ranked(chunks: Sequence[RetrievedChunk], stage: str) -> list[RetrievedChunk]:
    """Record each chunk's 1-based rank for a stage (input order is the ranking)."""
    return [c.model_copy(update={"ranks": {**c.ranks, stage: i}}) for i, c in enumerate(chunks, 1)]


def rrf_fuse(
    result_lists: Sequence[Sequence[RetrievedChunk]], limit: int, k: int = RRF_K
) -> list[RetrievedChunk]:
    """Reciprocal rank fusion: score = sum over lists of 1 / (k + rank).

    Uses ranks only, so scores on different scales (cosine vs BM25) need no normalising.
    Per-stage scores and ranks from every list are merged onto the fused chunk.
    """
    fused: dict[str, RetrievedChunk] = {}
    totals: dict[str, float] = {}
    for results in result_lists:
        for rank, chunk in enumerate(results, 1):
            totals[chunk.chunk_id] = totals.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            seen = fused.get(chunk.chunk_id)
            if seen is None:
                fused[chunk.chunk_id] = chunk
            else:
                fused[chunk.chunk_id] = seen.model_copy(
                    update={
                        "scores": {**seen.scores, **chunk.scores},
                        "ranks": {**seen.ranks, **chunk.ranks},
                    }
                )
    # Ties broken by chunk_id so results are deterministic.
    order = sorted(totals, key=lambda cid: (-totals[cid], cid))[:limit]
    return ranked(
        [
            fused[cid].model_copy(update={"scores": {**fused[cid].scores, "rrf": totals[cid]}})
            for cid in order
        ],
        "rrf",
    )
