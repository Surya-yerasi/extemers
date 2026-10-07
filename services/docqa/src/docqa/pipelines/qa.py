"""Question answering: (rewrite a follow-up) → retrieve with the chosen strategy → generate a
cited answer. Every stage is a span in the turn's trace.

retrieve() is separate from ask() so the eval harness can score retrieval on its own,
without paying for generation.
"""

import secrets
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from docqa.domain.answering import (
    NOT_FOUND_MESSAGE,
    SYSTEM_PROMPT,
    Citation,
    build_user_prompt,
    parse_answer,
)
from docqa.domain.conversation import (
    REWRITE_SYSTEM,
    Exchange,
    build_rewrite_prompt,
    parse_rewrite,
)
from docqa.domain.costs import CostEstimate, rerank_cost, token_cost
from docqa.domain.retrieval import RetrievedChunk, Strategy, rrf_fuse
from docqa.domain.tracing import Trace, Tracer
from docqa.ports import ChunkSearcher, Embedder, Generator, Reranker

MAX_QUESTION_CHARS = 1000
REWRITE_MAX_TOKENS = 120
TRACE_TOP = 5  # chunks listed per retrieval span in the trace


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
    standalone_question: str = ""  # what was searched for (differs after a rewrite)
    trace: Trace | None = None


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # Titan does not bill per call, so an estimate is enough


def _top(chunks: Sequence[RetrievedChunk], stage: str) -> list[dict[str, Any]]:
    """Compact view of a ranked list for the trace: file, chunk, score at this stage."""
    return [
        {
            "chunk_id": c.chunk_id,
            "file": c.source_key.rsplit("/", 1)[-1],
            "score": c.scores.get(stage),
        }
        for c in chunks[:TRACE_TOP]
    ]


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

    def retrieve(
        self, question: str, strategy: Strategy, tracer: Tracer | None = None
    ) -> Retrieval:
        question = validate_question(question)
        tracer = tracer or Tracer(f"r_{secrets.token_hex(8)}")
        tokens: dict[str, int] = {}
        cost = CostEstimate()
        dense: list[RetrievedChunk] = []
        lexical: list[RetrievedChunk] = []

        if strategy != Strategy.BM25:
            tokens["embed_input"] = _estimate_tokens(question)
            embed_cost = token_cost(self._embedding_model_id, tokens["embed_input"])
            cost = cost.add(embed_cost)
            with tracer.span(
                "embed",
                model=self._embedding_model_id,
                input_tokens=tokens["embed_input"],
                cost_usd=embed_cost.usd,
            ):
                vector = self._embedder.embed([question])[0]
            with tracer.span("dense_search", limit=self._candidates) as span:
                dense = self._searcher.vector_search(vector, self._candidates)
                span.set(hits=len(dense), top=_top(dense, "dense"))

        if strategy != Strategy.DENSE:
            with tracer.span("bm25_search", limit=self._candidates) as span:
                lexical = self._searcher.text_search(question, self._candidates)
                span.set(hits=len(lexical), top=_top(lexical, "bm25"))

        if strategy == Strategy.DENSE:
            chunks = dense[: self._top_k]
        elif strategy == Strategy.BM25:
            chunks = lexical[: self._top_k]
        else:
            with tracer.span("fuse", method="rrf", k=60) as span:
                fused = rrf_fuse([dense, lexical], limit=self._candidates)
                span.set(candidates=len(fused), top=_top(fused, "rrf"))
            if strategy == Strategy.HYBRID:
                chunks = fused[: self._top_k]
            else:
                rerank_usd = rerank_cost(self._reranker.model_id) if fused else CostEstimate()
                cost = cost.add(rerank_usd)
                with tracer.span(
                    "rerank", model=self._reranker.model_id, cost_usd=rerank_usd.usd
                ) as span:
                    chunks = self._reranker.rerank(question, fused, self._top_k)
                    span.set(top=_top(chunks, "rerank"))

        return Retrieval(
            strategy=strategy,
            chunks=chunks,
            timings_ms=tracer.finish().timings_ms,
            tokens=tokens,
            cost=cost,
        )

    def ask(
        self,
        question: str,
        strategy: Strategy,
        history: Sequence[Exchange] = (),
        tracer: Tracer | None = None,
    ) -> AskResult:
        question = validate_question(question)
        tracer = tracer or Tracer(f"q_{secrets.token_hex(8)}")
        tracer.set(strategy=strategy.value)
        tokens: dict[str, int] = {}
        cost = CostEstimate()

        standalone = question
        if history:  # a follow-up: make it searchable on its own
            with tracer.span(
                "rewrite", model=self._generator.model_id, history_turns=len(history)
            ) as span:
                rewrite = self._generator.generate(
                    REWRITE_SYSTEM, build_rewrite_prompt(history, question), REWRITE_MAX_TOKENS
                )
                standalone = parse_rewrite(rewrite.text, question, MAX_QUESTION_CHARS)
                rewrite_cost = token_cost(
                    self._generator.model_id, rewrite.input_tokens, rewrite.output_tokens
                )
                span.set(
                    question=question,
                    standalone_question=standalone,
                    input_tokens=rewrite.input_tokens,
                    output_tokens=rewrite.output_tokens,
                    cost_usd=rewrite_cost.usd,
                )
            tokens["rewrite_input"] = rewrite.input_tokens
            tokens["rewrite_output"] = rewrite.output_tokens
            cost = cost.add(rewrite_cost)

        retrieval = self.retrieve(standalone, strategy, tracer)
        tokens.update(retrieval.tokens)
        cost = cost.add(retrieval.cost)

        if retrieval.chunks:
            with tracer.span(
                "generate", model=self._generator.model_id, sources=len(retrieval.chunks)
            ) as span:
                generation = self._generator.generate(
                    SYSTEM_PROMPT,
                    build_user_prompt(standalone, retrieval.chunks),
                    self._max_answer_tokens,
                )
                parsed = parse_answer(generation.text, retrieval.chunks)
                generate_cost = token_cost(
                    self._generator.model_id, generation.input_tokens, generation.output_tokens
                )
                span.set(
                    input_tokens=generation.input_tokens,
                    output_tokens=generation.output_tokens,
                    cost_usd=generate_cost.usd,
                    not_found=parsed.not_found,
                    citations=[c.number for c in parsed.citations],
                )
            tokens["generate_input"] = generation.input_tokens
            tokens["generate_output"] = generation.output_tokens
            cost = cost.add(generate_cost)
            answer, not_found, citations = parsed.text, parsed.not_found, parsed.citations
        else:  # nothing indexed or nothing matched: do not pay for a model call
            answer, not_found, citations = NOT_FOUND_MESSAGE, True, []

        models = {"embedding": self._embedding_model_id, "generation": self._generator.model_id}
        if strategy == Strategy.HYBRID_RERANK:
            models["rerank"] = self._reranker.model_id
        tracer.set(not_found=not_found, cost_usd=cost.usd)
        trace = tracer.finish()
        return AskResult(
            strategy=strategy,
            answer=answer,
            not_found=not_found,
            citations=citations,
            chunks=retrieval.chunks,
            timings_ms={**trace.timings_ms, "total": trace.duration_ms},
            tokens=tokens,
            cost=cost,
            models=models,
            standalone_question=standalone,
            trace=trace,
        )


def validate_question(question: str) -> str:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return question
