"""Document inventory: what is in raw/, what made it into the index, and where each one is.

Pure: the pipeline gathers three sources and this module joins them.
- raw/ listing: what you uploaded (name, size, modified time; never the contents)
- ingest-status/<doc_id>.json: written by ingestion at every stage (live progress, failures)
- the index: what retrieval can actually search (chunks per document)

None of the three alone tells the truth. A file can be uploaded but not yet indexed, indexed
from an older version, half-way through embedding, failed, or deleted with its chunks still
searchable. The state shown per document is derived from all three.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from docqa.domain.models import doc_id_for

RAW_PREFIX = "raw/"
STATUS_PREFIX = "ingest-status/"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
# Lambda stops an ingest after 15 minutes; a "processing" record older than that will never
# finish (the function was killed, or a laptop run was interrupted).
STALL_AFTER = timedelta(minutes=15)


def status_key(doc_id: str) -> str:
    return f"{STATUS_PREFIX}{doc_id}.json"


def suffix(key: str) -> str:
    dot = key.rfind(".")
    return key[dot:].lower() if dot != -1 else ""


class IngestStage(StrEnum):
    """Ingestion steps, in order."""

    READ = "read"  # fetch the bytes, hash them
    PARSE = "parse"  # PDF text layer, or vision transcription of scanned pages/images
    METADATA = "metadata"  # an LLM extracts type, title, institution, date
    CHUNK = "chunk"  # split into retrieval units
    EMBED = "embed"  # one vector per chunk
    INDEX = "index"  # replace the document's rows; rebuild the BM25 index


class IngestState(StrEnum):
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class IngestStatus(BaseModel):
    """One ingestion attempt of one document, rewritten as it moves through the stages."""

    doc_id: str
    source_key: str
    state: IngestState
    stage: IngestStage | None = None  # current stage (processing) or where it failed
    progress: dict[str, int] = Field(default_factory=dict)  # e.g. {"done": 16, "total": 40}
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)  # per completed stage
    content_hash: str = ""
    pages: int | None = None
    vision_pages: int | None = None
    chunks: int | None = None
    reused_parse: bool | None = None
    index_table: str = ""
    prompts: dict[str, str] = Field(default_factory=dict)  # transcribe/metadata prompt IDs
    error: str | None = None


class BlobInfo(BaseModel):
    key: str
    size: int
    modified: datetime


class IndexedDocument(BaseModel):
    """A document as the index sees it (aggregated from its chunk rows)."""

    doc_id: str
    source_key: str
    content_hash: str
    chunks: int
    pages: int  # highest page number covered by a chunk
    doc_type: str = ""
    title: str = ""
    institution: str = ""
    person: str = ""
    date: str = ""


class IndexedChunk(BaseModel):
    chunk_id: str
    ordinal: int
    page_start: int
    page_end: int
    text: str
    embed_text: str  # what was embedded: context header + text


class DocState(StrEnum):
    INDEXED = "indexed"  # searchable, up to date
    PROCESSING = "processing"  # ingestion running now
    PENDING = "pending"  # uploaded, not ingested yet
    OUTDATED = "outdated"  # changed since it was indexed; the old version is still searched
    FAILED = "failed"  # the last attempt failed (an older version may still be searched)
    STALLED = "stalled"  # "processing" for too long: the run died without recording why
    ORPHANED = "orphaned"  # the file is gone but its chunks are still searchable
    UNSUPPORTED = "unsupported"  # a file type ingestion skips


ACTIVE_STATES = frozenset({DocState.PROCESSING})
ATTENTION_STATES = frozenset(
    {DocState.FAILED, DocState.STALLED, DocState.OUTDATED, DocState.ORPHANED, DocState.PENDING}
)


class DocumentRow(BaseModel):
    doc_id: str
    source_key: str
    state: DocState
    reason: str  # one sentence: why this state, and what to do about it
    size: int | None = None
    modified: datetime | None = None
    indexed: IndexedDocument | None = None
    status: IngestStatus | None = None


def derive_state(  # noqa: PLR0911 - one return per rule reads clearer than a table here
    raw: BlobInfo | None,
    status: IngestStatus | None,
    indexed: IndexedDocument | None,
    now: datetime,
) -> tuple[DocState, str] | None:
    """The state of one document and why. None: nothing left to show (fully deleted)."""
    if status and status.state is IngestState.PROCESSING:
        if now - status.updated_at > STALL_AFTER:
            return DocState.STALLED, (
                f"No progress since the {status.stage or 'first'} step; the run was "
                "interrupted. Ingest it again."
            )
        return DocState.PROCESSING, f"Ingesting: {status.stage or 'starting'}."
    if raw is None:
        if indexed:
            return DocState.ORPHANED, (
                "The file was removed but its chunks are still searchable. Prune the index."
            )
        return None
    if suffix(raw.key) not in SUPPORTED_SUFFIXES:
        return DocState.UNSUPPORTED, "Ingestion reads PDF, PNG and JPEG files only."
    # Changed after the last attempt started: that attempt saw an older version.
    changed = status is not None and raw.modified > status.started_at
    if status and status.state is IngestState.FAILED and not changed:
        searchable = " The previous version is still searchable." if indexed else ""
        return DocState.FAILED, f"Failed at {status.stage or 'start'}: {status.error}.{searchable}"
    if indexed or (status and status.state is IngestState.INDEXED):
        if changed:
            return DocState.OUTDATED, (
                "The file changed after it was indexed; answers use the old version until "
                "it is ingested again."
            )
        if indexed is None:
            return DocState.INDEXED, "Ingested, but it produced no text to index."
        return DocState.INDEXED, "Searchable."
    return DocState.PENDING, "Uploaded, not ingested yet."


def build_rows(
    raw: Sequence[BlobInfo],
    statuses: Sequence[IngestStatus],
    indexed: Sequence[IndexedDocument],
    now: datetime,
) -> list[DocumentRow]:
    """Join the three sources by doc_id; in progress and problems first, then by name."""
    raw_by_id = {doc_id_for(b.key): b for b in raw}
    status_by_id = {s.doc_id: s for s in statuses}
    index_by_id = {d.doc_id: d for d in indexed}
    rows = []
    for doc_id in raw_by_id.keys() | status_by_id.keys() | index_by_id.keys():
        blob, status, doc = raw_by_id.get(doc_id), status_by_id.get(doc_id), index_by_id.get(doc_id)
        derived = derive_state(blob, status, doc, now)
        if derived is None:
            continue
        source_key = (
            (blob and blob.key) or (doc and doc.source_key) or (status and status.source_key)
        )
        rows.append(
            DocumentRow(
                doc_id=doc_id,
                source_key=source_key or "",
                state=derived[0],
                reason=derived[1],
                size=blob.size if blob else None,
                modified=blob.modified if blob else None,
                indexed=doc,
                status=status,
            )
        )
    return sorted(rows, key=lambda r: (_PRIORITY[r.state], r.source_key))


_PRIORITY = {
    DocState.PROCESSING: 0, DocState.STALLED: 1, DocState.FAILED: 1, DocState.OUTDATED: 2,
    DocState.PENDING: 2, DocState.ORPHANED: 3, DocState.UNSUPPORTED: 4, DocState.INDEXED: 5,
}  # fmt: skip


def state_counts(rows: Sequence[DocumentRow]) -> dict[str, int]:
    counts = Counter(r.state.value for r in rows)
    return {s.value: counts.get(s.value, 0) for s in DocState}
