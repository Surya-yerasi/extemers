import pytest

from docqa.domain.answering import (
    NOT_FOUND,
    NOT_FOUND_MESSAGE,
    SYSTEM_PROMPT,
    build_user_prompt,
    parse_answer,
    source_label,
)
from docqa.domain.costs import CostEstimate, rerank_cost, token_cost
from tests.fakes import retrieved

CHUNKS = [retrieved("c1", "GPA 3.86", doc_id="t"), retrieved("c2", "BSc conferred", doc_id="d")]


def test_system_prompt_demands_citations_and_not_found() -> None:
    assert NOT_FOUND in SYSTEM_PROMPT
    assert "[1]" in SYSTEM_PROMPT


def test_source_label_uses_title_or_file_name_and_pages() -> None:
    assert source_label(CHUNKS[0]) == "Doc t (p1)"
    spread = retrieved("x").model_copy(update={"title": "", "page_end": 3})
    assert source_label(spread) == "d1.pdf (pp1-3)"


def test_user_prompt_numbers_sources_in_order() -> None:
    prompt = build_user_prompt("What GPA?", CHUNKS)
    assert prompt.index("[1] Doc t (p1)\nGPA 3.86") < prompt.index("[2] Doc d (p1)")
    assert prompt.endswith("Question: What GPA?")


def test_parse_answer_maps_citations_once_each() -> None:
    parsed = parse_answer("  GPA 3.86 [1], degree [2][1]. Also [7].  ", CHUNKS)
    assert parsed.text == "GPA 3.86 [1], degree [2][1]. Also [7]."
    assert [c.number for c in parsed.citations] == [1, 2]  # [7] is out of range
    assert parsed.citations[0].chunk_id == "c1"
    assert parsed.citations[1].source_key == "raw/d.pdf"
    assert not parsed.not_found


def test_parse_answer_not_found() -> None:
    for raw in (NOT_FOUND, f"{NOT_FOUND}.", "", "   "):
        parsed = parse_answer(raw, CHUNKS)
        assert parsed.not_found
        assert parsed.text == NOT_FOUND_MESSAGE
        assert parsed.citations == []


def test_token_cost_strips_profile_prefix() -> None:
    cost = token_cost("us.amazon.nova-micro-v1:0", 1000, 1000)
    assert cost.usd == pytest.approx(0.000035 + 0.00014)
    assert cost.unpriced_models == []


def test_unknown_models_are_flagged_not_guessed() -> None:
    assert token_cost("some.new-model", 10**6).model_dump() == {
        "usd": 0.0,
        "unpriced_models": ["some.new-model"],
    }
    assert rerank_cost("other-reranker").unpriced_models == ["other-reranker"]


def test_cost_add_merges() -> None:
    total = (
        CostEstimate(usd=0.001, unpriced_models=["b"])
        .add(rerank_cost("cohere.rerank-v3-5:0"))
        .add(CostEstimate(unpriced_models=["a", "b"]))
    )
    assert total.usd == pytest.approx(0.003)
    assert total.unpriced_models == ["a", "b"]
