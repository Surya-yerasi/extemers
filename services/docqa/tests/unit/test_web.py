from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from docqa.config import AppConfig
from docqa.pipelines.qa import QAService
from docqa.ports import Generation, ModelUnavailableError
from docqa.web.app import create_app
from docqa.web.auth import OAUTH_COOKIE, SESSION_COOKIE, TokenVerifier
from tests.conftest import CLIENT_ID, DOMAIN
from tests.fakes import FakeEmbedder, FakeGenerator, FakeReranker, FakeSearcher, retrieved


def start_login(client: TestClient) -> str:
    """Hit /login and return the state Cognito would echo back."""
    response = client.get("/login")
    assert response.status_code == 303
    return str(parse_qs(urlparse(response.headers["location"]).query)["state"][0])


def test_health_needs_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/health").headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in headers["content-security-policy"]


def test_page_redirects_to_login_without_session(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_api_returns_401_without_session(client: TestClient) -> None:
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.json() == {"error": "not authenticated"}


def test_invalid_session_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged")
    assert client.get("/api/me").status_code == 401


def test_login_redirects_to_cognito_with_pkce(client: TestClient) -> None:
    response = client.get("/login")
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}" == DOMAIN
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == ["http://testserver/callback"]
    assert query["code_challenge_method"] == ["S256"]
    cookie = response.cookies[OAUTH_COOKIE]
    assert cookie.startswith(query["state"][0] + ".")


