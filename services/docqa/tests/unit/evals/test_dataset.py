"""The synthetic golden set must stay consistent with the generated corpus."""

from pathlib import Path

import pytest

from docqa.adapters.pdf import extract_page_texts
from docqa.evals.dataset import load_golden
from docqa.evals.metrics import normalize
from tests.conftest_samples import SAMPLES

GOLDEN = Path(__file__).resolve().parents[3] / "evals" / "golden" / "synthetic.jsonl"
VISION_ONLY = {"acp_membership_card.png", "scholarship_award_letter_scanned.pdf"}


def test_golden_set_loads_with_every_category() -> None:
    items = load_golden(GOLDEN)
    assert len(items) >= 40
    assert {i.category for i in items} == {
        "fact", "table", "paraphrase", "multi_doc", "unanswerable"
    }  # fmt: skip
    for i in items:
        assert i.answerable == (i.category != "unanswerable"), i.id
        assert i.must_contain or not i.answerable, i.id


def test_every_evidence_text_is_in_its_document() -> None:
    texts = {
        p.name: normalize("\n".join(extract_page_texts(p.read_bytes())))
        for p in SAMPLES.glob("*.pdf")
        if p.name not in VISION_ONLY
    }
    for item in load_golden(GOLDEN):
        for ev in item.evidence:
            for doc in ev.docs:
                assert (SAMPLES / doc).exists(), f"{item.id}: {doc} missing"
                if doc in texts:
                    assert normalize(ev.text) in texts[doc], f"{item.id}: {ev.text!r} not in {doc}"


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    line = '{"id": "a", "category": "fact", "question": "q", "answer": "a"}\n'
    path = tmp_path / "g.jsonl"
    path.write_text(line + "\n" + line)
    with pytest.raises(ValueError, match="duplicate golden ids: a"):
        load_golden(path)
