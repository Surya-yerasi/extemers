from pathlib import Path

import pytest

from docqa.adapters.lancedb_index import LanceChunkIndex
from docqa.domain.chunking import chunk_document
from docqa.domain.models import DocMetadata, ExtractionMethod, Page, ParsedDocument


def parsed(doc_id: str, text: str) -> ParsedDocument:
    return ParsedDocument(
        doc_id=doc_id,
        source_key=f"raw/{doc_id}.pdf",
        content_hash="h",
        pages=[Page(number=1, markdown=text, method=ExtractionMethod.TEXT)],
        metadata=DocMetadata(doc_type="transcript", person="O'Brien"),
    )


def vec(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(4)]


@pytest.fixture
def index(tmp_path: Path) -> LanceChunkIndex:
    return LanceChunkIndex(str(tmp_path / "db"), "chunks__test", dimensions=4)


def test_upsert_count_and_replace(index: LanceChunkIndex) -> None:
    doc = parsed("d1", "Cumulative GPA 3.86")
    index.upsert_document(doc, chunk_document(doc), [vec(0)])
    assert index.count() == 1
    index.upsert_document(doc, chunk_document(doc), [vec(1)])  # same doc again: replaced
    assert index.count() == 1


def test_delete_document(index: LanceChunkIndex) -> None:
    for i, doc_id in enumerate(["d1", "d2"]):
        doc = parsed(doc_id, f"text {doc_id}")
        index.upsert_document(doc, chunk_document(doc), [vec(i)])
    index.delete_document("d1")
    assert index.count() == 1


def test_fts_and_vector_search_work_on_written_table(index: LanceChunkIndex) -> None:
    a = parsed("d1", "Bachelor of Science in Computer Science conferred")
    b = parsed("d2", "Scholarship award letter for four years")
    index.upsert_document(a, chunk_document(a), [vec(0)])
    index.upsert_document(b, chunk_document(b), [vec(1)])
    table = index._open()
    hits = table.search("scholarship", query_type="fts").limit(1).to_list()
    assert hits[0]["doc_id"] == "d2"
    nearest = table.search(vec(0)).metric("cosine").limit(1).to_list()
    assert nearest[0]["doc_id"] == "d1"
    assert nearest[0]["person"] == "O'Brien"  # quotes survive


def test_vector_count_mismatch(index: LanceChunkIndex) -> None:
    doc = parsed("d1", "x")
    with pytest.raises(ValueError, match="one vector per chunk"):
        index.upsert_document(doc, chunk_document(doc), [])


def test_reopen_existing_table(tmp_path: Path) -> None:
    doc = parsed("d1", "x")
    LanceChunkIndex(str(tmp_path), "t", 4).upsert_document(doc, chunk_document(doc), [vec(0)])
    assert LanceChunkIndex(str(tmp_path), "t", 4).count() == 1
