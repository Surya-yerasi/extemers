"""Pure business logic. No AWS, HTTP, or framework imports belong here."""

from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from operator import add, mul, sub, truediv


class Operation(StrEnum):
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class DomainError(ValueError):
    """Raised when inputs are well-formed but the operation is not allowed."""


_OPERATIONS: dict[Operation, Callable[[Decimal, Decimal], Decimal]] = {
    Operation.ADD: add,
    Operation.SUBTRACT: sub,
    Operation.MULTIPLY: mul,
    Operation.DIVIDE: truediv,
}


def calculate(operation: Operation, a: Decimal, b: Decimal) -> Decimal:
    if operation is Operation.DIVIDE and b == 0:
        raise DomainError("Division by zero is not allowed")
    return _OPERATIONS[operation](a, b)
