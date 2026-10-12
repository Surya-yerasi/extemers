"""Documents tab: state rules, the joined overview, the index catalog and the API."""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docqa import cli
from docqa.adapters.lancedb_index import LanceChunkIndex, LanceDocumentCatalog
from docqa.adapters.local_blobs import LocalBlobStore
from docqa.config import AppConfig
from docqa.domain.chunking import chunk_document
from docqa.domain.documents import (
    BlobInfo,
    DocState,
    IndexedChunk,
    IndexedDocument,
    IngestStage,
    IngestState,
    IngestStatus,
    build_rows,
    derive_state,
    state_counts,
    status_key,
)
from docqa.domain.models import DocMetadata, ExtractionMethod, Page, ParsedDocument, doc_id_for
from docqa.pipelines.documents import DocumentService
from docqa.web.app import create_app
from docqa.web.auth import SESSION_COOKIE, TokenVerifier
from tests.fakes import MemoryBlobStore

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
KEY = "raw/transcript.pdf"
DOC = doc_id_for(KEY)


def blob(key: str = KEY, modified: datetime = NOW - timedelta(hours=2)) -> BlobInfo:
    return BlobInfo(key=key, size=1234, modified=modified)


def status(
    state: IngestState = IngestState.INDEXED,
    started: datetime = NOW - timedelta(hours=1),
    updated: datetime | None = None,
    stage: IngestStage | None = None,
    key: str = KEY,
) -> IngestStatus:
    return IngestStatus(
        doc_id=doc_id_for(key),
        source_key=key,
        state=state,
        stage=stage,
        started_at=started,
        updated_at=updated or started,
        error="RuntimeError: boom" if state is IngestState.FAILED else None,
    )


def indexed(key: str = KEY, chunks: int = 3) -> IndexedDocument:
    return IndexedDocument(
        doc_id=doc_id_for(key), source_key=key, content_hash="h", chunks=chunks, pages=2
    )


# ------------------------------------------------------------------ state rules


@pytest.mark.parametrize(
    ("raw", "st", "ix", "expected"),
    [
        (blob(), None, None, DocState.PENDING),
        (blob(), None, indexed(), DocState.INDEXED),  # indexed before status tracking
        (blob(), status(), indexed(), DocState.INDEXED),
        (blob(), status(), None, DocState.INDEXED),  # ingested but produced no chunks
        (blob(), status(IngestState.PROCESSING, updated=NOW), None, DocState.PROCESSING),
        (
            blob(),
            status(IngestState.PROCESSING, updated=NOW - timedelta(minutes=16)),
            indexed(),
            DocState.STALLED,
        ),
        (blob(), status(IngestState.FAILED), None, DocState.FAILED),
        (blob(), status(IngestState.FAILED), indexed(), DocState.FAILED),  # old version kept
        (blob(modified=NOW), status(IngestState.FAILED), None, DocState.PENDING),  # replaced
        (blob(modified=NOW), status(), indexed(), DocState.OUTDATED),
        (None, None, indexed(), DocState.ORPHANED),
        (None, status(), indexed(), DocState.ORPHANED),
        (blob("raw/notes.docx"), None, None, DocState.UNSUPPORTED),
    ],
)
def test_derive_state(
    raw: BlobInfo | None,
    st: IngestStatus | None,
    ix: IndexedDocument | None,
    expected: DocState,
) -> None:
    derived = derive_state(raw, st, ix, NOW)
    assert derived is not None
    assert derived[0] is expected
    assert derived[1]  # every state explains itself


def test_failed_reason_names_the_stage_and_the_kept_version() -> None:
    st = status(IngestState.FAILED, stage=IngestStage.EMBED)
    derived = derive_state(blob(), st, indexed(), NOW)
    assert derived is not None
    assert "embed" in derived[1]
    assert "RuntimeError: boom" in derived[1]
    assert "previous version is still searchable" in derived[1]


def test_fully_deleted_document_disappears() -> None:
    assert derive_state(None, status(), None, NOW) is None


