import pytest

from docqa.domain.tracing import Tracer


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000


def test_spans_record_offsets_durations_and_attributes() -> None:
    clock = FakeClock()
    tracer = Tracer("c_0123456789abcdef.1", {"conversation_id": "c_0123456789abcdef"}, clock)
    clock.advance(5)
    with tracer.span("embed", model="titan") as span:
        clock.advance(20)
        span.set(input_tokens=7)
    with tracer.span("generate"):
        clock.advance(100)
    tracer.set(strategy="hybrid")
    clock.advance(1)
    trace = tracer.finish()

    assert trace.trace_id == "c_0123456789abcdef.1"
    assert trace.duration_ms == 126.0
    assert trace.attributes == {"conversation_id": "c_0123456789abcdef", "strategy": "hybrid"}
    embed, generate = trace.spans
    assert (embed.start_ms, embed.duration_ms) == (5.0, 20.0)
    assert embed.attributes == {"model": "titan", "input_tokens": 7}
    assert (generate.start_ms, generate.duration_ms, generate.status) == (25.0, 100.0, "ok")
    assert trace.timings_ms == {"embed": 20.0, "generate": 100.0}


def test_failed_span_is_recorded_and_the_error_propagates() -> None:
    clock = FakeClock()
    tracer = Tracer("t", clock=clock)

    def call_model() -> None:
        with tracer.span("generate") as span:
            span.set(model="nova")
            clock.advance(3)
            raise RuntimeError("throttled")

    with pytest.raises(RuntimeError, match="throttled"):
        call_model()
    failed = tracer.finish().spans[0]
    assert failed.status == "error"
    assert failed.error == "RuntimeError: throttled"
    assert failed.duration_ms == 3.0
    assert failed.attributes == {"model": "nova"}


def test_finish_returns_a_snapshot() -> None:
    tracer = Tracer("t")
    first = tracer.finish()
    with tracer.span("later"):
        pass
    assert first.spans == []
    assert [s.name for s in tracer.finish().spans] == ["later"]
