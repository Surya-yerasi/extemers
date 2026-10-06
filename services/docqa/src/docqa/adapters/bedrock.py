"""Bedrock adapters: vision transcription, metadata extraction, Titan embeddings,
answer generation and reranking.

Every call sets maxTokens explicitly (an unset value reserves the model maximum against the
quota) and uses adaptive retries for throttling.
"""

import json
from collections.abc import Sequence
from typing import Any

import boto3
from botocore.config import Config

from docqa.domain.extraction import METADATA_PROMPT, TRANSCRIBE_PROMPT, parse_metadata
from docqa.domain.models import DocMetadata
from docqa.domain.retrieval import RetrievedChunk, ranked
from docqa.ports import Generation

RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=120)


def bedrock_runtime() -> Any:
    return boto3.client("bedrock-runtime", config=RETRY_CONFIG)


def _first_text(response: dict[str, Any]) -> str:
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return str(block["text"])
    return ""


class BedrockVisionTranscriber:
    def __init__(self, model_id: str, client: Any = None, max_tokens: int = 4000) -> None:
        self._model_id = model_id
        self._client = client or bedrock_runtime()
        self._max_tokens = max_tokens

    def transcribe(self, image: bytes, image_format: str) -> str:
        response = self._client.converse(
            modelId=self._model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": image_format, "source": {"bytes": image}}},
                        {"text": TRANSCRIBE_PROMPT},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": self._max_tokens, "temperature": 0},
        )
        return _first_text(response).strip()


class BedrockMetadataExtractor:
    def __init__(self, model_id: str, client: Any = None, max_input_chars: int = 4000) -> None:
        self._model_id = model_id
        self._client = client or bedrock_runtime()
        self._max_input_chars = max_input_chars

    def extract(self, text: str) -> DocMetadata:
        response = self._client.converse(
            modelId=self._model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": METADATA_PROMPT.format(text=text[: self._max_input_chars])}
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        return parse_metadata(_first_text(response))


class TitanEmbedder:
    """Titan Text Embeddings V2: one text per request; 256, 512 or 1024 dimensions."""

    def __init__(self, model_id: str, dimensions: int = 1024, client: Any = None) -> None:
        if dimensions not in (256, 512, 1024):
            raise ValueError("Titan V2 supports 256, 512 or 1024 dimensions")
        self._model_id = model_id
        self._dimensions = dimensions
        self._client = client or bedrock_runtime()

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps(
                    {"inputText": text, "dimensions": self._dimensions, "normalize": True}
                ),
            )
            vectors.append(json.loads(response["body"].read())["embedding"])
        return vectors


class BedrockGenerator:
    """Answer generation through the Converse API (any Bedrock text model)."""

    def __init__(self, model_id: str, client: Any = None) -> None:
        self.model_id = model_id
        self._client = client or bedrock_runtime()

    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
        usage = response.get("usage", {})
        return Generation(
            text=_first_text(response).strip(),
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
        )


class CohereReranker:
    """Cohere Rerank 3.5 on Bedrock (InvokeModel). A cross-encoder: reads the query and
    each chunk together, so it is slower and more accurate than embedding similarity."""

    def __init__(self, model_id: str, client: Any = None) -> None:
        self.model_id = model_id
        self._client = client or bedrock_runtime()

    def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        response = self._client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(
                {
                    "query": query,
                    "documents": [c.text for c in chunks],
                    "top_n": min(top_n, len(chunks)),
                    "api_version": 2,
                }
            ),
        )
        results = json.loads(response["body"].read())["results"]
        return ranked(
            [
                chunks[r["index"]].model_copy(
                    update={
                        "scores": {
                            **chunks[r["index"]].scores,
                            "rerank": round(float(r["relevance_score"]), 6),
                        }
                    }
                )
                for r in results
            ],
            "rerank",
        )
