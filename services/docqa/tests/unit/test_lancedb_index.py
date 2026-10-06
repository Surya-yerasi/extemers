from pathlib import Path

import pytest

from docqa.adapters.lancedb_index import LanceChunkIndex, LanceChunkSearcher
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


def test_searcher_returns_ranked_chunks_with_scores(tmp_path: Path) -> None:
    uri = str(tmp_path / "db")
    writer = LanceChunkIndex(uri, "t", dimensions=4)
    for i, (doc_id, text) in enumerate(
        [("d1", "Cumulative GPA 3.86 in Computer Science"), ("d2", "Scholarship award letter")]
    ):
        doc = parsed(doc_id, text)
        writer.upsert_document(doc, chunk_document(doc), [vec(i)])

    searcher = LanceChunkSearcher(uri, "t")
    dense = searcher.vector_search(vec(1), limit=2)
    assert [c.doc_id for c in dense] == ["d2", "d1"]
    assert dense[0].scores["dense"] == pytest.approx(1.0)
    assert [c.ranks["dense"] for c in dense] == [1, 2]
    assert dense[0].source_key == "raw/d2.pdf"
    assert dense[0].doc_type == "transcript"

    lexical = searcher.text_search("What's my GPA?", limit=5)
    assert [c.doc_id for c in lexical] == ["d1"]
    assert lexical[0].scores["bm25"] > 0
    assert searcher.text_search("   ", limit=5) == []


def test_searcher_sees_documents_added_after_it_opened(tmp_path: Path) -> None:
    uri = str(tmp_path / "db")
    writer = LanceChunkIndex(uri, "t", dimensions=4)
    first = parsed("d1", "first document")
    writer.upsert_document(first, chunk_document(first), [vec(0)])
    searcher = LanceChunkSearcher(uri, "t")
    assert len(searcher.vector_search(vec(0), limit=5)) == 1

    second = parsed("d2", "second document")
    writer.upsert_document(second, chunk_document(second), [vec(1)])
    assert len(searcher.vector_search(vec(0), limit=5)) == 2


def test_searcher_on_missing_table_returns_nothing(tmp_path: Path) -> None:
    searcher = LanceChunkSearcher(str(tmp_path / "empty"), "t")
    assert searcher.vector_search(vec(0), limit=5) == []
    assert searcher.text_search("gpa", limit=5) == []
    assert not (tmp_path / "empty" / "t.lance").exists()  # read-only: nothing created
