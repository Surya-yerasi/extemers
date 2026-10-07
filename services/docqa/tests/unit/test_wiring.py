from pathlib import Path

import pytest

from docqa.adapters.conversation_store import BlobConversationStore
from docqa.adapters.lancedb_index import LanceChunkIndex
from docqa.adapters.local_blobs import LocalBlobStore
from docqa.adapters.ollama import OllamaEmbedder, OllamaGenerator, PassthroughReranker
from docqa.adapters.s3_blobs import S3BlobStore
from docqa.config import IngestSettings, Provider, QASettings
from docqa.pipelines.wiring import build_chat_service, build_ingest_service, build_qa_service


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Building boto3 clients must not depend on the developer's AWS config or login.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("DOCQA_PROVIDER", raising=False)


def test_local_ingest_uses_files_and_ollama(tmp_path: Path) -> None:
    settings = IngestSettings(provider=Provider.LOCAL, local_data_dir=tmp_path)
    service = build_ingest_service(settings)
    assert isinstance(service._blobs, LocalBlobStore)
    assert isinstance(service._embedder, OllamaEmbedder)
    assert isinstance(service._index, LanceChunkIndex)


def test_local_qa_uses_ollama_and_no_rerank(tmp_path: Path) -> None:
    qa = build_qa_service(QASettings(provider=Provider.LOCAL, local_data_dir=tmp_path), None)
    assert isinstance(qa._generator, OllamaGenerator)
    assert isinstance(qa._reranker, PassthroughReranker)
    assert qa._embedding_model_id == "local/bge-m3"


def test_bedrock_wiring(tmp_path: Path) -> None:
    ingest = build_ingest_service(IngestSettings(docs_bucket="b", lancedb_uri=str(tmp_path)))
    assert isinstance(ingest._blobs, S3BlobStore)
    qa = build_qa_service(QASettings(lancedb_uri=str(tmp_path)), "b")
    assert qa._embedding_model_id == "amazon.titan-embed-text-v2:0"
    assert qa._reranker.model_id == "cohere.rerank-v3-5:0"


def test_bedrock_ingest_without_bucket_is_rejected() -> None:
    settings = IngestSettings.model_construct(provider=Provider.BEDROCK, docs_bucket=None)
    with pytest.raises(ValueError, match="bucket is required"):
        build_ingest_service(settings)


def test_chat_conversations_live_next_to_the_documents(tmp_path: Path) -> None:
    local = build_chat_service(QASettings(provider=Provider.LOCAL, local_data_dir=tmp_path), None)
    assert isinstance(local._store, BlobConversationStore)
    assert isinstance(local._store._blobs, LocalBlobStore)
    deployed = build_chat_service(QASettings(lancedb_uri=str(tmp_path)), "bucket")
    assert isinstance(deployed._store._blobs, S3BlobStore)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="bucket is required"):
        build_chat_service(QASettings(), None)
