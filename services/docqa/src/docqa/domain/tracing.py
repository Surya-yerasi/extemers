"""Per-question traces: one span per pipeline stage, with timings and attributes.

App-level on purpose: the same trace is recorded locally and in Lambda, stored with the
conversation turn and shown in the Traces tab. Pure: no AWS imports.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Span(BaseModel):
    name: str
    start_ms: float  # offset from the start of the trace
    duration_ms: float
    depth: int = 0  # nesting: an agent step's retrieval spans sit one level below it
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Trace(BaseModel):
    trace_id: str
    started_at: datetime
    duration_ms: float = 0.0
    attributes: dict[str, Any] = Field(default_factory=dict)
    spans: list[Span] = Field(default_factory=list)

    @property
    def timings_ms(self) -> dict[str, float]:
        """Total time per span name (an agent may embed or search several times)."""
        totals: dict[str, float] = {}
        for span in self.spans:
            totals[span.name] = round(totals.get(span.name, 0.0) + span.duration_ms, 1)
        return totals


class ActiveSpan:
    """Handed to the code inside a span so it can attach results (tokens, top chunks...)."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set(self, **attributes: Any) -> None:
        self.attributes.update(attributes)


Clock = Callable[[], float]


class Tracer:
    def __init__(
        self,
        trace_id: str,
        attributes: dict[str, Any] | None = None,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._clock = clock
        self._origin = clock()
        self._trace = Trace(
            trace_id=trace_id, started_at=datetime.now(UTC), attributes=dict(attributes or {})
        )
        self._depth = 0

    def _elapsed_ms(self, since: float) -> float:
        return round((self._clock() - since) * 1000, 1)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[ActiveSpan]:
        active = ActiveSpan()
        active.set(**attributes)
        start = self._clock()
        span = Span(
            name=name,
            start_ms=self._elapsed_ms(self._origin),
            duration_ms=0.0,
            depth=self._depth,
        )
        self._depth += 1
        try:
            yield active
        except Exception as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._depth -= 1
            span.duration_ms = self._elapsed_ms(start)
            span.attributes = active.attributes
            self._trace.spans.append(span)

    def set(self, **attributes: Any) -> None:
        self._trace.attributes.update(attributes)

    def finish(self) -> Trace:
        """Snapshot with spans in start order (a parent ends after its children)."""
        self._trace.duration_ms = self._elapsed_ms(self._origin)
        snapshot = self._trace.model_copy(deep=True)
        snapshot.spans.sort(key=lambda span: (span.start_ms, span.depth))
        return snapshot
