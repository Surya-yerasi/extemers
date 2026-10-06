import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from docqa.config import IngestSettings, Provider, QASettings, Settings, load_config


def test_loads_from_ssm_parameter() -> None:
    stored = {
        "cognito_domain": "https://d",
        "cognito_client_id": "cid",
        "cognito_issuer": "https://iss",
        "allowed_redirect_uris": ["https://app/callback"],
        "docs_bucket": "bucket",
    }
    requested: list[str] = []

    def read(name: str) -> str:
        requested.append(name)
        return json.dumps(stored)

    config = load_config(Settings(environment="dev", config_parameter="/docqa/dev/config"), read)
    assert requested == ["/docqa/dev/config"]
    assert config.environment == "dev"
    assert config.cognito_client_id == "cid"
    assert config.docs_bucket == "bucket"


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCQA_COGNITO_DOMAIN", "https://d")
    monkeypatch.setenv("DOCQA_COGNITO_CLIENT_ID", "cid")
    monkeypatch.setenv("DOCQA_COGNITO_ISSUER", "https://iss")
    monkeypatch.setenv("DOCQA_ALLOWED_REDIRECT_URIS", '["http://localhost:8080/callback"]')
    config = load_config(Settings(), lambda _name: pytest.fail("SSM must not be called"))
    assert config.environment == "local"
    assert config.allowed_redirect_uris == ["http://localhost:8080/callback"]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("DOCQA_PROVIDER", "DOCQA_DOCS_BUCKET", "DOCQA_INDEX_TABLE", "DOCQA_LANCEDB_URI"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_bedrock_is_the_default_and_needs_a_bucket(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="DOCQA_DOCS_BUCKET"):
        IngestSettings()
    settings = IngestSettings(docs_bucket="b")
    assert settings.provider is Provider.BEDROCK
    assert settings.index_table == "chunks__struct400__titan1024"
    assert settings.resolved_lancedb_uri == "s3://b/lancedb"


def test_local_mode_switches_models_table_and_storage(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DOCQA_PROVIDER", "local")
    ingest = IngestSettings()  # no bucket needed
    assert ingest.index_table == "chunks__struct400__bgem3_1024"
    assert ingest.embedding_model_id == "bge-m3"
    assert ingest.vision_model_id == "qwen2.5vl:7b"
    assert ingest.resolved_lancedb_uri == str(Path(".data") / "lancedb")
    qa = QASettings()
    assert qa.generation_model_id == "qwen2.5:7b"
    assert qa.lancedb_uri_for(None) == str(Path(".data") / "lancedb")


def test_local_mode_keeps_explicit_values(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DOCQA_PROVIDER", "local")
    clean_env.setenv("DOCQA_INDEX_TABLE", "chunks__custom")
    settings = QASettings(generation_model_id="llama3.2:3b", lancedb_uri="/tmp/x")  # noqa: S108
    assert settings.index_table == "chunks__custom"
    assert settings.generation_model_id == "llama3.2:3b"
    assert settings.embedding_model_id == "bge-m3"
    assert settings.lancedb_uri_for("ignored") == "/tmp/x"  # noqa: S108


def test_bedrock_qa_needs_a_bucket_for_the_index(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="bucket is required"):
        QASettings().lancedb_uri_for(None)
