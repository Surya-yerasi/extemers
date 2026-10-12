"""Ingestion: raw document in S3 → parsed Markdown (cached) → chunks → vectors → index.

Idempotent: a document is keyed by its object path (doc_id) and its bytes (content_hash).
Re-uploading identical bytes reuses the cached parse; changed bytes replace the document.

Every stage is recorded in ingest-status/<doc_id>.json, so the Documents tab can show what
is running, how far it got, how long each step took, and why a document failed.
"""

import mimetypes
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from aws_lambda_powertools import Logger

from docqa.adapters.pdf import extract_page_texts, render_page_png
from docqa.domain.chunking import DEFAULT_CHUNKING, ChunkingConfig, chunk_document
from docqa.domain.documents import (
    RAW_PREFIX,
    SUPPORTED_SUFFIXES,
    IngestStage,
    IngestState,
    IngestStatus,
    status_key,
    suffix,
)
from docqa.domain.models import (
    ExtractionMethod,
    Page,
    ParsedDocument,
    content_hash,
    doc_id_for,
)
from docqa.ports import BlobStore, ChunkIndex, Embedder, MetadataExtractor, VisionTranscriber

logger = Logger(child=True)

__all__ = ["RAW_PREFIX", "IngestResult", "IngestService", "Outcome", "parsed_key"]

PARSED_PREFIX = "parsed/"
IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
MIN_TEXT_CHARS = 40  # pages with less text than this are treated as scanned
EMBED_BATCH = 16


class Outcome(StrEnum):
    INDEXED = "indexed"
    DELETED = "deleted"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class IngestResult:
    key: str
    outcome: Outcome
    chunks: int = 0
    vision_pages: int = 0
    reused_parse: bool = False
    reason: str = ""


def parsed_key(doc_id: str) -> str:
    return f"{PARSED_PREFIX}{doc_id}.json"


class _Tracker:
    """Writes the status record as ingestion moves through its stages.

    Best effort: a failed status write is logged and ingestion carries on, because the
    document matters more than the dashboard.
    """

    MIN_PROGRESS_INTERVAL_S = 1.0  # progress within a stage: at most one write per second

    def __init__(self, blobs: BlobStore, status: IngestStatus, clock: Callable[[], datetime]):
        self._blobs = blobs
        self._clock = clock
        self.status = status
        self._stage_started = time.perf_counter()
        self._last_write = 0.0

    def stage(self, stage: IngestStage, **progress: int) -> None:
        self._close_stage()
        self.status.stage = stage
        self.status.progress = dict(progress)
        self._write()

    def progress(self, **progress: int) -> None:
        first = "total" not in self.status.progress  # always show the size of the job
        self.status.progress = dict(progress)
        if first or time.perf_counter() - self._last_write >= self.MIN_PROGRESS_INTERVAL_S:
            self._write()

    def indexed(self, **fields: object) -> None:
        self._close_stage()
        self.status = self.status.model_copy(
            update={**fields, "state": IngestState.INDEXED, "stage": None, "progress": {}}
        )
        self.status.finished_at = self._clock()
        self._write()

    def failed(self, exc: BaseException) -> None:
        self._close_stage(record=False)  # keep the stage: it is where it failed
        self.status.state = IngestState.FAILED
        self.status.error = f"{type(exc).__name__}: {str(exc)[:300]}".rstrip(": ")
        self.status.finished_at = self._clock()
        self._write()

    def _close_stage(self, record: bool = True) -> None:
        now = time.perf_counter()
        if record and self.status.stage is not None:
            elapsed = (now - self._stage_started) * 1000
            self.status.timings_ms[self.status.stage.value] = round(elapsed, 1)
        self._stage_started = now

    def _write(self) -> None:
        self.status.updated_at = self._clock()
        self._last_write = time.perf_counter()
        try:
            self._blobs.put(
                status_key(self.status.doc_id),
                self.status.model_dump_json(indent=2).encode(),
                "application/json",
            )
        except Exception as exc:  # see the class docstring
            logger.warning(
                "ingest_status_write_failed",
                extra={"doc_id": self.status.doc_id, "error": type(exc).__name__},
            )


