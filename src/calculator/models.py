"""Request/response contracts for the HTTP API."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from calculator.domain import Operation


class CalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Operation
    a: Decimal
    b: Decimal


class CalculateResponse(BaseModel):
    operation: Operation
    a: Decimal
    b: Decimal
    result: Decimal


class ErrorResponse(BaseModel):
    error: str
