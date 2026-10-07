"""HTTP entry point. Runs under uvicorn; in Lambda, the AWS Lambda Web Adapter forwards
Function URL requests to it unchanged, so local and deployed behaviour match.

    uvicorn docqa.web.app:create_app --factory --port 8080
"""

import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import httpx
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from docqa.adapters.emf_metrics import EmfMetrics, NoopMetrics, TurnMetrics
from docqa.config import AppConfig, QASettings, get_config
from docqa.domain.conversation import ConversationSummary
from docqa.domain.retrieval import Strategy
from docqa.pipelines.chat import (
    ChatService,
    Conversation,
    ConversationFullError,
    ConversationNotFoundError,
    Turn,
    TurnFailedError,
)
from docqa.pipelines.qa import MAX_QUESTION_CHARS
from docqa.ports import ModelUnavailableError
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
STATIC_DIR = Path(__file__).parent / "static"

STRATEGY_LABELS = {
    Strategy.DENSE: "Dense (embeddings)",
    Strategy.BM25: "BM25 (keywords)",
    Strategy.HYBRID: "Hybrid (dense + BM25, RRF)",
    Strategy.HYBRID_RERANK: "Hybrid + Cohere rerank",
}

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
        request,
        "index.html",
        {
            "email": user.email,
            "environment": _config(request).environment,
            "strategies": STRATEGY_LABELS,
            "default_strategy": Strategy.HYBRID,
            "max_chars": MAX_QUESTION_CHARS,
        },
    )


@router.get("/api/me")
def me(user: CurrentUser) -> dict[str, str]:
    return {"sub": user.sub, "email": user.email}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    strategy: Strategy = Strategy.HYBRID
    conversation_id: str | None = Field(default=None, max_length=64)


class AskResponse(BaseModel):
    conversation: ConversationSummary
    turn: Turn


def _chat_service(request: Request) -> ChatService:
    """Built on first use, so /health and login never touch Bedrock or LanceDB."""
    if request.app.state.chat is None:
        try:
            request.app.state.chat = request.app.state.chat_factory(_config(request).docs_bucket)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "no document index configured"
            ) from exc
    chat: ChatService = request.app.state.chat
    return chat


def _lambda_ids(request: Request) -> dict[str, str]:
    """Correlation IDs added by Lambda / the Web Adapter, when running in AWS."""
    ids: dict[str, str] = {}
    if xray := request.headers.get("x-amzn-trace-id"):
        ids["xray_trace_id"] = xray
    try:
        context = json.loads(request.headers.get("x-amzn-lambda-context", "{}"))
        if isinstance(context, dict) and context.get("request_id"):
            ids["lambda_request_id"] = str(context["request_id"])
    except json.JSONDecodeError:
        pass
    return ids


def _model_failure(exc: BaseException, metrics: TurnMetrics, trace_id: str) -> HTTPException:
    """Map a model/provider failure to a safe HTTP error, and count it."""
    if isinstance(exc, ModelUnavailableError):  # local mode: Ollama down or model missing
        metrics.model_error(trace_id, "ModelUnavailable")
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.warning("ask_upstream_error", extra={"error_code": code, "trace_id": trace_id})
        metrics.model_error(trace_id, code)
        if code in {"ThrottlingException", "ServiceQuotaExceededException"}:
            return HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "model quota reached; try again later"
            )
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "upstream service error")
    if isinstance(exc, httpx.HTTPError):
        logger.warning("ask_model_http_error", extra={"error": type(exc).__name__})
        metrics.model_error(trace_id, type(exc).__name__)
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "model service error")
    raise exc


@router.post("/api/ask")
def ask(body: AskRequest, request: Request, user: CurrentUser) -> AskResponse:
    chat = _chat_service(request)
    metrics: TurnMetrics = request.app.state.metrics
    try:
        conversation, turn = chat.ask(
            user.sub, body.question, body.strategy, body.conversation_id, _lambda_ids(request)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc
    except ConversationFullError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except TurnFailedError as exc:
        error = _model_failure(exc.__cause__ or exc, metrics, exc.turn.turn_id)
        # The failed turn is saved; tell the page which trace explains it.
        error.headers = {"X-Docqa-Trace-Id": exc.turn.turn_id}
        raise error from exc

    result = turn.result
    if result is None:  # cannot happen: ChatService raises TurnFailedError instead
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "turn has no result")
    metrics.turn(turn.turn_id, result)
    # IDs, counts and timings only: never the question, chunks or answer.
    logger.info(
        "turn_completed",
        extra={
            "user_sub": user.sub,
            "conversation_id": conversation.conversation_id,
            "trace_id": turn.turn_id,
            "strategy": result.strategy,
            "rewritten": result.standalone_question != turn.question,
            "not_found": result.not_found,
            "chunks": len(result.chunks),
            "citations": len(result.citations),
            "timings_ms": result.timings_ms,
            "tokens": result.tokens,
            "cost_usd": result.cost.usd,
            **_lambda_ids(request),
        },
    )
    return AskResponse(conversation=conversation.summary(), turn=turn)


@router.get("/api/conversations")
def list_conversations(request: Request, user: CurrentUser) -> list[ConversationSummary]:
    return list(_chat_service(request).list(user.sub))


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request, user: CurrentUser) -> Conversation:
    try:
        return _chat_service(request).get(user.sub, conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc


@router.delete("/api/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str, request: Request, user: CurrentUser) -> None:
    try:
        _chat_service(request).delete(user.sub, conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc


@router.get("/api/traces/{trace_id}")
def get_trace(trace_id: str, request: Request, user: CurrentUser) -> Turn:
    try:
        return _chat_service(request).turn(user.sub, trace_id)[1]
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trace not found") from exc


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


def _default_chat_factory(bucket: str | None) -> ChatService:
    from docqa.pipelines.wiring import build_chat_service  # noqa: PLC0415 - keeps cold start lean

    return build_chat_service(QASettings(), bucket)


def _default_metrics(config: AppConfig) -> TurnMetrics:
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # only Lambda ships stdout to CloudWatch
        return EmfMetrics({"service": "docqa-web", "environment": config.environment})
    return NoopMetrics()


def create_app(
    config: AppConfig | None = None,
    verifier: TokenVerifier | None = None,
    http: httpx.Client | None = None,
    chat: ChatService | None = None,
    metrics: TurnMetrics | None = None,
) -> FastAPI:
    """Build the app. Dependencies are injectable for tests; defaults come from config."""
    config = config or get_config()
    app = FastAPI(title="docqa", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.verifier = verifier or TokenVerifier(
        config.cognito_issuer, config.cognito_client_id, jwks_key_resolver(config.cognito_issuer)
    )
    app.state.http = http or httpx.Client(timeout=10)
    app.state.chat = chat
    app.state.chat_factory = _default_chat_factory
    app.state.metrics = metrics or _default_metrics(config)
    app.middleware("http")(_security_headers)
    app.add_exception_handler(NotAuthenticatedError, _not_authenticated)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
