"""LanceDB chunk index: vectors + BM25 full-text index in one table, stored on S3 (or disk).

LanceChunkIndex writes (ingestion); LanceChunkSearcher reads (question answering).

One table per index variant (chunking x embedding), e.g. chunks__struct400__titan1024, so
experiments never overwrite each other.
"""

from collections.abc import Sequence
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from docqa.domain.models import Chunk, ParsedDocument
from docqa.domain.retrieval import RetrievedChunk, ranked


def chunk_schema(dimensions: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("ordinal", pa.int32()),
            pa.field("text", pa.string()),
            pa.field("embed_text", pa.string()),
            pa.field("page_start", pa.int32()),
            pa.field("page_end", pa.int32()),
            pa.field("source_key", pa.string()),
            pa.field("content_hash", pa.string()),
            pa.field("doc_type", pa.string()),
            pa.field("title", pa.string()),
            pa.field("institution", pa.string()),
            pa.field("person", pa.string()),
            pa.field("date", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimensions)),
        ]
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class LanceChunkIndex:
    def __init__(self, uri: str, table_name: str, dimensions: int) -> None:
        self._db: Any = lancedb.connect(uri)
        self._table_name = table_name
        self._dimensions = dimensions
        self._table: Any = None

    def _open(self) -> Any:
        if self._table is None:
            # exist_ok: open the table if another writer (or an earlier run) created it.
            self._table = self._db.create_table(
                self._table_name, schema=chunk_schema(self._dimensions), exist_ok=True
            )
        return self._table

    def upsert_document(
        self, doc: ParsedDocument, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk required")
        table = self._open()
        table.delete(f"doc_id = {_quote(doc.doc_id)}")
        meta = doc.metadata
        rows = [
            {
                **chunk.model_dump(exclude={"token_estimate"}),
                "source_key": doc.source_key,
                "content_hash": doc.content_hash,
                "doc_type": meta.doc_type,
                "title": meta.title,
                "institution": meta.institution,
                "person": meta.person,
                "date": meta.date,
                "vector": list(vector),
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        if rows:
            table.add(rows)
        self._refresh_fts(table)

    def delete_document(self, doc_id: str) -> None:
        table = self._open()
        table.delete(f"doc_id = {_quote(doc_id)}")
        self._refresh_fts(table)

    def count(self) -> int:
        return int(self._open().count_rows())

    @staticmethod
    def _refresh_fts(table: Any) -> None:
        # Rebuilding is cheap at this scale (a few thousand chunks) and keeps BM25 exact.
        if table.count_rows() > 0:
            table.create_index("text", config=FTS(), replace=True)


_RESULT_COLUMNS = [
    "chunk_id", "doc_id", "text", "page_start", "page_end", "source_key", "title", "doc_type",
]  # fmt: skip


def _to_chunk(row: dict[str, Any], stage: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(**{k: row[k] for k in _RESULT_COLUMNS}, scores={stage: round(score, 6)})


class LanceChunkSearcher:
    """Read-only. Never creates the table, so the web function needs no write access.

    Exact (flat) vector search: at a few thousand chunks it beats an ANN index on both
    recall and latency. A missing table (nothing ingested yet) returns no results.
    """

    def __init__(self, uri: str, table_name: str) -> None:
        self._db: Any = lancedb.connect(uri)
        self._table_name = table_name
        self._table: Any = None

    def _open(self) -> Any:
        if self._table is None:
            try:
                self._table = self._db.open_table(self._table_name)
            except ValueError:  # table not found
                return None
        else:
            self._table.checkout_latest()  # pick up documents ingested since the last query
        return self._table

    def vector_search(self, vector: Sequence[float], limit: int) -> list[RetrievedChunk]:
        table = self._open()
        if table is None:
            return []
        rows = (
            table.search(list(vector))
            .metric("cosine")
            .select(_RESULT_COLUMNS)
            .limit(limit)
            .to_list()
        )
        return ranked([_to_chunk(r, "dense", 1.0 - r["_distance"]) for r in rows], "dense")

    def text_search(self, query: str, limit: int) -> list[RetrievedChunk]:
        table = self._open()
        if table is None or not query.strip():
            return []
        rows = table.search(query, query_type="fts").select(_RESULT_COLUMNS).limit(limit).to_list()
        return ranked([_to_chunk(r, "bm25", r["_score"]) for r in rows], "bm25")
