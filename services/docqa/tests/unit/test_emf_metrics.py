import json

from docqa.adapters.emf_metrics import EmfMetrics, NoopMetrics
from docqa.domain.retrieval import Strategy
from tests.fakes import chat_service


def test_turn_emits_one_emf_document() -> None:
    lines: list[str] = []
    _, turn = chat_service().ask("u1", "gpa?", Strategy.HYBRID)
    assert turn.result is not None
    metrics = EmfMetrics({"service": "docqa-web", "environment": "dev"}, lines.append, lambda: 1.5)
    metrics.turn(turn.turn_id, turn.result)

    doc = json.loads(lines[0])
    meta = doc["_aws"]
    assert meta["Timestamp"] == 1500
    directive = meta["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "docqa"
    assert directive["Dimensions"] == [["service", "environment"]]
    names = [m["Name"] for m in directive["Metrics"]]
    assert names == ["AskLatency", "RetrievalLatency", "GenerationLatency", "CostUSD", "NotFound"]
    assert doc["service"] == "docqa-web"
    assert doc["AskLatency"] == turn.result.timings_ms["total"]
    assert doc["RetrievalLatency"] == sum(
        turn.result.timings_ms[s] for s in ("embed", "dense_search", "bm25_search", "fuse")
    )
    assert doc["NotFound"] == 0.0
    assert doc["trace_id"] == turn.turn_id  # a property, not a dimension
    assert doc["strategy"] == "hybrid"


def test_model_error_metric() -> None:
    lines: list[str] = []
    EmfMetrics({"service": "s"}, lines.append).model_error("c_x.1", "ThrottlingException")
    doc = json.loads(lines[0])
    assert doc["ModelErrors"] == 1.0
    assert doc["error_code"] == "ThrottlingException"
    assert doc["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "ModelErrors", "Unit": "Count"}
    ]


def test_noop_metrics_do_nothing() -> None:
    _, turn = chat_service().ask("u1", "gpa?", Strategy.BM25)
    assert turn.result is not None
    NoopMetrics().turn(turn.turn_id, turn.result)
    NoopMetrics().model_error("t", "x")
