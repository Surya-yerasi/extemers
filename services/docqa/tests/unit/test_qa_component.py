"""Ingest the synthetic corpus into a real LanceDB table on disk, then ask questions through
the real searcher. Only the models are fakes, so this exercises parsing, chunking, storage,
BM25 and fusion end to end without Bedrock."""

import pytest

from docqa.adapters.lancedb_index import LanceChunkIndex, LanceChunkSearcher
from docqa.domain.retrieval import Strategy
from docqa.pipelines.ingest import IngestService
from docqa.pipelines.qa import QAService
from tests.conftest_samples import sample
from tests.fakes import (
    FakeEmbedder,
    FakeGenerator,
    FakeMetadata,
    FakeReranker,
    FakeVision,
    MemoryBlobStore,
)


@pytest.fixture(scope="module")
def qa(tmp_path_factory: pytest.TempPathFactory) -> QAService:
    uri = str(tmp_path_factory.mktemp("lancedb"))
    embedder = FakeEmbedder()
    blobs = MemoryBlobStore(
        {
            "raw/transcript.pdf": sample("transcript_northfield_state.pdf"),
            "raw/degree.pdf": sample("degree_certificate_northfield_state.pdf"),
        }
    )
    ingest = IngestService(
        blobs=blobs,
        vision=FakeVision(),
        metadata=FakeMetadata(),
        embedder=embedder,
        index=LanceChunkIndex(uri, "chunks", embedder.dimensions),
    )
    for key in blobs.list_keys("raw/"):
        ingest.ingest(key)
    return QAService(
        searcher=LanceChunkSearcher(uri, "chunks"),
        embedder=embedder,
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=FakeGenerator("Cumulative GPA 3.86 [1]."),
        reranker=FakeReranker(),
        top_k=3,
    )


def test_bm25_finds_the_gpa_in_the_transcript(qa: QAService) -> None:
    result = qa.ask("What was my cumulative GPA?", Strategy.BM25)
    assert "Cumulative GPA: 3.86" in result.chunks[0].text
    assert result.citations[0].source_key == "raw/transcript.pdf"
    assert result.chunks[0].ranks == {"bm25": 1}


@pytest.mark.parametrize("strategy", list(Strategy))
def test_every_strategy_answers_over_a_real_index(qa: QAService, strategy: Strategy) -> None:
    result = qa.ask("Which degree was conferred?", strategy)
    assert 1 <= len(result.chunks) <= 3
    assert not result.not_found
    assert result.timings_ms["total"] > 0


def test_hybrid_keeps_scores_from_both_retrievers(qa: QAService) -> None:
    chunks = qa.retrieve("Cumulative GPA", Strategy.HYBRID).chunks
    # The fake embedder's vectors are arbitrary (and vary per process), so the dense rank is
    # too: find the GPA chunk rather than assuming its position.
    gpa = next(c for c in chunks if "Cumulative GPA" in c.text)
    assert gpa.ranks["bm25"] == 1
    assert set(gpa.scores) == {"dense", "bm25", "rrf"}
    assert set(gpa.ranks) == {"dense", "bm25", "rrf"}
