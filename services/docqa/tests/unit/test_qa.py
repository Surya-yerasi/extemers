import pytest

from docqa.domain.answering import NOT_FOUND, NOT_FOUND_MESSAGE
from docqa.domain.retrieval import Strategy
from docqa.pipelines.qa import MAX_QUESTION_CHARS, QAService
from tests.fakes import FakeEmbedder, FakeGenerator, FakeReranker, FakeSearcher, retrieved


def service(
    searcher: FakeSearcher | None = None,
    generator: FakeGenerator | None = None,
    reranker: FakeReranker | None = None,
    embedder: FakeEmbedder | None = None,
) -> QAService:
    return QAService(
        searcher=searcher
        or FakeSearcher(
            dense=[retrieved("a"), retrieved("b"), retrieved("c")],
            bm25=[retrieved("c"), retrieved("d")],
        ),
        embedder=embedder or FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=generator or FakeGenerator(),
        reranker=reranker or FakeReranker(),
        candidates=20,
        top_k=2,
    )


def chunk_ids(qa: QAService, strategy: Strategy) -> list[str]:
    return [c.chunk_id for c in qa.retrieve("gpa?", strategy).chunks]


def test_dense_uses_only_embeddings() -> None:
    searcher = FakeSearcher(dense=[retrieved("a"), retrieved("b"), retrieved("c")])
    embedder = FakeEmbedder()
    qa = service(searcher=searcher, embedder=embedder)
    retrieval = qa.retrieve("  gpa?  ", Strategy.DENSE)
    assert [c.chunk_id for c in retrieval.chunks] == ["a", "b"]  # top_k = 2
    assert searcher.calls == ["dense"]
    assert embedder.texts == ["gpa?"]
    assert set(retrieval.timings_ms) == {"embed", "dense_search"}
    assert retrieval.cost.usd > 0


def test_bm25_skips_embedding() -> None:
    searcher = FakeSearcher(bm25=[retrieved("c"), retrieved("d")])
    embedder = FakeEmbedder()
    retrieval = service(searcher=searcher, embedder=embedder).retrieve("gpa", Strategy.BM25)
    assert [c.chunk_id for c in retrieval.chunks] == ["c", "d"]
    assert searcher.calls == ["bm25"]
    assert embedder.texts == []
    assert retrieval.cost.usd == 0
    assert retrieval.tokens == {}


def test_hybrid_fuses_both_lists() -> None:
    # c is in both lists (dense 3rd, bm25 1st), so RRF lifts it to the top.
    assert chunk_ids(service(), Strategy.HYBRID) == ["c", "a"]


def test_hybrid_rerank_reorders_fused_candidates_and_costs_a_query() -> None:
    reranker = FakeReranker()
    qa = service(reranker=reranker)
    retrieval = qa.retrieve("gpa", Strategy.HYBRID_RERANK)
    # Fused order is c, a, b, d (b and d tie; ties go by ID); the fake reranker reverses it.
    assert [c.chunk_id for c in retrieval.chunks] == ["d", "b"]
    assert retrieval.chunks[0].scores["rerank"] == 0.5
    assert retrieval.chunks[0].ranks["rerank"] == 1
    assert reranker.calls == 1
    assert "rerank" in retrieval.timings_ms
    assert retrieval.cost.usd >= 0.002


def test_rerank_with_no_candidates_costs_nothing() -> None:
    qa = service(searcher=FakeSearcher())
    assert qa.retrieve("gpa", Strategy.HYBRID_RERANK).cost.usd < 0.002


@pytest.mark.parametrize("question", ["", "   ", "x" * (MAX_QUESTION_CHARS + 1)])
def test_invalid_questions(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        service().retrieve(question, Strategy.DENSE)


def test_ask_returns_cited_answer_with_debug_data() -> None:
    generator = FakeGenerator("Your GPA was 3.86 [1].")
    result = service(generator=generator).ask("What GPA?", Strategy.HYBRID)
    assert result.answer == "Your GPA was 3.86 [1]."
    assert not result.not_found
    assert [c.chunk_id for c in result.citations] == ["c"]
    assert [c.chunk_id for c in result.chunks] == ["c", "a"]
    assert result.tokens["generate_input"] == 1000
    assert result.tokens["generate_output"] == 100
    assert {"embed", "dense_search", "bm25_search", "fuse", "generate", "total"} <= set(
        result.timings_ms
    )
    assert result.models == {
        "embedding": "amazon.titan-embed-text-v2:0",
        "generation": "us.amazon.nova-micro-v1:0",
    }
    assert result.cost.usd > 0
    assert "[1] Doc d1 (p1)\ntext of c" in generator.prompts[0]
    assert generator.prompts[0].endswith("Question: What GPA?")


def test_ask_reports_rerank_model() -> None:
    result = service().ask("q", Strategy.HYBRID_RERANK)
    assert result.models["rerank"] == "cohere.rerank-v3-5:0"


def test_ask_model_says_not_found() -> None:
    result = service(generator=FakeGenerator(NOT_FOUND)).ask("Who is my dentist?", Strategy.DENSE)
    assert result.not_found
    assert result.answer == NOT_FOUND_MESSAGE
    assert result.citations == []


def test_ask_with_empty_index_skips_the_model() -> None:
    generator = FakeGenerator()
    result = service(searcher=FakeSearcher(), generator=generator).ask("q", Strategy.HYBRID)
    assert result.not_found
    assert generator.prompts == []
    assert "generate" not in result.timings_ms
