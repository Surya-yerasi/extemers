from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from docqa.adapters.emf_metrics import EmfMetrics, NoopMetrics
from docqa.config import AppConfig
from docqa.pipelines.chat import ChatService
from docqa.pipelines.qa import AskResult
from docqa.ports import Generation, ModelUnavailableError
from docqa.web.app import create_app
from docqa.web.auth import OAUTH_COOKIE, SESSION_COOKIE, TokenVerifier
from tests.conftest import CLIENT_ID, DOMAIN
from tests.fakes import FakeGenerator, chat_service


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
def chat() -> ChatService:
    return chat_service(FakeGenerator("GPA 3.86 [1]."))


class RecordingMetrics:
    def __init__(self) -> None:
        self.turns: list[str] = []
        self.errors: list[tuple[str, str]] = []

    def turn(self, trace_id: str, result: AskResult) -> None:
        self.turns.append(trace_id)

    def model_error(self, trace_id: str, error_code: str) -> None:
        self.errors.append((trace_id, error_code))


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def api(
    config: AppConfig,
    verifier: TokenVerifier,
    make_token: Callable[..., str],
    chat: ChatService,
    metrics: RecordingMetrics,
) -> TestClient:
    client = TestClient(
        create_app(config, verifier, chat=chat, metrics=metrics), follow_redirects=False
    )
    client.cookies.set(SESSION_COOKIE, make_token())
    return client


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/ask"),
        ("get", "/api/conversations"),
        ("get", "/api/conversations/c_0123456789abcdef"),
        ("delete", "/api/conversations/c_0123456789abcdef"),
        ("get", "/api/traces/c_0123456789abcdef.1"),
    ],
)
def test_conversation_api_requires_login(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(
        path, **({"json": {"question": "q"}} if method == "post" else {})
    )
    assert response.status_code == 401


def test_ask_starts_a_conversation_with_a_traced_turn(
    api: TestClient, metrics: RecordingMetrics
) -> None:
    response = api.post(
        "/api/ask",
        json={"question": "  gpa?  ", "strategy": "dense"},
        headers={
            "x-amzn-trace-id": "Root=1-abc",
            "x-amzn-lambda-context": '{"request_id": "req-1"}',
        },
    )
    assert response.status_code == 200
    body = response.json()
    conversation_id = body["conversation"]["conversation_id"]
    assert body["conversation"]["title"] == "gpa?"
    assert body["conversation"]["turns"] == 1
    turn = body["turn"]
    assert turn["turn_id"] == f"{conversation_id}.1"
    assert turn["result"]["answer"] == "GPA 3.86 [1]."
    assert turn["result"]["citations"][0]["chunk_id"] == "a"
    assert turn["result"]["trace"] is None  # stored once, on the turn
    trace = turn["trace"]
    assert trace["trace_id"] == turn["turn_id"]
    assert [s["name"] for s in trace["spans"]] == ["embed", "dense_search", "generate"]
    assert trace["attributes"]["conversation_id"] == conversation_id
    assert trace["attributes"]["xray_trace_id"] == "Root=1-abc"
    assert trace["attributes"]["lambda_request_id"] == "req-1"
    assert metrics.turns == [turn["turn_id"]]


def test_follow_up_is_rewritten_and_listed(api: TestClient, chat: ChatService) -> None:
    first = api.post("/api/ask", json={"question": "What was my GPA?"}).json()
    conversation_id = first["conversation"]["conversation_id"]
    second = api.post(
        "/api/ask", json={"question": "And for my master's?", "conversation_id": conversation_id}
    ).json()
    assert second["turn"]["turn_id"] == f"{conversation_id}.2"
    assert second["turn"]["result"]["standalone_question"] == "What was my master's GPA?"
    assert second["turn"]["trace"]["spans"][0]["name"] == "rewrite"

    listed = api.get("/api/conversations").json()
    assert [c["conversation_id"] for c in listed] == [conversation_id]
    assert listed[0]["turns"] == 2
    full = api.get(f"/api/conversations/{conversation_id}").json()
    assert [t["question"] for t in full["turns"]] == ["What was my GPA?", "And for my master's?"]
    trace = api.get(f"/api/traces/{conversation_id}.2").json()
    assert trace["question"] == "And for my master's?"


def test_conversations_are_private_to_their_owner(
    api: TestClient, make_token: Callable[..., str]
) -> None:
    conversation_id = api.post("/api/ask", json={"question": "gpa?"}).json()["conversation"][
        "conversation_id"
    ]
    api.cookies.set(SESSION_COOKIE, make_token(sub="someone-else"))
    assert api.get("/api/conversations").json() == []
    assert api.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert api.get(f"/api/traces/{conversation_id}.1").status_code == 404
    assert api.delete(f"/api/conversations/{conversation_id}").status_code == 404
    follow_up = {"question": "q", "conversation_id": conversation_id}
    assert api.post("/api/ask", json=follow_up).status_code == 404


def test_delete_conversation(api: TestClient) -> None:
    conversation_id = api.post("/api/ask", json={"question": "gpa?"}).json()["conversation"][
        "conversation_id"
    ]
    assert api.delete(f"/api/conversations/{conversation_id}").status_code == 204
    assert api.get("/api/conversations").json() == []
    assert api.get(f"/api/conversations/{conversation_id}").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/conversations/not-an-id",
        "/api/conversations/c_0123456789abcdef",
        "/api/traces/c_0123456789abcdef.1",
        "/api/traces/c_0123456789abcdef",
        "/api/traces/..%2F..%2Fsecret",
    ],
)
def test_unknown_or_malformed_ids_are_404(api: TestClient, path: str) -> None:
    assert api.get(path).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "x" * 1001},
        {"question": "gpa", "strategy": "magic"},
        {},
        {"question": "gpa", "conversation_id": "x" * 65},
    ],
)
def test_ask_validates_input(api: TestClient, chat: ChatService, payload: dict[str, Any]) -> None:
    assert api.post("/api/ask", json=payload).status_code == 422
    assert api.get("/api/conversations").json() == []


