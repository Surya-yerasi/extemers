from decimal import Decimal

import pytest

from calculator.domain import DomainError, Operation, calculate


@pytest.mark.parametrize(
    ("operation", "a", "b", "expected"),
    [
        (Operation.ADD, "2", "3", "5"),
        (Operation.SUBTRACT, "2", "3", "-1"),
        (Operation.MULTIPLY, "2.5", "4", "10.0"),
        (Operation.DIVIDE, "7", "2", "3.5"),
        (Operation.ADD, "0.1", "0.2", "0.3"),
    ],
)
def test_calculate(operation: Operation, a: str, b: str, expected: str) -> None:
    assert calculate(operation, Decimal(a), Decimal(b)) == Decimal(expected)


def test_divide_by_zero_raises() -> None:
    with pytest.raises(DomainError, match="Division by zero"):
        calculate(Operation.DIVIDE, Decimal(1), Decimal(0))
