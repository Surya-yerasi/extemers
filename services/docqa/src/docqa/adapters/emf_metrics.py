"""CloudWatch metrics via the Embedded Metric Format (EMF): one JSON line on stdout per event.
In Lambda, CloudWatch Logs turns it into metrics with no API call and no extra IAM.

Six metrics, one dimension set (service, environment): within the 10 free custom metrics.
Per-strategy and per-trace detail stays in the line as properties, queryable in Logs
Insights, instead of becoming dimensions (each dimension value is a new, billed metric).
"""

import json
import time
from collections.abc import Callable
from typing import Protocol

from docqa.pipelines.qa import AskResult

NAMESPACE = "docqa"
RETRIEVAL_STAGES = ("embed", "dense_search", "bm25_search", "fuse", "rerank")


class TurnMetrics(Protocol):
    def turn(self, trace_id: str, result: AskResult) -> None: ...
    def model_error(self, trace_id: str, error_code: str) -> None: ...


class NoopMetrics:
    """Local mode: nothing to send metrics to."""

    def turn(self, trace_id: str, result: AskResult) -> None:
        return None

    def model_error(self, trace_id: str, error_code: str) -> None:
        return None


class EmfMetrics:
    def __init__(
        self,
        dimensions: dict[str, str],
        emit: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dimensions = dimensions
        self._emit = emit
        self._clock = clock

    def turn(self, trace_id: str, result: AskResult) -> None:
        timings = result.timings_ms
        self._write(
            {
                "AskLatency": (timings.get("total", 0.0), "Milliseconds"),
                "RetrievalLatency": (
                    sum(timings.get(s, 0.0) for s in RETRIEVAL_STAGES),
                    "Milliseconds",
                ),
                "GenerationLatency": (timings.get("generate", 0.0), "Milliseconds"),
                "CostUSD": (result.cost.usd, "None"),
                "NotFound": (1.0 if result.not_found else 0.0, "Count"),
            },
            {"trace_id": trace_id, "strategy": result.strategy.value},
        )

    def model_error(self, trace_id: str, error_code: str) -> None:
        self._write(
            {"ModelErrors": (1.0, "Count")}, {"trace_id": trace_id, "error_code": error_code}
        )

    def _write(self, metrics: dict[str, tuple[float, str]], properties: dict[str, str]) -> None:
        document = {
            "_aws": {
                "Timestamp": int(self._clock() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": NAMESPACE,
                        "Dimensions": [list(self._dimensions)],
                        "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in metrics.items()],
                    }
                ],
            },
            **self._dimensions,
            **{name: value for name, (value, _) in metrics.items()},
            **properties,
        }
        self._emit(json.dumps(document))
