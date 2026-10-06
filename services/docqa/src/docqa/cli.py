"""Backfill and maintenance from a laptop (uses your AWS credentials).

uv run python -m docqa.cli ingest            # every document under raw/
uv run python -m docqa.cli ingest raw/a.pdf  # one document
uv run python -m docqa.cli stats
"""

import argparse
import sys

from docqa.config import IngestSettings
from docqa.pipelines.ingest import RAW_PREFIX
from docqa.pipelines.wiring import build_ingest_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docqa")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="parse, chunk, embed and index documents")
    ingest.add_argument("keys", nargs="*", help="S3 keys under raw/ (default: all)")
    sub.add_parser("stats", help="show index size")
    args = parser.parse_args(argv)

    settings = IngestSettings()
    service = build_ingest_service(settings)

    if args.command == "stats":
        print(f"{settings.index_table}: {service._index.count()} chunks")
        return 0

    keys = args.keys or service._blobs.list_keys(RAW_PREFIX)
    failures = 0
    for key in keys:
        try:
            result = service.ingest(key)
            detail = result.reason or f"{result.chunks} chunks, {result.vision_pages} vision pages"
            print(f"{result.outcome:8} {key}  ({detail})")
        except Exception as exc:
            failures += 1
            print(f"FAILED   {key}  ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