def test_whitespace_question_is_rejected_without_a_turn(api: TestClient) -> None:
    response = api.post("/api/ask", json={"question": "   "})
    assert response.status_code == 422
    assert response.json() == {"detail": "question is empty"}
    assert api.get("/api/conversations").json() == []


def test_full_conversation_is_rejected(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docqa.pipelines.chat.MAX_TURNS", 1)
    conversation_id = api.post("/api/ask", json={"question": "a"}).json()["conversation"][
        "conversation_id"
    ]
    response = api.post("/api/ask", json={"question": "b", "conversation_id": conversation_id})
    assert response.status_code == 409


class FailingGenerator(FakeGenerator):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "status", "detail", "code"),
    [
        (
            ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "Converse"),
            503,
            "model quota reached; try again later",
            "ThrottlingException",
        ),
        (
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "Converse"),
            502,
            "upstream service error",
            "AccessDeniedException",
        ),
        (
            ModelUnavailableError("Ollama model qwen2.5:7b is missing (ollama pull qwen2.5:7b)"),
            503,
            "Ollama model qwen2.5:7b is missing (ollama pull qwen2.5:7b)",
            "ModelUnavailable",
        ),
        (httpx.ConnectTimeout("slow"), 502, "model service error", "ConnectTimeout"),
    ],
)
def test_model_failures_are_mapped_saved_and_traced(  # noqa: PLR0913, PLR0917
    config: AppConfig,
    verifier: TokenVerifier,
    make_token: Callable[..., str],
    metrics: RecordingMetrics,
    exc: Exception,
    status: int,
    detail: str,
    code: str,
) -> None:
    app = create_app(config, verifier, chat=chat_service(FailingGenerator(exc)), metrics=metrics)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, make_token())
    response = client.post("/api/ask", json={"question": "gpa?"})
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    trace_id = response.headers["x-docqa-trace-id"]
    assert metrics.errors == [(trace_id, code)]
    failed = client.get(f"/api/traces/{trace_id}").json()
    assert failed["result"] is None
    assert failed["error"].startswith(type(exc).__name__)
    generate = failed["trace"]["spans"][-1]
    assert (generate["name"], generate["status"]) == ("generate", "error")


def test_unexpected_errors_are_not_swallowed(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    app = create_app(config, verifier, chat=chat_service(FailingGenerator(KeyError("bug"))))
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.post("/api/ask", json={"question": "gpa?"}).status_code == 500


def test_chat_service_is_built_lazily_from_config(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    app = create_app(config, verifier)
    built: list[str | None] = []

    def factory(bucket: str | None) -> ChatService:
        built.append(bucket)
        return chat_service()

    app.state.chat_factory = factory
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.get("/health").status_code == 200
    assert built == []  # not built for health checks
    client.post("/api/ask", json={"question": "q"})
    client.get("/api/conversations")
    assert built == ["docqa-test-bucket"]  # built once


def test_unavailable_index_is_503(
    config: AppConfig, verifier: TokenVerifier, make_token: Callable[..., str]
) -> None:
    app = create_app(config, verifier)

    def factory(bucket: str | None) -> ChatService:
        raise ValueError("a documents bucket is required")

    app.state.chat_factory = factory
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, make_token())
    assert client.get("/api/conversations").status_code == 503


def test_metrics_default_to_emf_only_in_lambda(
    config: AppConfig, verifier: TokenVerifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert isinstance(create_app(config, verifier).state.metrics, NoopMetrics)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "docqa-dev-web")
    assert isinstance(create_app(config, verifier).state.metrics, EmfMetrics)


def test_index_renders_chat_and_traces(client: TestClient, make_token: Callable[..., str]) -> None:
    client.cookies.set(SESSION_COOKIE, make_token())
    page = client.get("/").text
    for marker in ('id="composer"', 'id="conversation-list"', 'id="view-traces"', 'id="waterfall"'):
        assert marker in page
    assert '<option value="hybrid" selected>' in page
    assert 'value="hybrid_rerank"' in page
    assert '<script src="/static/app.js"' in page


def test_static_assets_are_served(client: TestClient) -> None:
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "textContent" in response.text
    assert ".innerHTML" not in response.text
