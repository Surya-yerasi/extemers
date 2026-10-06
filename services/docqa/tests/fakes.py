"""In-memory implementations of the ports, for pipeline tests."""

from collections.abc import Sequence

from docqa.domain.models import Chunk, DocMetadata, ParsedDocument


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
