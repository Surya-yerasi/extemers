"""Application service: orchestrates domain logic and (later) external clients like Bedrock."""

from aws_lambda_powertools import Logger

from calculator.domain import calculate
from calculator.models import CalculateRequest, CalculateResponse

logger = Logger(child=True)


class CalculatorService:
    def calculate(self, request: CalculateRequest) -> CalculateResponse:
        result = calculate(request.operation, request.a, request.b)
        logger.info("Calculated", extra={"operation": request.operation.value})
        return CalculateResponse(
            operation=request.operation, a=request.a, b=request.b, result=result
        )
