from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from docqa.web.auth import OAUTH_COOKIE, SESSION_COOKIE
from tests.conftest import CLIENT_ID, DOMAIN


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
