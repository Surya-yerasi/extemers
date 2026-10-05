"""Lambda entry point. Translates API Gateway HTTP API events to service calls; no logic here."""

from http import HTTPStatus
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from calculator.config import get_settings
from calculator.domain import DomainError
from calculator.models import CalculateRequest, ErrorResponse
from calculator.service import CalculatorService

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
service = CalculatorService()


def _error(status: HTTPStatus, message: str) -> Response[str]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=ErrorResponse(error=message).model_dump_json(),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": get_settings().environment}


@app.post("/calculate")
@tracer.capture_method
def calculate() -> Response[str]:
    try:
        payload = app.current_event.json_body
    except ValueError as exc:
        raise BadRequestError("Request body must be valid JSON") from exc
    request = CalculateRequest.model_validate(payload)
    response = service.calculate(request)
    return Response(
        status_code=HTTPStatus.OK,
        content_type=content_types.APPLICATION_JSON,
        body=response.model_dump_json(),
    )


@app.exception_handler(ValidationError)  # type: ignore[untyped-decorator]  # Powertools lacks types here
def handle_validation_error(exc: ValidationError) -> Response[str]:
    details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return _error(HTTPStatus.UNPROCESSABLE_ENTITY, details)


@app.exception_handler(DomainError)  # type: ignore[untyped-decorator]
def handle_domain_error(exc: DomainError) -> Response[str]:
    return _error(HTTPStatus.BAD_REQUEST, str(exc))


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
