import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docqa.config import AppConfig
from docqa.web.app import create_app
from docqa.web.auth import TokenVerifier

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT_ID = "test-client-id"
DOMAIN = "https://docqa-test.auth.us-east-1.amazoncognito.com"
BASE = "http://testserver/"


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def make_token(signing_key: rsa.RSAPrivateKey) -> Callable[..., str]:
    def make(**overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": "user-123",
            "email": "me@example.com",
            "aud": CLIENT_ID,
            "iss": ISSUER,
            "token_use": "id",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, signing_key, algorithm="RS256")

    return make


@pytest.fixture
def verifier(signing_key: rsa.RSAPrivateKey) -> TokenVerifier:
    public_key = signing_key.public_key()
    return TokenVerifier(ISSUER, CLIENT_ID, lambda _token: public_key)


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        environment="test",
        cognito_domain=DOMAIN,
        cognito_client_id=CLIENT_ID,
        cognito_issuer=ISSUER,
        allowed_redirect_uris=[f"{BASE}callback"],
        docs_bucket="docqa-test-bucket",
    )


@pytest.fixture
def token_endpoint() -> dict[str, Any]:
    """Configure the fake Cognito /oauth2/token response; records the last request."""
    return {"status": 200, "json": {}, "last_request": None}


@pytest.fixture
def client(
    config: AppConfig, verifier: TokenVerifier, token_endpoint: dict[str, Any]
) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        token_endpoint["last_request"] = request
        return httpx.Response(token_endpoint["status"], json=token_endpoint["json"])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TestClient(create_app(config, verifier, http), follow_redirects=False)
