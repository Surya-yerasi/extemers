"""In-memory implementations of the ports, for pipeline tests."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from docqa.domain.conversation import REWRITE_SYSTEM
from docqa.domain.models import Chunk, DocMetadata, ParsedDocument
from docqa.domain.retrieval import RetrievedChunk, ranked
from docqa.ports import Generation

if TYPE_CHECKING:
    from docqa.pipelines.chat import ChatService


class MemoryBlobStore:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


class FakeVision:
    def __init__(self, text: str = "# Scanned page\n\nTranscribed text from an image.") -> None:
        self.text = text
        self.calls: list[str] = []

    def transcribe(self, image: bytes, image_format: str) -> str:
        self.calls.append(image_format)
        return self.text


class FakeMetadata:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str) -> DocMetadata:
        self.calls += 1
        return DocMetadata(doc_type="transcript", institution="Northfield State University")


class FakeEmbedder:
    """Deterministic 8-dim vectors derived from the text (no network)."""

    dimensions = 8

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[float((hash(t) >> i) & 1) for i in range(self.dimensions)] for t in texts]


class MemoryIndex:
    def __init__(self) -> None:
        self.docs: dict[str, list[Chunk]] = {}

    def upsert_document(
        self, doc: ParsedDocument, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> None:
        assert len(chunks) == len(vectors)
        self.docs[doc.doc_id] = list(chunks)

    def delete_document(self, doc_id: str) -> None:
        self.docs.pop(doc_id, None)

    def count(self) -> int:
        return sum(len(c) for c in self.docs.values())


def retrieved(chunk_id: str, text: str = "", doc_id: str = "d1", page: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text or f"text of {chunk_id}",
        page_start=page,
        page_end=page,
        source_key=f"raw/{doc_id}.pdf",
        title=f"Doc {doc_id}",
    )


class FakeSearcher:
    """Returns fixed best-first lists; records what it was asked."""

    def __init__(
        self, dense: list[RetrievedChunk] | None = None, bm25: list[RetrievedChunk] | None = None
    ) -> None:
        self.dense = ranked(
            [c.model_copy(update={"scores": {"dense": 0.9}}) for c in dense or []], "dense"
        )
        self.bm25 = ranked(
            [c.model_copy(update={"scores": {"bm25": 5.0}}) for c in bm25 or []], "bm25"
        )
        self.calls: list[str] = []

    def vector_search(self, vector: Sequence[float], limit: int) -> list[RetrievedChunk]:
        self.calls.append("dense")
        return self.dense[:limit]

    def text_search(self, query: str, limit: int) -> list[RetrievedChunk]:
        self.calls.append("bm25")
        return self.bm25[:limit]


class FakeGenerator:
    """Answers with `text`; answers follow-up rewrite requests with `rewrite`."""

    model_id = "us.amazon.nova-micro-v1:0"

    def __init__(
        self, text: str = "Your GPA was 3.86 [1].", rewrite: str = "What was my master's GPA?"
    ) -> None:
        self.text = text
        self.rewrite = rewrite
        self.prompts: list[str] = []
        self.rewrite_prompts: list[str] = []

    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        if system == REWRITE_SYSTEM:
            self.rewrite_prompts.append(prompt)
            return Generation(text=self.rewrite, input_tokens=200, output_tokens=12)
        self.prompts.append(prompt)
        return Generation(text=self.text, input_tokens=1000, output_tokens=100)


class FakeReranker:
    """Reverses the order: makes it obvious in tests that reranking happened."""

    model_id = "cohere.rerank-v3-5:0"

    def __init__(self) -> None:
        self.calls = 0

    def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        self.calls += 1
        out = [c.model_copy(update={"scores": {**c.scores, "rerank": 0.5}}) for c in chunks]
        return ranked(list(reversed(out))[:top_n], "rerank")


def chat_service(
    generator: FakeGenerator | None = None,
    searcher: FakeSearcher | None = None,
    blobs: MemoryBlobStore | None = None,
) -> "ChatService":
    from docqa.adapters.conversation_store import BlobConversationStore  # noqa: PLC0415
    from docqa.pipelines.chat import ChatService  # noqa: PLC0415
    from docqa.pipelines.qa import QAService  # noqa: PLC0415

    qa = QAService(
        searcher=searcher
        or FakeSearcher(dense=[retrieved("a", "GPA 3.86")], bm25=[retrieved("a", "GPA 3.86")]),
        embedder=FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=generator or FakeGenerator(),
        reranker=FakeReranker(),
    )
    return ChatService(qa, BlobConversationStore(blobs or MemoryBlobStore()))
