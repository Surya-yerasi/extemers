"""LLM-as-judge with RAGAS. Optional and slow (several model calls per answer), so runs
choose it explicitly. The judge talks to any OpenAI-compatible endpoint; locally that is
Ollama's /v1 API.

RAGAS (and LangChain) are dev dependencies only, imported lazily: never in the Lambda image.
"""

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

ALL_METRICS = (
    "faithfulness",  # are the answer's claims supported by the retrieved chunks?
    "answer_relevancy",  # does the answer address the question?
    "context_precision",  # are the useful chunks ranked first?
    "context_recall",  # do the chunks contain what the reference answer needs?
    "answer_correctness",  # does the answer agree with the reference answer?
)


class Judge(Protocol):
    model_id: str

    def score(
        self, question: str, answer: str, contexts: Sequence[str], reference: str
    ) -> dict[str, float | None]: ...


class RagasJudge:
    def __init__(
        self,
        base_url: str,
        model: str,
        embedding_model: str,
        metrics: Sequence[str] = ALL_METRICS,
        api_key: str = "ollama",  # Ollama ignores the key; the client requires one
    ) -> None:
        unknown = set(metrics) - set(ALL_METRICS)
        if unknown:
            raise ValueError(f"unknown judge metrics: {', '.join(sorted(unknown))}")
        from openai import AsyncOpenAI  # noqa: PLC0415 - optional dependency
        from ragas.embeddings import OpenAIEmbeddings  # noqa: PLC0415
        from ragas.llms import llm_factory  # noqa: PLC0415
        from ragas.metrics import collections as rm  # noqa: PLC0415

        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        llm = llm_factory(model, provider="openai", client=client, temperature=0, max_tokens=1024)
        embeddings = OpenAIEmbeddings(client=client, model=embedding_model)
        available: dict[str, Any] = {
            "faithfulness": rm.Faithfulness(llm=llm),
            "answer_relevancy": rm.AnswerRelevancy(llm=llm, embeddings=embeddings),
            "context_precision": rm.ContextPrecision(llm=llm),
            "context_recall": rm.ContextRecall(llm=llm),
            "answer_correctness": rm.AnswerCorrectness(llm=llm, embeddings=embeddings),
        }
        self._metrics = {name: available[name] for name in metrics}
        self.model_id = model

    def score(
        self, question: str, answer: str, contexts: Sequence[str], reference: str
    ) -> dict[str, float | None]:
        return asyncio.run(self._score(question, answer, list(contexts), reference))

    async def _score(
        self, question: str, answer: str, contexts: list[str], reference: str
    ) -> dict[str, float | None]:
        inputs: dict[str, dict[str, Any]] = {
            "faithfulness": {
                "user_input": question, "response": answer, "retrieved_contexts": contexts
            },
            "answer_relevancy": {"user_input": question, "response": answer},
            "context_precision": {
                "user_input": question, "reference": reference, "retrieved_contexts": contexts
            },
            "context_recall": {
                "user_input": question, "retrieved_contexts": contexts, "reference": reference
            },
            "answer_correctness": {
                "user_input": question, "response": answer, "reference": reference
            },
        }  # fmt: skip
        scores: dict[str, float | None] = {}
        for name, metric in self._metrics.items():
            try:
                result = await metric.ascore(**inputs[name])
                scores[name] = float(result.value)
            except Exception:  # a judge failure scores None; it must not abort the run
                scores[name] = None
        return scores
