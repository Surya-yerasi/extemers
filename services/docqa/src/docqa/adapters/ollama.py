"""Local models through Ollama's HTTP API (https://github.com/ollama/ollama/blob/main/docs/api.md).

Local mode costs $0 and keeps documents on the laptop. Model IDs reported to the rest of the
app carry a "local/" prefix, so cost estimates and the debug panel can tell them apart.
"""

import base64
from collections.abc import Sequence
from typing import Any

import httpx

from docqa.domain.costs import LOCAL_MODEL_PREFIX as LOCAL_PREFIX
from docqa.domain.extraction import (
    METADATA_PROMPT,
    TRANSCRIBE_PROMPT,
    clean_transcription,
    parse_metadata,
)
from docqa.domain.models import DocMetadata
from docqa.domain.retrieval import RetrievedChunk, ranked
from docqa.ports import Generation, ModelUnavailableError

CONTEXT_TOKENS = 8192  # Ollama's default window is smaller than 5 sources + instructions


class OllamaClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        # Generous timeout: the first call loads the model into memory.
        self._http = http or httpx.Client(timeout=300)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model", "")
        try:
            response = self._http.post(f"{self._base_url}{path}", json=payload)
        except httpx.ConnectError as exc:
            raise ModelUnavailableError(
                f"Ollama is not running at {self._base_url} (start it: brew services start ollama)"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise ModelUnavailableError(f"Ollama model {model} is missing (ollama pull {model})")
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        json_output: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": CONTEXT_TOKENS},
        }
        if json_output:
            payload["format"] = "json"
        return self.post("/api/chat", payload)


def _content(response: dict[str, Any]) -> str:
    return str(response.get("message", {}).get("content", "")).strip()


class OllamaEmbedder:
    def __init__(self, client: OllamaClient, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self.model_id = LOCAL_PREFIX + model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.post("/api/embed", {"model": self._model, "input": list(texts)})
        vectors: list[list[float]] = response["embeddings"]
        if vectors and len(vectors[0]) != self._dimensions:
            raise ValueError(
                f"{self._model} returns {len(vectors[0])} dimensions, "
                f"the index expects {self._dimensions}"
            )
        return vectors


class OllamaGenerator:
    def __init__(self, client: OllamaClient, model: str) -> None:
        self._client = client
        self._model = model
        self.model_id = LOCAL_PREFIX + model

    def generate(
        self, system: str, prompt: str, max_tokens: int, json_mode: bool = False
    ) -> Generation:
        response = self._client.chat(
            self._model,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens,
            json_output=json_mode,
        )
        return Generation(
            text=_content(response),
            input_tokens=int(response.get("prompt_eval_count", 0)),
            output_tokens=int(response.get("eval_count", 0)),
        )


class OllamaVisionTranscriber:
    def __init__(self, client: OllamaClient, model: str, max_tokens: int = 4000) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def transcribe(self, image: bytes, image_format: str) -> str:
        message = {
            "role": "user",
            "content": TRANSCRIBE_PROMPT,
            "images": [base64.b64encode(image).decode("ascii")],
        }
        return clean_transcription(
            _content(self._client.chat(self._model, [message], self._max_tokens))
        )


class OllamaMetadataExtractor:
    def __init__(self, client: OllamaClient, model: str, max_input_chars: int = 4000) -> None:
        self._client = client
        self._model = model
        self._max_input_chars = max_input_chars

    def extract(self, text: str) -> DocMetadata:
        prompt = METADATA_PROMPT.format(text=text[: self._max_input_chars])
        response = self._client.chat(
            self._model, [{"role": "user", "content": prompt}], 300, json_output=True
        )
        return parse_metadata(_content(response))


class PassthroughReranker:
    """No local reranker: keeps the fused order, so hybrid_rerank behaves like hybrid.
    The debug panel shows this model ID, so the comparison is never mistaken for a real one."""

    model_id = LOCAL_PREFIX + "no-rerank"

    def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        return ranked(list(chunks[:top_n]), "rerank")