class IngestService:
    def __init__(  # noqa: PLR0913 - explicit dependencies, injected for testing
        self,
        *,
        blobs: BlobStore,
        vision: VisionTranscriber,
        metadata: MetadataExtractor,
        embedder: Embedder,
        index: ChunkIndex,
        chunking: ChunkingConfig = DEFAULT_CHUNKING,
        render_page: Callable[[bytes, int], bytes] = render_page_png,
        index_table: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._blobs = blobs
        self._vision = vision
        self._metadata = metadata
        self._embedder = embedder
        self._index = index
        self._chunking = chunking
        self._render_page = render_page
        self._index_table = index_table
        self._clock = clock

    def ingest(self, key: str) -> IngestResult:
        if not key.startswith(RAW_PREFIX) or key.endswith("/"):
            return IngestResult(key, Outcome.SKIPPED, reason="not under raw/")
        ext = suffix(key)
        if ext not in SUPPORTED_SUFFIXES:
            return IngestResult(key, Outcome.SKIPPED, reason=f"unsupported type {ext or '?'}")

        doc_id = doc_id_for(key)
        now = self._clock()
        tracker = _Tracker(
            self._blobs,
            IngestStatus(
                doc_id=doc_id,
                source_key=key,
                state=IngestState.PROCESSING,
                started_at=now,
                updated_at=now,
                index_table=self._index_table,
            ),
            self._clock,
        )
        try:
            return self._ingest(key, doc_id, ext, tracker)
        except Exception as exc:
            tracker.failed(exc)
            raise

    def _ingest(self, key: str, doc_id: str, ext: str, tracker: _Tracker) -> IngestResult:
        tracker.stage(IngestStage.READ)
        data = self._blobs.get(key)
        digest = content_hash(data)
        tracker.status.content_hash = digest

        doc, reused = self._cached_parse(doc_id, digest)
        if doc is None:
            tracker.stage(IngestStage.PARSE)
            doc = self._parse(key, doc_id, digest, data, ext, tracker=tracker)
            tracker.stage(IngestStage.METADATA)
            full_text = "\n\n".join(p.markdown for p in doc.pages)
            doc.metadata = self._metadata.extract(full_text)
            self._blobs.put(
                parsed_key(doc_id), doc.model_dump_json(indent=2).encode(), "application/json"
            )

        tracker.stage(IngestStage.CHUNK)
        chunks = chunk_document(doc, self._chunking)
        tracker.stage(IngestStage.EMBED, done=0, total=len(chunks))
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            vectors.extend(self._embedder.embed([c.embed_text for c in batch]))
            tracker.progress(done=len(vectors), total=len(chunks))
        tracker.stage(IngestStage.INDEX)
        self._index.upsert_document(doc, chunks, vectors)

        vision_pages = sum(1 for p in doc.pages if p.method is ExtractionMethod.VISION)
        tracker.indexed(
            pages=len(doc.pages), vision_pages=vision_pages, chunks=len(chunks), reused_parse=reused
        )
        result = IngestResult(key, Outcome.INDEXED, len(chunks), vision_pages, reused)
        logger.info(
            "document_indexed",
            extra={
                "doc_id": doc_id,
                "pages": len(doc.pages),
                "chunks": len(chunks),
                "vision_pages": vision_pages,
                "reused_parse": reused,
            },
        )
        return result

    def delete(self, key: str) -> IngestResult:
        doc_id = doc_id_for(key)
        self._index.delete_document(doc_id)
        for derived in (parsed_key(doc_id), status_key(doc_id)):
            if self._blobs.exists(derived):
                self._blobs.delete(derived)
        logger.info("document_deleted", extra={"doc_id": doc_id})
        return IngestResult(key, Outcome.DELETED)

    def _cached_parse(self, doc_id: str, digest: str) -> tuple[ParsedDocument | None, bool]:
        if not self._blobs.exists(parsed_key(doc_id)):
            return None, False
        cached = ParsedDocument.model_validate_json(self._blobs.get(parsed_key(doc_id)))
        if cached.content_hash != digest:
            return None, False
        return cached, True

    def _parse(  # noqa: PLR0913
        self, key: str, doc_id: str, digest: str, data: bytes, ext: str, *, tracker: _Tracker
    ) -> ParsedDocument:
        pages: list[Page] = []
        if ext in IMAGE_FORMATS:
            tracker.progress(done=0, total=1)
            markdown = self._vision.transcribe(data, IMAGE_FORMATS[ext])
            pages.append(Page(number=1, markdown=markdown, method=ExtractionMethod.VISION))
        else:
            texts = extract_page_texts(data)
            for index, text in enumerate(texts):
                tracker.progress(done=index, total=len(texts))
                if len(text) >= MIN_TEXT_CHARS:
                    pages.append(
                        Page(number=index + 1, markdown=text, method=ExtractionMethod.TEXT)
                    )
                else:
                    image = self._render_page(data, index)
                    markdown = self._vision.transcribe(image, "png")
                    pages.append(
                        Page(number=index + 1, markdown=markdown, method=ExtractionMethod.VISION)
                    )
        return ParsedDocument(doc_id=doc_id, source_key=key, content_hash=digest, pages=pages)


def content_type_for(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"
