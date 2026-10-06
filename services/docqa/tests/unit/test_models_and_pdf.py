from docqa.adapters.pdf import extract_page_texts, layout_to_markdown, render_page_png
from docqa.domain.models import content_hash, doc_id_for
from tests.conftest_samples import sample


def test_doc_id_is_stable_and_path_based() -> None:
    assert doc_id_for("raw/a.pdf") == doc_id_for("raw/a.pdf")
    assert doc_id_for("raw/a.pdf") != doc_id_for("raw/b.pdf")
    assert len(doc_id_for("raw/a.pdf")) == 16


def test_content_hash() -> None:
    assert content_hash(b"x") != content_hash(b"y")


def test_layout_rows_become_markdown_table() -> None:
    text = "Title line\n  Term      Course     Grade\n  Fall 2017      CS 101      A\nTotal:  done"
    assert layout_to_markdown(text) == (
        "Title line\n| Term | Course | Grade |\n| --- | --- | --- |\n| Fall 2017 | CS 101 | A |\n"
        "Total: done"
    )


def test_single_aligned_line_is_not_a_table() -> None:
    assert layout_to_markdown("Name      Alex      Rivera") == "Name   Alex   Rivera"


def test_transcript_text_layer_keeps_table_rows() -> None:
    [page] = extract_page_texts(sample("transcript_northfield_state.pdf"))
    assert "| Fall 2018 | CS 310 | Algorithms | 4 | A |" in page
    assert "Cumulative GPA: 3.86" in page


def test_scanned_pdf_has_no_text_layer() -> None:
    assert extract_page_texts(sample("scholarship_award_letter_scanned.pdf")) == [""]


def test_render_page_png() -> None:
    png = render_page_png(sample("scholarship_award_letter_scanned.pdf"), 0)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
