"""Ingestion function entry point.

Lambda Web Adapter forwards non-HTTP events (here: S3 notifications) as POST /events.
This app is separate from the web app, so /events is never reachable through the public
Function URL; the ingest function has no URL at all.

    uvicorn docqa.ingest_app:create_app --factory --port 8080
"""

from typing import Any
from urllib.parse import unquote_plus

from aws_lambda_powertools import Logger
from fastapi import FastAPI, Request

from docqa.config import IngestSettings
from docqa.pipelines.ingest import IngestService
from docqa.pipelines.wiring import build_ingest_service

logger = Logger(service="docqa-ingest")


def s3_records(event: dict[str, Any]) -> list[tuple[str, str]]:
    """(event_name, key) pairs from an S3 notification; keys arrive URL-encoded."""
    return [
        (r["eventName"], unquote_plus(r["s3"]["object"]["key"]))
        for r in event.get("Records", [])
        if r.get("eventSource") == "aws:s3"
    ]


def create_app(service: IngestService | None = None) -> FastAPI:
    app = FastAPI(title="docqa-ingest", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def get_service() -> IngestService:
        if app.state.service is None:  # built lazily: first event, not at import
            app.state.service = build_ingest_service(IngestSettings())
        svc: IngestService = app.state.service
        return svc

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events")
    async def events(request: Request) -> dict[str, Any]:
        service = get_service()
        results = []
        # Errors propagate: Lambda reports a failure and S3's async invoke retries.
        for event_name, key in s3_records(await request.json()):
            if event_name.startswith("ObjectRemoved"):
                result = service.delete(key)
            else:
                result = service.ingest(key)
            results.append({"key": result.key, "outcome": result.outcome, "chunks": result.chunks})
        logger.info("events_processed", extra={"count": len(results)})
        return {"results": results}

    return app
