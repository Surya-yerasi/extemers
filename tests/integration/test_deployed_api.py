"""Smoke tests against a deployed stack. Run with: API_URL=https://... pytest -m integration"""

import json
import os
import urllib.request

import pytest

API_URL = os.environ.get("API_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not API_URL, reason="API_URL not set"),
]


def _request(method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict]:  # type: ignore[type-arg]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - URL comes from our own deployment output
        f"{API_URL}{path}", data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_health() -> None:
    status, body = _request("GET", "/health")
    assert status == 200
    assert body["status"] == "ok"


def test_add() -> None:
    status, body = _request("POST", "/calculate", {"operation": "add", "a": 2, "b": 3})
    assert status == 200
    assert body["result"] == "5"


def test_divide_by_zero() -> None:
    status, _ = _request("POST", "/calculate", {"operation": "divide", "a": 1, "b": 0})
    assert status == 400
