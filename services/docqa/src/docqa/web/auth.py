"""Cognito OIDC login: authorization code + PKCE, public client, no stored secrets.

The session cookie holds Cognito's signed ID token. Every request re-verifies its signature
against Cognito's published keys (JWKS), so the app needs no signing secret of its own.
"""

import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import jwt

SESSION_COOKIE = "docqa_session"
OAUTH_COOKIE = "docqa_oauth"
OAUTH_COOKIE_MAX_AGE = 600  # seconds allowed to complete the hosted login


class AuthError(Exception):
    """Login could not be completed or a token is invalid."""


@dataclass(frozen=True)
class User:
    sub: str
    email: str
    expires_at: int


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(
    domain: str, client_id: str, redirect_uri: str, state: str, challenge: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{domain}/oauth2/authorize?{query}"


def logout_url(domain: str, client_id: str, logout_uri: str) -> str:
    return f"{domain}/logout?{urlencode({'client_id': client_id, 'logout_uri': logout_uri})}"


# Given a raw JWT, return the public key that signed it.
KeyResolver = Callable[[str], Any]


def jwks_key_resolver(issuer: str) -> KeyResolver:
    client = jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)
    return lambda token: client.get_signing_key_from_jwt(token).key


class TokenVerifier:
    def __init__(self, issuer: str, client_id: str, resolve_key: KeyResolver) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._resolve_key = resolve_key

    def verify_id_token(self, token: str) -> User:
        try:
            claims = jwt.decode(
                token,
                self._resolve_key(token),
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except (jwt.PyJWTError, ValueError) as exc:
            raise AuthError("invalid token") from exc
        if claims.get("token_use") != "id":
            raise AuthError("not an ID token")
        return User(sub=claims["sub"], email=claims.get("email", ""), expires_at=int(claims["exp"]))
