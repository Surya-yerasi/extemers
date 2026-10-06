import pytest

from docqa.domain.retrieval import RRF_K, RetrievedChunk, ranked, rrf_fuse
from tests.fakes import retrieved


def ids(chunks: list[RetrievedChunk]) -> list[str]:
    return [c.chunk_id for c in chunks]


def test_ranked_records_one_based_rank_per_stage() -> None:
    out = ranked([retrieved("a"), retrieved("b")], "dense")
    assert [c.ranks for c in out] == [{"dense": 1}, {"dense": 2}]


def test_rrf_rewards_agreement_between_lists() -> None:
    dense = ranked([retrieved("a"), retrieved("b"), retrieved("c")], "dense")
    bm25 = ranked([retrieved("c"), retrieved("b"), retrieved("d")], "bm25")
    fused = rrf_fuse([dense, bm25], limit=10)
    # c (ranks 3 and 1) edges out b (2 and 2): 1/63 + 1/61 > 2/62. a and d appear once.
    assert ids(fused) == ["c", "b", "a", "d"]
    b = next(c for c in fused if c.chunk_id == "b")
    assert b.scores["rrf"] == pytest.approx(2 / (RRF_K + 2))
    assert b.ranks["dense"] == 2
    assert b.ranks["bm25"] == 2


def test_rrf_exact_order_and_merged_scores() -> None:
    dense = ranked(
        [retrieved("a").model_copy(update={"scores": {"dense": 0.9}}), retrieved("b")], "dense"
    )
    bm25 = ranked([retrieved("a").model_copy(update={"scores": {"bm25": 7.0}})], "bm25")
    fused = rrf_fuse([dense, bm25], limit=10)
    assert ids(fused) == ["a", "b"]
    assert fused[0].scores == {"dense": 0.9, "bm25": 7.0, "rrf": pytest.approx(2 / (RRF_K + 1))}
    assert fused[0].ranks == {"dense": 1, "bm25": 1, "rrf": 1}
    assert fused[1].ranks == {"dense": 2, "rrf": 2}


def test_rrf_ties_are_deterministic_and_limit_applies() -> None:
    fused = rrf_fuse([[retrieved("z")], [retrieved("y")], [retrieved("x")]], limit=2)
    assert ids(fused) == ["x", "y"]


def test_rrf_empty() -> None:
    assert rrf_fuse([[], []], limit=5) == []
