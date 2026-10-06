import json
from dataclasses import dataclass, field

import pytest

from docqa.domain.models import doc_id_for
from docqa.pipelines.ingest import IngestService, Outcome, content_type_for, parsed_key
from tests.conftest_samples import sample
from tests.fakes import FakeEmbedder, FakeMetadata, FakeVision, MemoryBlobStore, MemoryIndex


def _corpus() -> MemoryBlobStore:
    return MemoryBlobStore(
        {
            "raw/transcript.pdf": sample("transcript_northfield_state.pdf"),
            "raw/award.pdf": sample("scholarship_award_letter_scanned.pdf"),
            "raw/card.png": sample("acp_membership_card.png"),
            "raw/notes.docx": b"unsupported",
        }
    )


@dataclass
class Parts:
    blobs: MemoryBlobStore = field(default_factory=_corpus)
    vision: FakeVision = field(default_factory=FakeVision)
    metadata: FakeMetadata = field(default_factory=FakeMetadata)
    embedder: FakeEmbedder = field(default_factory=FakeEmbedder)
    index: MemoryIndex = field(default_factory=MemoryIndex)


@pytest.fixture
def parts() -> Parts:
    return Parts()


@pytest.fixture
def service(parts: Parts) -> IngestService:
    return IngestService(
        blobs=parts.blobs,
        vision=parts.vision,
        metadata=parts.metadata,
        embedder=parts.embedder,
        index=parts.index,
    )


def test_text_pdf_is_indexed_without_vision(service: IngestService, parts: Parts) -> None:
    result = service.ingest("raw/transcript.pdf")
    assert result.outcome is Outcome.INDEXED
    assert result.chunks >= 1
    assert result.vision_pages == 0
    assert parts.vision.calls == []
    assert parts.metadata.calls == 1
    chunks = parts.index.docs[doc_id_for("raw/transcript.pdf")]
    assert any("| Fall 2018 | CS 310 | Algorithms | 4 | A |" in c.text for c in chunks)
    assert all(c.embed_text.startswith("Transcript · Northfield State University") for c in chunks)


def test_parse_is_cached_as_json(service: IngestService, parts: Parts) -> None:
    service.ingest("raw/transcript.pdf")
    cached = json.loads(parts.blobs.objects[parsed_key(doc_id_for("raw/transcript.pdf"))])
    assert cached["source_key"] == "raw/transcript.pdf"
    assert cached["metadata"]["doc_type"] == "transcript"


def test_scanned_pdf_uses_vision_on_rendered_page(service: IngestService, parts: Parts) -> None:
    result = service.ingest("raw/award.pdf")
    assert result.vision_pages == 1
    assert parts.vision.calls == ["png"]


def test_image_goes_straight_to_vision(service: IngestService, parts: Parts) -> None:
    assert service.ingest("raw/card.png").vision_pages == 1
    assert parts.vision.calls == ["png"]


def test_reingest_same_bytes_reuses_parse(service: IngestService, parts: Parts) -> None:
    service.ingest("raw/award.pdf")
    again = service.ingest("raw/award.pdf")
    assert again.reused_parse is True
    assert parts.vision.calls == ["png"]  # vision not called a second time
    assert parts.metadata.calls == 1


def test_changed_bytes_reparse(service: IngestService, parts: Parts) -> None:
    service.ingest("raw/award.pdf")
    parts.blobs.objects["raw/award.pdf"] = sample("transcript_northfield_state.pdf")
    assert service.ingest("raw/award.pdf").reused_parse is False


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("raw/notes.docx", "unsupported type .docx"),
        ("parsed/x.json", "not under raw/"),
        ("raw/folder/", "not under raw/"),
        ("raw/noext", "unsupported type ?"),
    ],
)
def test_skips(service: IngestService, key: str, reason: str) -> None:
    result = service.ingest(key)
    assert result.outcome is Outcome.SKIPPED
    assert result.reason == reason


def test_delete_removes_index_and_cache(service: IngestService, parts: Parts) -> None:
    service.ingest("raw/transcript.pdf")
    result = service.delete("raw/transcript.pdf")
    assert result.outcome is Outcome.DELETED
    assert parts.index.count() == 0
    assert parsed_key(doc_id_for("raw/transcript.pdf")) not in parts.blobs.objects


def test_delete_unknown_document_is_harmless(service: IngestService) -> None:
    assert service.delete("raw/never-seen.pdf").outcome is Outcome.DELETED


def test_content_type_for() -> None:
    assert content_type_for("a.json") == "application/json"
    assert content_type_for("a.unknownext") == "application/octet-stream"
