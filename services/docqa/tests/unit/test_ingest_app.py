from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from docqa.domain.models import doc_id_for
from docqa.ingest_app import create_app, s3_records
from docqa.pipelines.ingest import IngestResult, Outcome


def s3_event(*records: tuple[str, str]) -> dict[str, Any]:
    return {
        "Records": [
            {"eventSource": "aws:s3", "eventName": name, "s3": {"object": {"key": key}}}
            for name, key in records
        ]
    }


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ingest(self, key: str) -> IngestResult:
        self.calls.append(("ingest", key))
        return IngestResult(key, Outcome.INDEXED, chunks=3)

    def delete(self, key: str) -> IngestResult:
        self.calls.append(("delete", key))
        return IngestResult(key, Outcome.DELETED)


def test_s3_records_decodes_keys_and_ignores_other_sources() -> None:
    event = s3_event(("ObjectCreated:Put", "raw/My+Transcript%282019%29.pdf"))
    event["Records"].append({"eventSource": "aws:sqs"})
    assert s3_records(event) == [("ObjectCreated:Put", "raw/My Transcript(2019).pdf")]


def test_events_dispatch_create_and_remove() -> None:
    service = RecordingService()
    client = TestClient(create_app(service))  # type: ignore[arg-type]
    response = client.post(
        "/events",
        json=s3_event(("ObjectCreated:Put", "raw/a.pdf"), ("ObjectRemoved:Delete", "raw/b.pdf")),
    )
    assert response.status_code == 200
    assert service.calls == [("ingest", "raw/a.pdf"), ("delete", "raw/b.pdf")]
    assert response.json()["results"][0] == {"key": "raw/a.pdf", "outcome": "indexed", "chunks": 3}


def test_health() -> None:
    assert TestClient(create_app(RecordingService())).get("/health").json() == {"status": "ok"}  # type: ignore[arg-type]


def test_web_app_has_no_events_route(client: TestClient) -> None:
    """/events must never be reachable through the public Function URL."""
    assert client.post("/events", json={}).status_code in {404, 405}


def test_failures_are_logged_with_the_doc_id_and_fail_the_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, dict[str, Any]]] = []

    class RecordingLogger:
        def exception(self, message: str, extra: dict[str, Any]) -> None:
            logged.append((message, extra))

        def info(self, message: str, extra: dict[str, Any]) -> None:
            pass

    class Denied(RecordingService):
        def ingest(self, key: str) -> IngestResult:
            error = {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}
            raise ClientError(error, "HeadObject")  # type: ignore[arg-type]

    monkeypatch.setattr("docqa.ingest_app.logger", RecordingLogger())
    client = TestClient(create_app(Denied()), raise_server_exceptions=False)  # type: ignore[arg-type]
    response = client.post("/events", json=s3_event(("ObjectCreated:Put", "raw/secret name.pdf")))
    assert response.status_code == 500  # => failed invocation => S3 retries
    assert logged == [
        (
            "ingest_failed",
            {
                "doc_id": doc_id_for("raw/secret name.pdf"),  # never the file name
                "error": "ClientError",
                "error_code": "AccessDenied",
            },
        )
    ]


def test_service_is_built_on_the_first_event_not_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    def build(settings: object) -> RecordingService:
        built.append("built")
        return RecordingService()

    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")
    monkeypatch.setattr("docqa.pipelines.wiring.build_ingest_service", build)
    client = TestClient(create_app())
    assert client.get("/health").status_code == 200
    assert built == []
    client.post("/events", json=s3_event(("ObjectCreated:Put", "raw/a.pdf")))
    client.post("/events", json=s3_event(("ObjectCreated:Put", "raw/b.pdf")))
    assert built == ["built"]
