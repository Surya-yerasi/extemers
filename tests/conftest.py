import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _lambda_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERTOOLS_TRACE_DISABLED", "true")
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "calculator")


@pytest.fixture
def lambda_context() -> Any:
    class Context:
        function_name = "calculator"
        memory_limit_in_mb = 128
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:calculator"
        aws_request_id = "test-request-id"

    return Context()


@pytest.fixture
def apigw_event() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = json.loads((FIXTURES / "apigw_event.json").read_text())

    def make(method: str = "POST", path: str = "/calculate", body: Any = None) -> dict[str, Any]:
        event = copy.deepcopy(base)
        event["routeKey"] = event["requestContext"]["routeKey"] = f"{method} {path}"
        event["rawPath"] = event["requestContext"]["http"]["path"] = path
        event["requestContext"]["http"]["method"] = method
        if body is None:
            event["body"] = ""
        else:
            event["body"] = body if isinstance(body, str) else json.dumps(body)
        return event

    return make