def test_build_rows_joins_by_doc_id_and_puts_work_first() -> None:
    rows = build_rows(
        raw=[blob("raw/a.pdf"), blob("raw/b.pdf"), blob("raw/c.pdf")],
        statuses=[
            status(key="raw/a.pdf"),
            status(IngestState.PROCESSING, updated=NOW, key="raw/c.pdf"),
        ],
        indexed=[indexed("raw/a.pdf"), indexed("raw/gone.pdf")],
        now=NOW,
    )
    assert [(r.source_key, r.state) for r in rows] == [
        ("raw/c.pdf", DocState.PROCESSING),
        ("raw/b.pdf", DocState.PENDING),
        ("raw/gone.pdf", DocState.ORPHANED),
        ("raw/a.pdf", DocState.INDEXED),
    ]
    a = rows[-1]
    assert a.size == 1234
    assert a.indexed is not None
    assert a.status is not None
    counts = state_counts(rows)
    assert counts["indexed"] == counts["pending"] == counts["processing"] == 1
    assert counts["failed"] == 0  # every state is present, zero or not


# ------------------------------------------------------------------ service


class FakeCatalog:
    def __init__(self, docs: list[IndexedDocument]) -> None:
        self.docs = docs
        self.chunk_calls: list[str] = []

    def documents(self) -> list[IndexedDocument]:
        return self.docs

    def chunks(self, doc_id: str) -> list[IndexedChunk]:
        self.chunk_calls.append(doc_id)
        return [
            IndexedChunk(
                chunk_id=f"{doc_id}:0",
                ordinal=0,
                page_start=1,
                page_end=1,
                text="t",
                embed_text="h\n\nt",
            )
        ]


def service(blobs: MemoryBlobStore, docs: list[IndexedDocument]) -> DocumentService:
    return DocumentService(blobs, FakeCatalog(docs), "chunks__t", source="raw/", clock=lambda: NOW)


def test_overview_totals_and_activity() -> None:
    running = status(IngestState.PROCESSING, updated=NOW, key="raw/b.pdf")
    done = status().model_copy(update={"pages": 2, "vision_pages": 1, "chunks": 3})
    blobs = MemoryBlobStore(
        {
            KEY: b"x" * 10,
            "raw/b.pdf": b"y" * 5,
            status_key(DOC): done.model_dump_json().encode(),
            status_key(running.doc_id): running.model_dump_json().encode(),
            status_key("0" * 16): b"not json",  # skipped, not fatal
        }
    )
    overview = service(blobs, [indexed()]).overview()
    assert overview.active
    assert overview.totals == {
        "documents": 2, "chunks": 3, "pages": 2, "vision_pages": 1, "bytes": 15,
    }  # fmt: skip
    assert overview.counts["processing"] == 1
    assert overview.counts["indexed"] == 1
    assert overview.index_table == "chunks__t"


def test_document_detail_reads_chunks_only_when_indexed() -> None:
    blobs = MemoryBlobStore({KEY: b"x", "raw/new.pdf": b"y"})
    catalog = FakeCatalog([indexed()])
    svc = DocumentService(blobs, catalog, "chunks__t", clock=lambda: NOW)
    detail = svc.document(DOC)
    assert detail is not None
    assert len(detail.chunks) == 1
    pending = svc.document(doc_id_for("raw/new.pdf"))
    assert pending is not None
    assert pending.document.state is DocState.PENDING
    assert pending.chunks == []
    assert catalog.chunk_calls == [DOC]
    assert svc.document("f" * 16) is None


# ------------------------------------------------------------------ adapters


def parsed(key: str, pages: int) -> ParsedDocument:
    return ParsedDocument(
        doc_id=doc_id_for(key),
        source_key=key,
        content_hash="abc",
        pages=[
            Page(
                number=n, markdown=f"## Page {n}\n\n" + "words " * 300, method=ExtractionMethod.TEXT
            )
            for n in range(1, pages + 1)
        ],
        metadata=DocMetadata(doc_type="transcript", title="Transcript", institution="Northfield"),
    )


def test_lance_catalog_aggregates_per_document(tmp_path: Path) -> None:
    uri = str(tmp_path / "db")
    catalog = LanceDocumentCatalog(uri, "chunks__t")
    assert catalog.documents() == []  # no table yet
    assert catalog.chunks(DOC) == []

    index = LanceChunkIndex(uri, "chunks__t", dimensions=4)
    for key, pages in [(KEY, 3), ("raw/o'brien.pdf", 1)]:
        doc = parsed(key, pages)
        chunks = chunk_document(doc)
        index.upsert_document(doc, chunks, [[1.0, 0, 0, 0]] * len(chunks))

    docs = {d.source_key: d for d in catalog.documents()}
    transcript = docs[KEY]
    assert transcript.pages == 3
    assert transcript.chunks == len(chunk_document(parsed(KEY, 3)))
    assert (transcript.title, transcript.institution, transcript.person) == (
        "Transcript",
        "Northfield",
        "",
    )
    stored = catalog.chunks(DOC)
    assert [c.ordinal for c in stored] == list(range(transcript.chunks))
    assert stored[0].embed_text.endswith(stored[0].text)
    quoted = catalog.chunks(doc_id_for("raw/o'brien.pdf"))  # a quote in the key is safe
    assert len(quoted) == len(chunk_document(parsed("raw/o'brien.pdf", 1)))


