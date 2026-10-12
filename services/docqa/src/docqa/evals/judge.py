"""LLM-as-judge with RAGAS (plus a coherence rubric). Optional and slow (several model calls
per answer), so runs choose it explicitly. The judge talks to any OpenAI-compatible endpoint;
locally that is Ollama's /v1 API.

With an empty reference (live traffic has no answer key), context precision is judged
against the answer instead, and the metrics that need a reference score None.

RAGAS (and LangChain) are dev dependencies only, imported lazily: never in the Lambda image.
"""

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from docqa.domain.judging import COHERENCE_SYSTEM, build_coherence_prompt, parse_coherence

ALL_METRICS = (
    "faithfulness",  # are the answer's claims supported by the retrieved chunks?
    "answer_relevancy",  # does the answer address the question?
    "context_precision",  # are the useful chunks ranked first?
    "context_recall",  # do the chunks contain what the reference answer needs?
    "answer_correctness",  # does the answer agree with the reference answer?
    "coherence",  # is the answer clear and consistent? (rubric, 1-5 scaled to 0-1)
)
NEEDS_REFERENCE = frozenset({"context_recall", "answer_correctness"})


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
            "context_precision_unlabelled": rm.ContextPrecisionWithoutReference(llm=llm),
            "context_recall": rm.ContextRecall(llm=llm),
            "answer_correctness": rm.AnswerCorrectness(llm=llm, embeddings=embeddings),
        }
        self._metrics: dict[str, Any] = {name: available.get(name) for name in metrics}
        self._unlabelled_precision = available["context_precision_unlabelled"]
        self._client = client
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
        if not reference:
            inputs["context_precision"] = {
                "user_input": question, "response": answer, "retrieved_contexts": contexts
            }  # fmt: skip
        scores: dict[str, float | None] = {}
        for name, metric in self._metrics.items():
            if name in NEEDS_REFERENCE and not reference:
                scores[name] = None
                continue
            try:
                if name == "coherence":
                    scores[name] = await self._coherence(question, answer)
                    continue
                scorer = metric
                if name == "context_precision" and not reference:
                    scorer = self._unlabelled_precision
                result = await scorer.ascore(**inputs[name])
                scores[name] = float(result.value)
            except Exception:  # a judge failure scores None; it must not abort the run
                scores[name] = None
        return scores

    async def _coherence(self, question: str, answer: str) -> float | None:
        response = await self._client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": COHERENCE_SYSTEM},
                {"role": "user", "content": build_coherence_prompt(question, answer)},
            ],
            temperature=0,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        return parse_coherence(response.choices[0].message.content or "")