def test_login_rejects_unexpected_host(client: TestClient) -> None:
    response = client.get("/login", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_callback_rejects_state_mismatch(client: TestClient) -> None:
    start_login(client)
    assert client.get("/callback", params={"code": "c", "state": "wrong"}).status_code == 400


def test_callback_rejects_missing_oauth_cookie(client: TestClient) -> None:
    assert client.get("/callback", params={"code": "c", "state": "s"}).status_code == 400


def test_callback_success_sets_session(
    client: TestClient, token_endpoint: dict[str, Any], make_token: Callable[..., str]
) -> None:
    state = start_login(client)
    token_endpoint["json"] = {"id_token": make_token()}

    response = client.get("/callback", params={"code": "auth-code", "state": state})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    sent = parse_qs(token_endpoint["last_request"].content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["auth-code"]
    assert sent["code_verifier"][0]  # PKCE verifier forwarded
    assert client.get("/api/me").json() == {"sub": "user-123", "email": "me@example.com"}


def test_callback_token_endpoint_error(client: TestClient, token_endpoint: dict[str, Any]) -> None:
    state = start_login(client)
    token_endpoint["status"] = 400
    assert client.get("/callback", params={"code": "c", "state": state}).status_code == 400


def test_callback_rejects_invalid_id_token(
    client: TestClient, token_endpoint: dict[str, Any], make_token: Callable[..., str]
) -> None:
    state = start_login(client)
    token_endpoint["json"] = {"id_token": make_token(aud="someone-else")}
    assert client.get("/callback", params={"code": "c", "state": state}).status_code == 400


def test_index_shows_signed_in_user(client: TestClient, make_token: Callable[..., str]) -> None:
    client.cookies.set(SESSION_COOKIE, make_token())
    response = client.get("/")
    assert response.status_code == 200
    assert "me@example.com" in response.text


def test_index_escapes_html_in_claims(client: TestClient, make_token: Callable[..., str]) -> None:
    client.cookies.set(SESSION_COOKIE, make_token(email="<script>x</script>"))
    assert "<script>x</script>" not in client.get("/").text


def test_logout_clears_session_and_redirects_to_cognito(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    client.cookies.set(SESSION_COOKIE, make_token())
    response = client.get("/logout")
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"{DOMAIN}/logout?")
    assert SESSION_COOKIE in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.fixture
def qa_client(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> tuple[TestClient, FakeGenerator]:
    generator = FakeGenerator("GPA 3.86 [1].")
    qa = QAService(
        searcher=FakeSearcher(dense=[retrieved("a")], bm25=[retrieved("a")]),
        embedder=FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=generator,
        reranker=FakeReranker(),
    )
    client = TestClient(create_app(config, verifier, qa=qa), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, make_token())
    return client, generator


def test_ask_requires_login(client: TestClient) -> None:
    response = client.post("/api/ask", json={"question": "gpa?"})
    assert response.status_code == 401


def test_ask_returns_answer_citations_and_debug(
    qa_client: tuple[TestClient, FakeGenerator],
) -> None:
    client, _ = qa_client
    response = client.post("/api/ask", json={"question": "gpa?", "strategy": "dense"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "GPA 3.86 [1]."
    assert body["strategy"] == "dense"
    assert body["citations"][0]["chunk_id"] == "a"
    assert body["chunks"][0]["scores"] == {"dense": 0.9}
    assert body["timings_ms"]["total"] >= 0
    assert body["cost"]["usd"] > 0


def test_ask_defaults_to_hybrid(qa_client: tuple[TestClient, FakeGenerator]) -> None:
    client, _ = qa_client
    assert client.post("/api/ask", json={"question": "gpa?"}).json()["strategy"] == "hybrid"


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "x" * 1001},
        {"question": "gpa", "strategy": "magic"},
        {},
    ],
)
def test_ask_validates_input(
    qa_client: tuple[TestClient, FakeGenerator], payload: dict[str, Any]
) -> None:
    client, generator = qa_client
    assert client.post("/api/ask", json=payload).status_code == 422
    assert generator.prompts == []


def test_ask_whitespace_question_is_rejected(qa_client: tuple[TestClient, FakeGenerator]) -> None:
    client, _ = qa_client
    response = client.post("/api/ask", json={"question": "   "})
    assert response.status_code == 422
    assert response.json() == {"detail": "question is empty"}


class RaisingGenerator(FakeGenerator):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        raise ClientError({"Error": {"Code": self.code, "Message": "no"}}, "Converse")


@pytest.mark.parametrize(
    ("code", "status", "detail"),
    [
        ("ThrottlingException", 503, "model quota reached; try again later"),
        ("AccessDeniedException", 502, "upstream service error"),
    ],
)
def test_ask_maps_bedrock_errors(
    qa_client: tuple[TestClient, FakeGenerator], code: str, status: int, detail: str
) -> None:
    client, _ = qa_client
    client.app.state.qa._generator = RaisingGenerator(code)  # type: ignore[attr-defined]
    response = client.post("/api/ask", json={"question": "gpa?"})
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_ask_builds_service_lazily_from_config(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    app = create_app(config, verifier)
    built: list[str] = []
    qa = QAService(
        searcher=FakeSearcher(),
        embedder=FakeEmbedder(),
        embedding_model_id="e",
        generator=FakeGenerator(),
        reranker=FakeReranker(),
    )

    def factory(bucket: str) -> QAService:
        built.append(bucket)
        return qa

    app.state.qa_factory = factory
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.get("/health").status_code == 200
    assert built == []  # not built for health checks
    client.post("/api/ask", json={"question": "q"})
    client.post("/api/ask", json={"question": "q"})
    assert built == ["docqa-test-bucket"]  # built once


def test_ask_without_bucket_is_unavailable(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    client = TestClient(create_app(config.model_copy(update={"docs_bucket": None}), verifier))
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.post("/api/ask", json={"question": "q"}).status_code == 503


def test_index_renders_ask_form(client: TestClient, make_token: Callable[..., str]) -> None:
    client.cookies.set(SESSION_COOKIE, make_token())
    page = client.get("/").text
    assert 'id="ask-form"' in page
    assert '<option value="hybrid" selected>' in page
    assert 'value="hybrid_rerank"' in page
    assert '<script src="/static/app.js"' in page


def test_static_assets_are_served(client: TestClient) -> None:
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "textContent" in response.text
    assert "innerHTML" not in response.text.replace("never innerHTML", "")


class UnavailableGenerator(FakeGenerator):
    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        raise ModelUnavailableError("Ollama model qwen2.5:7b is missing (ollama pull qwen2.5:7b)")


class BrokenGenerator(FakeGenerator):
    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        raise httpx.ConnectTimeout("slow")


@pytest.mark.parametrize(
    ("generator", "status", "detail"),
    [
        (
            UnavailableGenerator(),
            503,
            "Ollama model qwen2.5:7b is missing (ollama pull qwen2.5:7b)",
        ),
        (BrokenGenerator(), 502, "model service error"),
    ],
)
def test_ask_maps_local_model_errors(
    qa_client: tuple[TestClient, FakeGenerator],
    generator: FakeGenerator,
    status: int,
    detail: str,
) -> None:
    client, _ = qa_client
    client.app.state.qa._generator = generator  # type: ignore[attr-defined]
    response = client.post("/api/ask", json={"question": "gpa?"})
    assert response.status_code == status
    assert response.json() == {"detail": detail}
