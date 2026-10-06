"""Ingestion: raw document in S3 → parsed Markdown (cached) → chunks → vectors → index.

Idempotent: a document is keyed by its object path (doc_id) and its bytes (content_hash).
Re-uploading identical bytes reuses the cached parse; changed bytes replace the document.
"""

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aws_lambda_powertools import Logger

from docqa.adapters.pdf import extract_page_texts, render_page_png
from docqa.domain.chunking import DEFAULT_CHUNKING, ChunkingConfig, chunk_document
from docqa.domain.models import (
    ExtractionMethod,
    Page,
    ParsedDocument,
    content_hash,
    doc_id_for,
)
from docqa.ports import BlobStore, ChunkIndex, Embedder, MetadataExtractor, VisionTranscriber

logger = Logger(child=True)

RAW_PREFIX = "raw/"
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


def suffix(key: str) -> str:
    dot = key.rfind(".")
    return key[dot:].lower() if dot != -1 else ""


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
    ) -> None:
        self._blobs = blobs
        self._vision = vision
        self._metadata = metadata
        self._embedder = embedder
        self._index = index
        self._chunking = chunking
        self._render_page = render_page

    def ingest(self, key: str) -> IngestResult:
        if not key.startswith(RAW_PREFIX) or key.endswith("/"):
            return IngestResult(key, Outcome.SKIPPED, reason="not under raw/")
        ext = suffix(key)
        if ext != ".pdf" and ext not in IMAGE_FORMATS:
            return IngestResult(key, Outcome.SKIPPED, reason=f"unsupported type {ext or '?'}")

        data = self._blobs.get(key)
        doc_id = doc_id_for(key)
        digest = content_hash(data)

        doc, reused = self._cached_parse(doc_id, digest)
        if doc is None:
            doc = self._parse(key, doc_id, digest, data, ext)
            full_text = "\n\n".join(p.markdown for p in doc.pages)
            doc.metadata = self._metadata.extract(full_text)
            self._blobs.put(
                parsed_key(doc_id), doc.model_dump_json(indent=2).encode(), "application/json"
            )

        chunks = chunk_document(doc, self._chunking)
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            vectors.extend(self._embedder.embed([c.embed_text for c in batch]))
        self._index.upsert_document(doc, chunks, vectors)

        vision_pages = sum(1 for p in doc.pages if p.method is ExtractionMethod.VISION)
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
        if self._blobs.exists(parsed_key(doc_id)):
            self._blobs.delete(parsed_key(doc_id))
        logger.info("document_deleted", extra={"doc_id": doc_id})
        return IngestResult(key, Outcome.DELETED)

    def _cached_parse(self, doc_id: str, digest: str) -> tuple[ParsedDocument | None, bool]:
        if not self._blobs.exists(parsed_key(doc_id)):
            return None, False
        cached = ParsedDocument.model_validate_json(self._blobs.get(parsed_key(doc_id)))
        if cached.content_hash != digest:
            return None, False
        return cached, True

    def _parse(self, key: str, doc_id: str, digest: str, data: bytes, ext: str) -> ParsedDocument:
        pages: list[Page] = []
        if ext in IMAGE_FORMATS:
            markdown = self._vision.transcribe(data, IMAGE_FORMATS[ext])
            pages.append(Page(number=1, markdown=markdown, method=ExtractionMethod.VISION))
        else:
            for index, text in enumerate(extract_page_texts(data)):
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
