import json

import pytest

from docqa import cli
from docqa.pipelines.ingest import IngestResult, Outcome
from docqa.pipelines.qa import QAService
from tests.fakes import (
    FakeEmbedder,
    FakeGenerator,
    FakeReranker,
    FakeSearcher,
    MemoryBlobStore,
    MemoryIndex,
    retrieved,
)


class StubService:
    def __init__(self, fail_on: str = "") -> None:
        self._blobs = MemoryBlobStore({"raw/a.pdf": b"", "raw/b.docx": b""})
        self._index = MemoryIndex()
        self.fail_on = fail_on

    def ingest(self, key: str) -> IngestResult:
        if key == self.fail_on:
            raise RuntimeError("boom")
        if key.endswith(".docx"):
            return IngestResult(key, Outcome.SKIPPED, reason="unsupported type .docx")
        return IngestResult(key, Outcome.INDEXED, chunks=2)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")


def use(monkeypatch: pytest.MonkeyPatch, service: StubService) -> None:
    monkeypatch.setattr(cli, "build_ingest_service", lambda _settings: service)


def test_ingest_all(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    use(monkeypatch, StubService())
    assert cli.main(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "indexed  raw/a.pdf  (2 chunks, 0 vision pages)" in out
    assert "skipped  raw/b.docx  (unsupported type .docx)" in out


def test_ingest_failure_sets_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, StubService(fail_on="raw/a.pdf"))
    assert cli.main(["ingest", "raw/a.pdf"]) == 1
    assert "FAILED   raw/a.pdf  (RuntimeError: boom)" in capsys.readouterr().err


def test_stats(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    use(monkeypatch, StubService())
    assert cli.main(["stats"]) == 0
    assert "chunks__struct400__titan1024: 0 chunks" in capsys.readouterr().out


def test_ask_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    qa = QAService(
        searcher=FakeSearcher(bm25=[retrieved("a")]),
        embedder=FakeEmbedder(),
        embedding_model_id="e",
        generator=FakeGenerator("GPA [1]"),
        reranker=FakeReranker(),
    )
    buckets: list[str] = []

    def build(_settings: object, bucket: str) -> QAService:
        buckets.append(bucket)
        return qa

    monkeypatch.setattr(cli, "build_qa_service", build)
    assert cli.main(["ask", "gpa?", "--strategy", "bm25"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["answer"] == "GPA [1]"
    assert out["strategy"] == "bm25"
    assert buckets == ["bucket"]
