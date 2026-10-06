"""Structure-aware Markdown chunking.

Blocks (headings, paragraphs, list runs, tables) are kept whole where possible and packed
into chunks of about `max_tokens`, with overlap between neighbours. Oversized tables are
split by rows and repeat their header in every piece, so a table chunk is still readable on
its own. A document that fits in one chunk is kept whole.
"""

import math
import re
from dataclasses import dataclass

from docqa.domain.models import Chunk, DocMetadata, ParsedDocument

_HEADING = re.compile(r"^#{1,6}\s")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}")


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic estimate (~4 characters per token for English)."""
    return max(1, math.ceil(len(text) / 4))


@dataclass(frozen=True)
class Block:
    text: str
    page: int
    is_table: bool = False


@dataclass(frozen=True)
class ChunkingConfig:
    max_tokens: int = 400
    overlap_tokens: int = 60  # ~15%


DEFAULT_CHUNKING = ChunkingConfig()


def split_blocks(markdown: str, page: int) -> list[Block]:
    """Split one page of Markdown into headings, paragraphs and tables."""
    blocks: list[Block] = []
    current: list[str] = []
    in_table = False

    def flush() -> None:
        nonlocal current
        text = "\n".join(current).strip()
        if text:
            blocks.append(Block(text=text, page=page, is_table=in_table))
        current = []

    for line in markdown.splitlines():
        is_row = bool(_TABLE_ROW.match(line))
        if is_row != in_table:
            flush()
            in_table = is_row
        if not line.strip():
            if not in_table:
                flush()
            continue
        if _HEADING.match(line) and not in_table:
            flush()
            blocks.append(Block(text=line.strip(), page=page))
            continue
        current.append(line)
    flush()
    return blocks


def _split_table(block: Block, max_tokens: int) -> list[Block]:
    lines = block.text.splitlines()
    has_header = len(lines) > 1 and bool(_TABLE_DIVIDER.match(lines[1]))
    header = lines[:2] if has_header else []
    rows = lines[2:] if has_header else lines
    pieces: list[Block] = []
    current: list[str] = []
    for row in rows:
        candidate = "\n".join([*header, *current, row])
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(Block("\n".join([*header, *current]), block.page, is_table=True))
            current = []
        current.append(row)
    if current:
        pieces.append(Block("\n".join([*header, *current]), block.page, is_table=True))
    return pieces


def _split_text(block: Block, max_tokens: int) -> list[Block]:
    """Split an oversized paragraph on line and sentence boundaries (hard-wrap as a last resort)."""
    sentences = [s for line in block.text.splitlines() for s in re.split(r"(?<=[.!?])\s+", line)]
    pieces: list[Block] = []
    current = ""
    for sentence in sentences:
        while estimate_tokens(sentence) > max_tokens:  # one huge "sentence"
            cut = max_tokens * 4
            if current:
                pieces.append(Block(current, block.page))
                current = ""
            pieces.append(Block(sentence[:cut], block.page))
            sentence = sentence[cut:]  # noqa: PLW2901
        candidate = f"{current} {sentence}".strip()
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(Block(current, block.page))
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(Block(current, block.page))
    return pieces


def _fit(blocks: list[Block], max_tokens: int) -> list[Block]:
    fitted: list[Block] = []
    for block in blocks:
        if estimate_tokens(block.text) <= max_tokens:
            fitted.append(block)
        elif block.is_table:
            fitted.extend(_split_table(block, max_tokens))
        else:
            fitted.extend(_split_text(block, max_tokens))
    return fitted


def context_header(metadata: DocMetadata, page_start: int, page_end: int) -> str:
    """One-line document context prepended before embedding ("contextual retrieval")."""
    pages = f"p{page_start}" if page_start == page_end else f"p{page_start}-{page_end}"
    parts = [
        metadata.doc_type.replace("_", " ").title(),
        metadata.title,
        metadata.institution,
        metadata.person,
        metadata.date,
        pages,
    ]
    return " · ".join(p for p in parts if p)


def chunk_document(
    doc: ParsedDocument, config: ChunkingConfig = DEFAULT_CHUNKING, *, contextual: bool = True
) -> list[Chunk]:
    blocks = [b for page in doc.pages for b in split_blocks(page.markdown, page.number)]
    blocks = _fit(blocks, config.max_tokens)

    groups: list[list[Block]] = []
    current: list[Block] = []
    for block in blocks:
        size = estimate_tokens("\n\n".join(b.text for b in [*current, block]))
        if current and size > config.max_tokens:
            groups.append(current)
            # Overlap: carry trailing blocks of the previous chunk, up to overlap_tokens.
            carry: list[Block] = []
            for prev in reversed(current):
                if (
                    estimate_tokens("\n\n".join(b.text for b in [prev, *carry]))
                    > config.overlap_tokens
                ):
                    break
                carry.insert(0, prev)
            if estimate_tokens("\n\n".join(b.text for b in [*carry, block])) > config.max_tokens:
                carry = []
            current = carry
        current.append(block)
    if current:
        groups.append(current)

    chunks: list[Chunk] = []
    for ordinal, group in enumerate(groups):
        text = "\n\n".join(b.text for b in group)
        start, end = group[0].page, group[-1].page
        header = context_header(doc.metadata, start, end) if contextual else ""
        chunks.append(
            Chunk(
                chunk_id=f"{doc.doc_id}-{ordinal:04d}",
                doc_id=doc.doc_id,
                ordinal=ordinal,
                text=text,
                embed_text=f"{header}\n\n{text}" if header else text,
                page_start=start,
                page_end=end,
                token_estimate=estimate_tokens(text),
            )
        )
    return chunks
