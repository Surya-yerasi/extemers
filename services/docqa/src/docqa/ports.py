"""Interfaces the pipelines depend on. Adapters implement them; tests use in-memory fakes."""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from docqa.domain.models import Chunk, DocMetadata, ParsedDocument
from docqa.domain.retrieval import RetrievedChunk


class ModelUnavailableError(Exception):
    """A model provider cannot serve the request (not running, model missing). The message
    is safe to show to the signed-in user."""


class BlobStore(Protocol):
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str) -> list[str]: ...


class VisionTranscriber(Protocol):
    def transcribe(self, image: bytes, image_format: str) -> str:
        """Return the page content as Markdown (tables as Markdown tables)."""
        ...


class MetadataExtractor(Protocol):
    def extract(self, text: str) -> DocMetadata: ...


class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class ChunkIndex(Protocol):
    def upsert_document(
        self, doc: ParsedDocument, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> None: ...
    def delete_document(self, doc_id: str) -> None: ...
    def count(self) -> int: ...


class ChunkSearcher(Protocol):
    """Read-only search over one index table. Results are best-first."""

    def vector_search(self, vector: Sequence[float], limit: int) -> list[RetrievedChunk]: ...
    def text_search(self, query: str, limit: int) -> list[RetrievedChunk]: ...


class Reranker(Protocol):
    model_id: str

    def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        """Return the best top_n chunks, best-first, each with a "rerank" score."""
        ...


class Generation(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int


class Generator(Protocol):
    model_id: str

    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation: ...
