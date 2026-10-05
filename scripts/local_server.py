"""Run the Lambda handler locally behind a minimal HTTP server.

Converts each HTTP request into an API Gateway HTTP API (v2) event, so the exact
same handler code path runs locally as in AWS.

    uv run python scripts/local_server.py [--port 8000]
"""

import argparse
import contextlib
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "calculator")
os.environ.setdefault("APP_ENVIRONMENT", "local")

from calculator.handler import lambda_handler


class _Context:
    function_name = "calculator-local"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:local:000000000000:function:calculator-local"

    def __init__(self) -> None:
        self.aws_request_id = str(uuid.uuid4())


def _to_event(method: str, raw_path: str, headers: dict[str, str], body: str) -> dict[str, Any]:
    url = urlsplit(raw_path)
    return {
        "version": "2.0",
        "routeKey": f"{method} {url.path}",
        "rawPath": url.path,
        "rawQueryString": url.query,
        "headers": {k.lower(): v for k, v in headers.items()},
        "requestContext": {
            "http": {"method": method, "path": url.path, "sourceIp": "127.0.0.1"},
            "requestId": str(uuid.uuid4()),
            "routeKey": f"{method} {url.path}",
            "stage": "$default",
            "timeEpoch": int(time.time() * 1000),
        },
        "body": body,
        "isBase64Encoded": False,
    }


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length).decode() if length else ""
        event = _to_event(self.command, self.path, dict(self.headers), body)
        result = lambda_handler(event, _Context())  # type: ignore[arg-type]
        payload = result.get("body", "").encode()
        self.send_response(result["statusCode"])
        for key, value in (result.get("headers") or {}).items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch  # noqa: N815


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving calculator on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
