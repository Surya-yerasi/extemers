from docqa.domain.chunking import (
    ChunkingConfig,
    chunk_document,
    context_header,
    estimate_tokens,
    split_blocks,
)
from docqa.domain.models import DocMetadata, ExtractionMethod, Page, ParsedDocument

TABLE = "| Course | Grade |\n| --- | --- |\n" + "\n".join(
    f"| Course number {i} with a long descriptive title | A |" for i in range(40)
)


def doc(*pages: str, metadata: DocMetadata | None = None) -> ParsedDocument:
    return ParsedDocument(
        doc_id="d1",
        source_key="raw/x.pdf",
        content_hash="h",
        pages=[
            Page(number=i + 1, markdown=md, method=ExtractionMethod.TEXT)
            for i, md in enumerate(pages)
        ],
        metadata=metadata or DocMetadata(),
    )


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100


def test_split_blocks_separates_headings_paragraphs_tables() -> None:
    md = "# Title\nIntro line one\nIntro line two\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\nAfter"
    blocks = split_blocks(md, page=3)
    assert [b.text for b in blocks] == [
        "# Title",
        "Intro line one\nIntro line two",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
        "After",
    ]
    assert [b.is_table for b in blocks] == [False, False, True, False]
    assert {b.page for b in blocks} == {3}


def test_small_document_is_one_chunk() -> None:
    chunks = chunk_document(doc("# Diploma\n\nConferred on Alex Rivera."))
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "d1-0000"
    assert chunks[0].text == "# Diploma\n\nConferred on Alex Rivera."


def test_chunks_respect_max_tokens() -> None:
    paragraphs = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(12))
    chunks = chunk_document(doc(paragraphs), ChunkingConfig(max_tokens=200, overlap_tokens=0))
    assert len(chunks) > 1
    assert all(c.token_estimate <= 200 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_overlap_repeats_trailing_block() -> None:
    paragraphs = "\n\n".join(f"P{i} " + "x" * 300 for i in range(6))
    chunks = chunk_document(doc(paragraphs), ChunkingConfig(max_tokens=200, overlap_tokens=90))
    first_last_block = chunks[0].text.split("\n\n")[-1]
    assert chunks[1].text.startswith(first_last_block)


def test_large_table_split_repeats_header() -> None:
    chunks = chunk_document(doc(TABLE), ChunkingConfig(max_tokens=150, overlap_tokens=0))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("| Course | Grade |\n| --- | --- |")


def test_oversized_paragraph_splits_on_lines_and_sentences() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_document(doc(text), ChunkingConfig(max_tokens=100, overlap_tokens=0))
    assert len(chunks) > 1
    assert all(c.token_estimate <= 100 for c in chunks)


def test_unbreakable_text_is_hard_wrapped() -> None:
    chunks = chunk_document(doc("z" * 5000), ChunkingConfig(max_tokens=100, overlap_tokens=0))
    assert all(c.token_estimate <= 100 for c in chunks)
    assert "".join(c.text for c in chunks) == "z" * 5000


def test_page_ranges_span_pages() -> None:
    chunks = chunk_document(doc("Page one text.", "Page two text."))
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 2)


def test_contextual_header_in_embed_text_only() -> None:
    meta = DocMetadata(doc_type="transcript", institution="Northfield State", date="2019")
    chunk = chunk_document(doc("GPA 3.86", metadata=meta))[0]
    assert chunk.text == "GPA 3.86"
    assert chunk.embed_text == "Transcript · Northfield State · 2019 · p1\n\nGPA 3.86"


def test_contextual_off() -> None:
    meta = DocMetadata(doc_type="transcript")
    chunk = chunk_document(doc("GPA", metadata=meta), contextual=False)[0]
    assert chunk.embed_text == chunk.text


def test_context_header_page_range() -> None:
    assert context_header(DocMetadata(doc_type="degree_certificate"), 2, 3) == (
        "Degree Certificate · p2-3"
    )
