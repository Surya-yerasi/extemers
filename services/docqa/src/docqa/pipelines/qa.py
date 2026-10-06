"""Question answering: retrieve with the chosen strategy, then generate a cited answer.

retrieve() is separate from ask() so the eval harness can score retrieval on its own,
without paying for generation.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel, Field

from docqa.domain.answering import (
    NOT_FOUND_MESSAGE,
    SYSTEM_PROMPT,
    Citation,
    build_user_prompt,
    parse_answer,
)
from docqa.domain.costs import CostEstimate, rerank_cost, token_cost
from docqa.domain.retrieval import RetrievedChunk, Strategy, rrf_fuse
from docqa.ports import ChunkSearcher, Embedder, Generator, Reranker

MAX_QUESTION_CHARS = 1000


class Retrieval(BaseModel):
    strategy: Strategy
    chunks: list[RetrievedChunk]  # best-first, at most top_k
    timings_ms: dict[str, float] = Field(default_factory=dict)
    tokens: dict[str, int] = Field(default_factory=dict)
    cost: CostEstimate = Field(default_factory=CostEstimate)


class AskResult(BaseModel):
    strategy: Strategy
    answer: str
    not_found: bool
    citations: list[Citation]
    chunks: list[RetrievedChunk]  # what the model was given, in source-number order
    timings_ms: dict[str, float]
    tokens: dict[str, int]
    cost: CostEstimate
    models: dict[str, str]


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((time.perf_counter() - start) * 1000, 1)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # Titan does not bill per call, so an estimate is enough


class QAService:
    def __init__(  # noqa: PLR0913 - explicit dependencies, injected by wiring and tests
        self,
        *,
        searcher: ChunkSearcher,
        embedder: Embedder,
        embedding_model_id: str,
        generator: Generator,
        reranker: Reranker,
        candidates: int = 20,
        top_k: int = 5,
        max_answer_tokens: int = 600,
    ) -> None:
        self._searcher = searcher
        self._embedder = embedder
        self._embedding_model_id = embedding_model_id
        self._generator = generator
        self._reranker = reranker
        self._candidates = candidates
        self._top_k = top_k
        self._max_answer_tokens = max_answer_tokens

    def retrieve(self, question: str, strategy: Strategy) -> Retrieval:
        question = _validate(question)
        timings: dict[str, float] = {}
        tokens: dict[str, int] = {}
        cost = CostEstimate()
        dense: list[RetrievedChunk] = []
        lexical: list[RetrievedChunk] = []

        if strategy != Strategy.BM25:
            with _timed(timings, "embed"):
                vector = self._embedder.embed([question])[0]
            tokens["embed_input"] = _estimate_tokens(question)
            cost = cost.add(token_cost(self._embedding_model_id, tokens["embed_input"]))
            with _timed(timings, "dense_search"):
                dense = self._searcher.vector_search(vector, self._candidates)

        if strategy != Strategy.DENSE:
            with _timed(timings, "bm25_search"):
                lexical = self._searcher.text_search(question, self._candidates)

        if strategy == Strategy.DENSE:
            chunks = dense[: self._top_k]
        elif strategy == Strategy.BM25:
            chunks = lexical[: self._top_k]
        else:
            with _timed(timings, "fuse"):
                fused = rrf_fuse([dense, lexical], limit=self._candidates)
            if strategy == Strategy.HYBRID:
                chunks = fused[: self._top_k]
            else:
                with _timed(timings, "rerank"):
                    chunks = self._reranker.rerank(question, fused, self._top_k)
                if fused:
                    cost = cost.add(rerank_cost(self._reranker.model_id))

        return Retrieval(
            strategy=strategy, chunks=chunks, timings_ms=timings, tokens=tokens, cost=cost
        )

    def ask(self, question: str, strategy: Strategy) -> AskResult:
        start = time.perf_counter()
        retrieval = self.retrieve(question, strategy)
        timings = dict(retrieval.timings_ms)
        tokens = dict(retrieval.tokens)
        cost = retrieval.cost

        if retrieval.chunks:
            with _timed(timings, "generate"):
                generation = self._generator.generate(
                    SYSTEM_PROMPT,
                    build_user_prompt(question.strip(), retrieval.chunks),
                    self._max_answer_tokens,
                )
            tokens["generate_input"] = generation.input_tokens
            tokens["generate_output"] = generation.output_tokens
            cost = cost.add(
                token_cost(
                    self._generator.model_id, generation.input_tokens, generation.output_tokens
                )
            )
            parsed = parse_answer(generation.text, retrieval.chunks)
            answer, not_found, citations = parsed.text, parsed.not_found, parsed.citations
        else:  # nothing indexed or nothing matched: do not pay for a model call
            answer, not_found, citations = NOT_FOUND_MESSAGE, True, []

        timings["total"] = round((time.perf_counter() - start) * 1000, 1)
        models = {"embedding": self._embedding_model_id, "generation": self._generator.model_id}
        if strategy == Strategy.HYBRID_RERANK:
            models["rerank"] = self._reranker.model_id
        return AskResult(
            strategy=strategy,
            answer=answer,
            not_found=not_found,
            citations=citations,
            chunks=retrieval.chunks,
            timings_ms=timings,
            tokens=tokens,
            cost=cost,
            models=models,
        )


def _validate(question: str) -> str:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return question
