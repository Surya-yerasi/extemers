"""Core data types for ingestion and retrieval. Pure: no AWS or storage imports."""

import hashlib
from enum import StrEnum

from pydantic import BaseModel, Field


class ExtractionMethod(StrEnum):
    TEXT = "text"  # text layer read directly from the PDF
    VISION = "vision"  # page rendered to an image and transcribed by a vision model


class Page(BaseModel):
    number: int  # 1-based
    markdown: str
    method: ExtractionMethod


class DocMetadata(BaseModel):
    """Extracted once per document; used for filters and the contextual chunk header."""

    doc_type: str = "document"  # e.g. transcript, degree_certificate, letter
    title: str = ""
    institution: str = ""
    person: str = ""
    date: str = ""  # free-form as written in the document (e.g. "May 2019")


class ParsedDocument(BaseModel):
    """Cached as JSON under parsed/ so re-chunking or re-embedding never re-parses."""

    doc_id: str
    source_key: str
    content_hash: str
    pages: list[Page]
    metadata: DocMetadata = Field(default_factory=DocMetadata)


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    ordinal: int
    text: str  # the chunk as shown to the user and to BM25
    embed_text: str  # context header + text: what gets embedded
    page_start: int
    page_end: int
    token_estimate: int


def doc_id_for(source_key: str) -> str:
    """Stable ID per object path: re-uploading the same key replaces the same document."""
    return hashlib.sha256(source_key.encode()).hexdigest()[:16]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
