from decimal import Decimal

import pytest
from pydantic import ValidationError

from calculator.domain import Operation
from calculator.models import CalculateRequest


def test_valid_request() -> None:
    req = CalculateRequest.model_validate({"operation": "add", "a": 1, "b": "2.5"})
    assert req.operation is Operation.ADD
    assert req.b == Decimal("2.5")


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "power", "a": 1, "b": 2},
        {"operation": "add", "a": "x", "b": 2},
        {"operation": "add", "a": 1},
        {"operation": "add", "a": 1, "b": 2, "extra": True},
    ],
)
def test_invalid_request(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CalculateRequest.model_validate(payload)
