"""On-demand Bedrock prices (us-east-1, USD), for the per-question cost estimate.

Check https://aws.amazon.com/bedrock/pricing/ when adding a model; unknown models cost 0
and are flagged so the estimate is never silently wrong.
"""

from pydantic import BaseModel

# Models served on the laptop (local mode, Ollama) are free.
LOCAL_MODEL_PREFIX = "local/"

# USD per 1,000 tokens: (input, output). Keys are model IDs without the "us." profile prefix.
TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "amazon.nova-micro-v1:0": (0.000035, 0.00014),
    "amazon.nova-2-lite-v1:0": (0.0003, 0.0025),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (0.001, 0.005),
    "amazon.titan-embed-text-v2:0": (0.00002, 0.0),
}

# USD per query (one query = up to 100 documents).
RERANK_PRICES: dict[str, float] = {"cohere.rerank-v3-5:0": 0.002}


class CostEstimate(BaseModel):
    usd: float = 0.0
    unpriced_models: list[str] = []

    def add(self, other: "CostEstimate") -> "CostEstimate":
        return CostEstimate(
            usd=self.usd + other.usd,
            unpriced_models=sorted({*self.unpriced_models, *other.unpriced_models}),
        )


def _base_model(model_id: str) -> str:
    return model_id.split(".", 1)[1] if model_id.startswith(("us.", "eu.", "apac.")) else model_id


def token_cost(model_id: str, input_tokens: int, output_tokens: int = 0) -> CostEstimate:
    if model_id.startswith(LOCAL_MODEL_PREFIX):
        return CostEstimate()
    price = TOKEN_PRICES.get(_base_model(model_id))
    if price is None:
        return CostEstimate(unpriced_models=[model_id])
    return CostEstimate(usd=(input_tokens * price[0] + output_tokens * price[1]) / 1000)


def rerank_cost(model_id: str) -> CostEstimate:
    if model_id.startswith(LOCAL_MODEL_PREFIX):
        return CostEstimate()
    price = RERANK_PRICES.get(model_id)
    if price is None:
        return CostEstimate(unpriced_models=[model_id])
    return CostEstimate(usd=price)
