"""CI regression gate: BM25 retrieval over the real synthetic corpus, scored against the
golden set. Exercises PDF parsing, chunking, the LanceDB full-text index and the metrics
together, with no model calls. A chunking or parsing change that hurts retrieval fails here.

Dense retrieval is not gated: CI has no embedding model (the fake vectors are random).
"""

import pytest

from docqa.adapters.lancedb_index import LanceChunkIndex, LanceChunkSearcher
from docqa.domain.retrieval import Strategy
from docqa.evals.dataset import load_golden
from docqa.evals.runner import run_eval, summarize
from docqa.pipelines.ingest import IngestService
from docqa.pipelines.qa import QAService
from tests.conftest_samples import SAMPLES
from tests.fakes import (
    FakeEmbedder,
    FakeGenerator,
    FakeMetadata,
    FakeReranker,
    FakeVision,
    MemoryBlobStore,
)
from tests.unit.evals.test_dataset import GOLDEN, VISION_ONLY


@pytest.fixture(scope="module")
def qa(tmp_path_factory: pytest.TempPathFactory) -> QAService:
    uri = str(tmp_path_factory.mktemp("lancedb"))
    embedder = FakeEmbedder()
    blobs = MemoryBlobStore(
        {f"raw/samples/{p.name}": p.read_bytes() for p in SAMPLES.glob("*.pdf")}
    )
    ingest = IngestService(
        blobs=blobs,
        vision=FakeVision(),  # scanned pages get placeholder text, so their questions are skipped
        metadata=FakeMetadata(),
        embedder=embedder,
        index=LanceChunkIndex(uri, "chunks", embedder.dimensions),
    )
    for key in blobs.list_keys("raw/"):
        ingest.ingest(key)
    return QAService(
        searcher=LanceChunkSearcher(uri, "chunks"),
        embedder=embedder,
        embedding_model_id="fake",
        generator=FakeGenerator(),
        reranker=FakeReranker(),
    )


def test_bm25_retrieval_quality_does_not_regress(qa: QAService) -> None:
    items = [
        i
        for i in load_golden(GOLDEN)
        if i.answerable and not any(d in VISION_ONLY for e in i.evidence for d in e.docs)
    ]
    assert len(items) >= 35
    summary = summarize(run_eval(qa, items, [Strategy.BM25], generate=False))[0]
    # Measured 2026-10-06: see documents/06-docqa.md (Phase 4). Thresholds sit just below.
    assert summary.retrieval["hit@5"] is not None
    assert summary.retrieval["hit@5"] >= 0.95
    assert summary.retrieval["mrr"] is not None
    assert summary.retrieval["mrr"] >= 0.80
