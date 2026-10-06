"""HTTP entry point. Runs under uvicorn; in Lambda, the AWS Lambda Web Adapter forwards
Function URL requests to it unchanged, so local and deployed behaviour match.

    uvicorn docqa.web.app:create_app --factory --port 8080
"""

import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import httpx
from aws_lambda_powertools import Logger
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from docqa.config import AppConfig, get_config
from docqa.web.auth import (
    OAUTH_COOKIE,
    OAUTH_COOKIE_MAX_AGE,
    SESSION_COOKIE,
    AuthError,
    TokenVerifier,
    User,
    authorize_url,
    jwks_key_resolver,
    logout_url,
    pkce_pair,
)

logger = Logger(service="docqa-web")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class NotAuthenticatedError(Exception):
    """Raised by the auth dependency; HTML routes redirect, API routes return 401."""


router = APIRouter()


def _config(request: Request) -> AppConfig:
    config: AppConfig = request.app.state.config
    return config


def _base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}/"


def _redirect_uri(request: Request) -> str:
    uri = f"{_base_url(request)}callback"
    # The Host header is client-controlled: only ever send Cognito an allow-listed URI.
    if uri not in _config(request).allowed_redirect_uris:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unexpected host")
    return uri


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def current_user(request: Request) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise NotAuthenticatedError
    verifier: TokenVerifier = request.app.state.verifier
    try:
        return verifier.verify_id_token(token)
    except AuthError as exc:
        raise NotAuthenticatedError from exc


CurrentUser = Annotated[User, Depends(current_user)]


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "environment": _config(request).environment}


@router.get("/login")
def login(request: Request) -> Response:
    config = _config(request)
    state = secrets.token_urlsafe(24)
    code_verifier, challenge = pkce_pair()
    target = authorize_url(
        config.cognito_domain, config.cognito_client_id, _redirect_uri(request), state, challenge
    )
    response = RedirectResponse(target, status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        OAUTH_COOKIE,
        f"{state}.{code_verifier}",
        max_age=OAUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=_secure(request),
        samesite="lax",
    )
    return response


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "") -> Response:
    config = _config(request)
    stored = request.cookies.get(OAUTH_COOKIE, "")
    expected_state, _, code_verifier = stored.partition(".")
    if not code or not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "login expired or invalid; try again")

    http: httpx.Client = request.app.state.http
    token_response = http.post(
        f"{config.cognito_domain}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config.cognito_client_id,
            "code": code,
            "redirect_uri": _redirect_uri(request),
            "code_verifier": code_verifier,
        },
    )
    if token_response.status_code != status.HTTP_200_OK:
        logger.warning("token_exchange_failed", extra={"status": token_response.status_code})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "login failed; try again")

    id_token = token_response.json().get("id_token", "")
    verifier: TokenVerifier = request.app.state.verifier
    try:
        user = verifier.verify_id_token(id_token)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "login failed; try again") from exc

    logger.info("login_succeeded", extra={"user_sub": user.sub})
    response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    response.delete_cookie(OAUTH_COOKIE)
    response.set_cookie(
        SESSION_COOKIE,
        id_token,
        max_age=max(user.expires_at - int(time.time()), 0),
        httponly=True,
        secure=_secure(request),
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout(request: Request) -> Response:
    config = _config(request)
    response = RedirectResponse(
        logout_url(config.cognito_domain, config.cognito_client_id, _base_url(request)),
        status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser) -> Response:
    return templates.TemplateResponse(
        request, "index.html", {"email": user.email, "environment": _config(request).environment}
    )


@router.get("/api/me")
def me(user: CurrentUser) -> dict[str, str]:
    return {"sub": user.sub, "email": user.email}


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


async def _not_authenticated(request: Request, _: Exception) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not authenticated"}, status.HTTP_401_UNAUTHORIZED)
    return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)


def create_app(
    config: AppConfig | None = None,
    verifier: TokenVerifier | None = None,
    http: httpx.Client | None = None,
) -> FastAPI:
    """Build the app. Dependencies are injectable for tests; defaults come from config."""
    config = config or get_config()
    app = FastAPI(title="docqa", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.verifier = verifier or TokenVerifier(
        config.cognito_issuer, config.cognito_client_id, jwks_key_resolver(config.cognito_issuer)
    )
    app.state.http = http or httpx.Client(timeout=10)
    app.middleware("http")(_security_headers)
    app.add_exception_handler(NotAuthenticatedError, _not_authenticated)
    app.include_router(router)
    return app
