import json

import pytest

from docqa.config import Settings, load_config


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
