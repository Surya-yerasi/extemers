"""The Documents tab: every document, joined across raw/, ingest status and the index.

Reads names, sizes, status records and index rows; never the uploaded files themselves (the
deployed web function has no read access to raw/ contents).
"""

from collections.abc import Callable
from datetime import UTC, datetime

from aws_lambda_powertools import Logger
from pydantic import BaseModel

from docqa.domain.documents import (
    ACTIVE_STATES,
    RAW_PREFIX,
    STATUS_PREFIX,
    DocumentRow,
    IndexedChunk,
    IngestStatus,
    build_rows,
    state_counts,
)
from docqa.ports import BlobStore, DocumentCatalog

logger = Logger(child=True)


class DocumentsOverview(BaseModel):
    generated_at: datetime
    index_table: str
    source: str  # where to put new documents
    ingest_hint: str  # how they get ingested from there
    counts: dict[str, int]  # per DocState
    totals: dict[str, int]  # documents, chunks, pages, vision_pages, bytes
    active: bool  # anything processing: the page polls faster
    documents: list[DocumentRow]


class DocumentDetail(BaseModel):
    document: DocumentRow
    chunks: list[IndexedChunk]


class DocumentService:
    def __init__(  # noqa: PLR0913 - explicit dependencies, injected for testing
        self,
        blobs: BlobStore,
        catalog: DocumentCatalog,
        index_table: str,
        *,
        source: str = "raw/",
        ingest_hint: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._blobs = blobs
        self._catalog = catalog
        self._index_table = index_table
        self._source = source
        self._ingest_hint = ingest_hint
        self._clock = clock

    def overview(self) -> DocumentsOverview:
        rows = self._rows()
        indexed = [r.indexed for r in rows if r.indexed]
        statuses = [r.status for r in rows if r.status and r.status.vision_pages is not None]
        return DocumentsOverview(
            generated_at=self._clock(),
            index_table=self._index_table,
            source=self._source,
            ingest_hint=self._ingest_hint,
            counts=state_counts(rows),
            totals={
                "documents": len(rows),
                "chunks": sum(d.chunks for d in indexed),
                "pages": sum(d.pages for d in indexed),
                "vision_pages": sum(s.vision_pages or 0 for s in statuses),
                "bytes": sum(r.size or 0 for r in rows),
            },
            active=any(r.state in ACTIVE_STATES for r in rows),
            documents=rows,
        )

    def document(self, doc_id: str) -> DocumentDetail | None:
        row = next((r for r in self._rows() if r.doc_id == doc_id), None)
        if row is None:
            return None
        chunks = self._catalog.chunks(doc_id) if row.indexed else []
        return DocumentDetail(document=row, chunks=chunks)

    def _rows(self) -> list[DocumentRow]:
        return build_rows(
            self._blobs.list_objects(RAW_PREFIX),
            self._statuses(),
            self._catalog.documents(),
            self._clock(),
        )

    def _statuses(self) -> list[IngestStatus]:
        statuses = []
        for key in self._blobs.list_keys(STATUS_PREFIX):
            try:
                statuses.append(IngestStatus.model_validate_json(self._blobs.get(key)))
            except Exception:  # one bad record must not hide the others
                # Unreadable, or deleted between the listing and the read.
                logger.warning("ingest_status_unreadable", extra={"key": key})
        return statuses