def test_local_list_objects(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("raw/a.pdf", b"12345", "application/pdf")
    os.utime(tmp_path / "raw" / "a.pdf", (1_700_000_000, 1_700_000_000))
    [info] = store.list_objects("raw/")
    assert (info.key, info.size) == ("raw/a.pdf", 5)
    assert info.modified == datetime.fromtimestamp(1_700_000_000, UTC)


def test_s3_list_objects_skips_folders() -> None:
    import boto3  # noqa: PLC0415
    from botocore.stub import Stubber  # noqa: PLC0415

    from docqa.adapters.s3_blobs import S3BlobStore  # noqa: PLC0415

    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    stub = Stubber(client)
    stub.add_response(
        "list_objects_v2",
        {
            "Contents": [
                {"Key": "raw/", "Size": 0, "LastModified": NOW},
                {"Key": "raw/a.pdf", "Size": 9, "LastModified": NOW},
            ]
        },
        {"Bucket": "b", "Prefix": "raw/"},
    )
    stub.activate()
    assert S3BlobStore("b", client).list_objects("raw/") == [
        BlobInfo(key="raw/a.pdf", size=9, modified=NOW)
    ]


# ------------------------------------------------------------------ CLI prune


def test_prune_deletes_documents_whose_file_is_gone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted: list[str] = []

    stale = status(IngestState.FAILED, key="raw/failed-then-deleted.pdf")

    class Service:
        _blobs = MemoryBlobStore(
            {
                KEY: b"x",
                status_key(DOC): status().model_dump_json().encode(),
                status_key(stale.doc_id): stale.model_dump_json().encode(),
            }
        )

        def delete(self, key: str) -> None:
            deleted.append(key)
            self._blobs.delete(status_key(doc_id_for(key)))

    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")
    monkeypatch.setattr(cli, "build_ingest_service", lambda _settings: Service())
    monkeypatch.setattr(
        cli,
        "LanceDocumentCatalog",
        lambda _uri, _table: FakeCatalog([indexed(), indexed("raw/gone.pdf", chunks=4)]),
    )
    assert cli.main(["prune"]) == 0
    assert deleted == ["raw/gone.pdf", "raw/failed-then-deleted.pdf"]
    out = capsys.readouterr().out
    assert "deleted  raw/gone.pdf  (4 chunks)" in out
    assert "1 document(s) pruned, 1 stale status record(s) removed" in out


# ------------------------------------------------------------------ API


def test_document_endpoints(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    blobs = MemoryBlobStore({KEY: b"x"})
    app = create_app(config, verifier, documents=service(blobs, [indexed()]))
    client = TestClient(app)
    assert client.get("/api/documents").status_code == 401
    client.cookies.set(SESSION_COOKIE, make_token())

    overview = client.get("/api/documents").json()
    assert [d["state"] for d in overview["documents"]] == ["indexed"]
    assert overview["source"] == "raw/"

    detail = client.get(f"/api/documents/{DOC}").json()
    assert detail["document"]["source_key"] == KEY
    assert detail["chunks"][0]["embed_text"] == "h\n\nt"
    assert client.get("/api/documents/" + "f" * 16).status_code == 404
    assert client.get("/api/documents/not-an-id").status_code == 404


def test_document_service_is_built_lazily(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    app = create_app(config, verifier)
    app.state.documents_factory = lambda bucket: (_ for _ in ()).throw(ValueError("no bucket"))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.get("/api/documents").status_code == 503


def test_page_has_documents_tab(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    client = TestClient(create_app(config, verifier))
    client.cookies.set(SESSION_COOKIE, make_token())
    page = client.get("/").text
    assert 'id="tab-documents"' in page
    assert "/static/documents.js" in page
    assert client.get("/static/documents.js").status_code == 200
