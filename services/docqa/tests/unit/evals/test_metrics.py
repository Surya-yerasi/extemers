import pytest

from docqa.domain.answering import Citation
from docqa.domain.costs import CostEstimate
from docqa.domain.retrieval import RetrievedChunk, Strategy
from docqa.evals.dataset import Evidence, GoldenItem
from docqa.evals.metrics import (
    answer_scores,
    chunk_matches,
    contains,
    mean,
    normalize,
    percentile,
    retrieval_scores,
)
from docqa.pipelines.qa import AskResult
from tests.fakes import retrieved


def chunk(doc: str, text: str, cid: str | None = None) -> RetrievedChunk:
    return retrieved(cid or f"{doc}-{len(text)}", text, doc_id=doc).model_copy(
        update={"source_key": f"raw/samples/{doc}.pdf"}
    )


GPA = Evidence(docs=["transcript.pdf"], text="Cumulative GPA: 3.86")
MS_GPA = Evidence(docs=["ms.pdf"], text="graduate GPA: 3.72")


def item(*evidence: Evidence, **kw: object) -> GoldenItem:
    return GoldenItem(
        id="x", category="fact", question="q?", answer="a", evidence=list(evidence), **kw
    )


def test_normalize_handles_markdown_and_line_wraps() -> None:
    assert normalize("## Date\n  issued:   **June**") == "date issued: june"


def test_chunk_matches_needs_right_document_and_text() -> None:
    assert chunk_matches(GPA, chunk("transcript", "... cumulative\nGPA: 3.86 on a 4.00 scale"))
    assert not chunk_matches(GPA, chunk("degree", "Cumulative GPA: 3.86"))  # wrong document
    assert not chunk_matches(GPA, chunk("transcript", "GPA 3.86"))  # text absent


def test_retrieval_scores_single_fact() -> None:
    chunks = [chunk("degree", "BSc"), chunk("transcript", "Cumulative GPA: 3.86"), chunk("x", "")]
    scores = retrieval_scores(item(GPA), chunks)
    assert scores["mrr"] == 0.5
    assert (scores["hit@1"], scores["hit@3"], scores["hit@5"]) == (0.0, 1.0, 1.0)
    assert scores["recall@1"] == 0.0
    assert scores["ndcg@3"] == pytest.approx(1 / 1.5849625, rel=1e-6)  # 1/log2(3)


def test_retrieval_scores_multi_fact_counts_each_fact_once() -> None:
    gpa_a = chunk("transcript", "Cumulative GPA: 3.86", "a")
    gpa_b = chunk("transcript", "Cumulative GPA: 3.86 again", "b")  # same fact twice
    ms = chunk("ms", "Cumulative graduate GPA: 3.72", "c")
    scores = retrieval_scores(item(GPA, MS_GPA), [gpa_a, gpa_b, ms])
    assert scores["recall@1"] == 0.5
    assert scores["recall@3"] == 1.0
    ideal = 1 + 1 / 1.5849625
    assert scores["ndcg@3"] == pytest.approx((1 + 1 / 2) / ideal, rel=1e-6)


def test_retrieval_scores_nothing_found() -> None:
    scores = retrieval_scores(item(GPA), [])
    assert scores["mrr"] == 0.0
    assert scores["hit@5"] == 0.0
    assert scores["ndcg@5"] == 0.0


@pytest.mark.parametrize(
    ("answer", "expected", "found"),
    [
        ("Your salary was $78,500.", "78,500", True),
        ("Your salary was $78500.", "78,500", True),
        ("You received an A [1].", "A", True),
        ("You received a B+ [1].", "A", False),  # the article "a" is not the grade "A"
        ("You received an A- [1].", "A", False),  # A- is a different grade
        ("You received an A- [1].", "A-", True),
        ("Grade B+.", "B+", True),
        ("Grade B.", "B+", False),
        ("magna cum laude", "Magna Cum Laude", True),
        ("Score 18.0", "8.0", False),
    ],
)
def test_contains(answer: str, expected: str, found: bool) -> None:
    assert contains(answer, expected) is found


def result(answer: str, not_found: bool = False, cited: tuple[str, ...] = ()) -> AskResult:
    return AskResult(
        strategy=Strategy.HYBRID,
        answer=answer,
        not_found=not_found,
        citations=[
            Citation(
                number=i,
                chunk_id=f"c{i}",
                doc_id="d",
                source_key=f"raw/{doc}",
                title="",
                page_start=1,
                page_end=1,
            )
            for i, doc in enumerate(cited, 1)
        ],
        chunks=[],
        timings_ms={},
        tokens={},
        cost=CostEstimate(),
        models={},
    )


def test_answer_scores_answerable() -> None:
    golden = item(GPA, must_contain=["3.86", ["four", "4"]], must_not_contain=["3.72"])
    good = answer_scores(golden, result("GPA 3.86 over four years [1]", cited=("transcript.pdf",)))
    assert good == {
        "correct": 1.0,
        "refusal_correct": 1.0,
        "citation_hit": 1.0,
        "citation_precision": 1.0,
    }
    mixed = answer_scores(
        golden, result("3.86 and 3.72 in 4 years", cited=("transcript.pdf", "ms.pdf"))
    )
    assert mixed["correct"] == 0.0  # forbidden 3.72
    assert mixed["citation_precision"] == 0.5
    missing = answer_scores(golden, result("GPA 3.86"))  # neither "four" nor "4"
    assert missing["correct"] == 0.0
    assert missing["citation_precision"] is None
    refused = answer_scores(golden, result("not found", not_found=True))
    assert (refused["correct"], refused["refusal_correct"]) == (0.0, 0.0)


def test_answer_scores_unanswerable() -> None:
    golden = item()
    assert answer_scores(golden, result("n/a", not_found=True))["correct"] == 1.0
    made_up = answer_scores(golden, result("Your passport is X123 [1]", cited=("a.pdf",)))
    assert made_up == {
        "correct": 0.0,
        "refusal_correct": 0.0,
        "citation_hit": None,
        "citation_precision": None,
    }


def test_mean_and_percentile() -> None:
    assert mean([1.0, None, 0.0]) == 0.5
    assert mean([None]) is None
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
    assert percentile(values, 100) == 20.0
    assert percentile([], 95) is None
