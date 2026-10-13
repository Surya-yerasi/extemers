"""Grounded answer: the user prompt, and citation parsing. Pure: no AWS imports."""

import re
from collections.abc import Sequence

from pydantic import BaseModel

from docqa.domain.retrieval import RetrievedChunk

NOT_FOUND = "NOT_FOUND"
NOT_FOUND_MESSAGE = "I couldn't find that in your documents."

# The system prompt is prompts/answer.md (versioned); it must use the NOT_FOUND marker.

_CITATION = re.compile(r"\[(\d+)\]")
_MARKER = re.compile(rf"\[?{NOT_FOUND}\]?")


class Citation(BaseModel):
    number: int  # as written in the answer, 1-based
    chunk_id: str
    doc_id: str
    source_key: str
    title: str
    page_start: int
    page_end: int


class ParsedAnswer(BaseModel):
    text: str
    citations: list[Citation]
    not_found: bool


def source_label(chunk: RetrievedChunk) -> str:
    pages = (
        f"p{chunk.page_start}"
        if chunk.page_start == chunk.page_end
        else f"pp{chunk.page_start}-{chunk.page_end}"
    )
    name = chunk.title or chunk.source_key.rsplit("/", 1)[-1]
    return f"{name} ({pages})"


def build_user_prompt(question: str, chunks: Sequence[RetrievedChunk]) -> str:
    sources = "\n\n".join(f"[{i}] {source_label(c)}\n{c.text}" for i, c in enumerate(chunks, 1))
    return f"Sources:\n\n{sources}\n\nQuestion: {question}"


def parse_answer(raw: str, chunks: Sequence[RetrievedChunk]) -> ParsedAnswer:
    """Map [n] markers to chunks. Out-of-range markers are dropped from the citation list.

    A NOT_FOUND marker inside an answer ("GPA stands for ... [NOT_FOUND]") is removed; if
    what remains cites no source, the whole answer counts as not found: an uncited answer
    to a question the sources cannot answer is general knowledge, not a grounded answer.
    """
    text = raw.strip()
    if not text or text.startswith(NOT_FOUND):
        return ParsedAnswer(text=NOT_FOUND_MESSAGE, citations=[], not_found=True)
    marked = _MARKER.search(text) is not None
    text = " ".join(_MARKER.sub("", text).split()) if marked else text
    citations: list[Citation] = []
    for number in dict.fromkeys(int(n) for n in _CITATION.findall(text)):
        if 1 <= number <= len(chunks):
            c = chunks[number - 1]
            citations.append(
                Citation(
                    number=number,
                    chunk_id=c.chunk_id,
                    doc_id=c.doc_id,
                    source_key=c.source_key,
                    title=c.title,
                    page_start=c.page_start,
                    page_end=c.page_end,
                )
            )
    if marked and not citations:
        return ParsedAnswer(text=NOT_FOUND_MESSAGE, citations=[], not_found=True)
    return ParsedAnswer(text=text, citations=citations, not_found=False)
