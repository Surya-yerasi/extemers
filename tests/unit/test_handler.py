import json
from collections.abc import Callable
from typing import Any

import pytest

from calculator.handler import lambda_handler

EventFactory = Callable[..., dict[str, Any]]


def invoke(event: dict[str, Any], context: Any) -> tuple[int, dict[str, Any]]:
    response = lambda_handler(event, context)
    return response["statusCode"], json.loads(response["body"])


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("add", "5"),
        ("subtract", "-1"),
        ("multiply", "6"),
        ("divide", "0.6666666666666666666666666667"),
    ],
)
def test_calculate_ok(
    apigw_event: EventFactory, lambda_context: Any, operation: str, expected: str
) -> None:
    status, body = invoke(
        apigw_event(body={"operation": operation, "a": 2, "b": 3}), lambda_context
    )
    assert status == 200
    assert body["result"] == expected
    assert body["operation"] == operation


def test_divide_by_zero_is_400(apigw_event: EventFactory, lambda_context: Any) -> None:
    status, body = invoke(apigw_event(body={"operation": "divide", "a": 1, "b": 0}), lambda_context)
    assert status == 400
    assert "Division by zero" in body["error"]


def test_unknown_operation_is_422(apigw_event: EventFactory, lambda_context: Any) -> None:
    status, body = invoke(apigw_event(body={"operation": "power", "a": 1, "b": 2}), lambda_context)
    assert status == 422
    assert "operation" in body["error"]


def test_invalid_json_is_400(apigw_event: EventFactory, lambda_context: Any) -> None:
    status, body = invoke(apigw_event(body="{not json"), lambda_context)
    assert status == 400
    assert "valid JSON" in body["message"]


def test_health(apigw_event: EventFactory, lambda_context: Any) -> None:
    status, body = invoke(apigw_event(method="GET", path="/health"), lambda_context)
    assert status == 200
    assert body["status"] == "ok"


def test_unknown_route_is_404(apigw_event: EventFactory, lambda_context: Any) -> None:
    status, _ = invoke(apigw_event(method="GET", path="/nope"), lambda_context)
    assert status == 404
