import base64
import hashlib
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from docqa.web.auth import AuthError, TokenVerifier, authorize_url, logout_url, pkce_pair
from tests.conftest import CLIENT_ID, ISSUER


def test_pkce_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert 43 <= len(verifier) <= 128  # RFC 7636 length bounds


def test_pkce_pairs_are_unique() -> None:
    assert pkce_pair()[0] != pkce_pair()[0]


def test_authorize_url_contains_pkce_and_state() -> None:
    url = authorize_url("https://d.example", "cid", "https://app/callback", "st", "ch")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://d.example/oauth2/authorize?")
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["ch"]
    assert query["state"] == ["st"]
    assert query["redirect_uri"] == ["https://app/callback"]


def test_logout_url() -> None:
    query = parse_qs(urlparse(logout_url("https://d.example", "cid", "https://app/")).query)
    assert query == {"client_id": ["cid"], "logout_uri": ["https://app/"]}


def test_valid_id_token(verifier: TokenVerifier, make_token: Callable[..., str]) -> None:
    user = verifier.verify_id_token(make_token())
    assert user.sub == "user-123"
    assert user.email == "me@example.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": 1},  # expired
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"token_use": "access"},
        {"sub": None},  # required claim missing
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "access-token", "missing-sub"],
)
def test_rejected_tokens(
    verifier: TokenVerifier, make_token: Callable[..., str], overrides: dict[str, object]
) -> None:
    with pytest.raises(AuthError):
        verifier.verify_id_token(make_token(**overrides))


def test_rejects_token_signed_by_another_key(make_token: Callable[..., str]) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    with pytest.raises(AuthError):
        TokenVerifier(ISSUER, CLIENT_ID, lambda _t: other).verify_id_token(make_token())


def test_rejects_garbage(verifier: TokenVerifier) -> None:
    with pytest.raises(AuthError):
        verifier.verify_id_token("not-a-jwt")
