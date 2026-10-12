import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from docqa.domain.documents import IngestStage, IngestState, IngestStatus, status_key
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


def test_delete_removes_index_cache_and_status(service: IngestService, parts: Parts) -> None:
    service.ingest("raw/transcript.pdf")
    result = service.delete("raw/transcript.pdf")
    assert result.outcome is Outcome.DELETED
    assert parts.index.count() == 0
    doc_id = doc_id_for("raw/transcript.pdf")
    assert parsed_key(doc_id) not in parts.blobs.objects
    assert status_key(doc_id) not in parts.blobs.objects


def test_delete_unknown_document_is_harmless(service: IngestService) -> None:
    assert service.delete("raw/never-seen.pdf").outcome is Outcome.DELETED


def test_content_type_for() -> None:
    assert content_type_for("a.json") == "application/json"
    assert content_type_for("a.unknownext") == "application/octet-stream"


# ------------------------------------------------------------------ status records


def read_status(parts: Parts, key: str) -> IngestStatus:
    return IngestStatus.model_validate_json(parts.blobs.objects[status_key(doc_id_for(key))])


def test_status_records_every_stage(service: IngestService, parts: Parts) -> None:
    result = service.ingest("raw/award.pdf")
    st = read_status(parts, "raw/award.pdf")
    assert st.state is IngestState.INDEXED
    assert st.stage is None
    assert list(st.timings_ms) == ["read", "parse", "metadata", "chunk", "embed", "index"]
    assert (st.pages, st.vision_pages, st.chunks, st.reused_parse) == (1, 1, result.chunks, False)
    assert st.finished_at is not None
    assert st.started_at <= st.finished_at
    assert st.content_hash
    assert st.error is None


def test_reused_parse_records_no_parse_or_metadata_time(
    service: IngestService, parts: Parts
) -> None:
    service.ingest("raw/award.pdf")
    service.ingest("raw/award.pdf")
    st = read_status(parts, "raw/award.pdf")
    assert st.reused_parse is True
    assert "parse" not in st.timings_ms
    assert "metadata" not in st.timings_ms


def test_failure_is_recorded_at_its_stage_and_reraised(parts: Parts) -> None:
    class BrokenEmbedder:
        dimensions = 8

        def embed(self, texts: object) -> list[list[float]]:
            raise RuntimeError("model unavailable")

    service = IngestService(
        blobs=parts.blobs,
        vision=parts.vision,
        metadata=parts.metadata,
        embedder=BrokenEmbedder(),
        index=parts.index,
        index_table="chunks__t",
    )
    with pytest.raises(RuntimeError):
        service.ingest("raw/transcript.pdf")
    st = read_status(parts, "raw/transcript.pdf")
    assert st.state is IngestState.FAILED
    assert st.stage is IngestStage.EMBED
    assert st.error == "RuntimeError: model unavailable"
    assert st.index_table == "chunks__t"
    assert "embed" not in st.timings_ms  # the failed stage has no completed time
    assert "chunk" in st.timings_ms


def test_status_shows_progress_while_running(parts: Parts) -> None:
    seen: list[IngestStatus] = []

    class WatchingEmbedder:
        dimensions = 8

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            seen.append(read_status(parts, "raw/transcript.pdf"))
            return [[1.0] * 8 for _ in texts]

    service = IngestService(
        blobs=parts.blobs,
        vision=parts.vision,
        metadata=parts.metadata,
        embedder=WatchingEmbedder(),
        index=parts.index,
    )
    service.ingest("raw/transcript.pdf")
    first = seen[0]
    assert first.state is IngestState.PROCESSING
    assert first.stage is IngestStage.EMBED
    assert first.progress["done"] == 0
    assert first.progress["total"] >= 1


def test_status_write_failure_does_not_fail_ingestion(parts: Parts) -> None:
    class StatusWritesFail(MemoryBlobStore):
        def put(self, key: str, data: bytes, content_type: str) -> None:
            if key.startswith("ingest-status/"):
                raise OSError("disk full")
            super().put(key, data, content_type)

    blobs = StatusWritesFail(parts.blobs.objects)
    service = IngestService(
        blobs=blobs,
        vision=parts.vision,
        metadata=parts.metadata,
        embedder=parts.embedder,
        index=parts.index,
    )
    assert service.ingest("raw/transcript.pdf").outcome is Outcome.INDEXED
