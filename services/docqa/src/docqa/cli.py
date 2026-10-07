"""Backfill, maintenance and ad-hoc questions from a laptop (uses your AWS credentials).

uv run python -m docqa.cli ingest            # every document under raw/
uv run python -m docqa.cli ingest raw/a.pdf  # one document
uv run python -m docqa.cli stats
uv run python -m docqa.cli ask "What was my GPA?" --strategy hybrid_rerank
uv run python -m docqa.cli eval --strategies dense,bm25 --retrieval-only
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from docqa.config import IngestSettings, QASettings
from docqa.domain.retrieval import Strategy
from docqa.evals.dataset import load_golden
from docqa.evals.judge import ALL_METRICS, Judge, RagasJudge
from docqa.evals.report import render_report
from docqa.evals.runner import EvalRecord, run_eval, summarize
from docqa.pipelines.ingest import RAW_PREFIX
from docqa.pipelines.wiring import build_ingest_service, build_qa_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docqa")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="parse, chunk, embed and index documents")
    ingest.add_argument("keys", nargs="*", help="S3 keys under raw/ (default: all)")
    sub.add_parser("stats", help="show index size")
    ask = sub.add_parser("ask", help="answer a question (prints JSON)")
    ask.add_argument("question")
    ask.add_argument(
        "--strategy", choices=[s.value for s in Strategy], default=Strategy.HYBRID.value
    )
    ev = sub.add_parser("eval", help="run a golden set through the strategies; write a report")
    ev.add_argument("--dataset", type=Path, default=Path("evals/golden/synthetic.jsonl"))
    ev.add_argument("--strategies", default=",".join(s.value for s in Strategy))
    ev.add_argument("--retrieval-only", action="store_true", help="skip generation (faster)")
    ev.add_argument("--judge", choices=["none", "local"], default="none")
    ev.add_argument("--judge-model", default="qwen2.5:7b", help="Ollama model for --judge local")
    ev.add_argument("--judge-metrics", default=",".join(ALL_METRICS))
    ev.add_argument("--limit", type=int, help="first N questions only")
    ev.add_argument("--out", type=Path, help="default: .data/evals/<time>-<provider>/")
    args = parser.parse_args(argv)

    settings = IngestSettings()

    if args.command == "eval":
        return _eval(args, settings)

    if args.command == "ask":
        qa = build_qa_service(QASettings(), settings.docs_bucket)
        answer = qa.ask(args.question, Strategy(args.strategy))
        print(json.dumps(answer.model_dump(mode="json"), indent=2))
        return 0

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


def _eval(args: argparse.Namespace, settings: IngestSettings) -> int:
    qa_settings = QASettings()
    items = load_golden(args.dataset)[: args.limit]
    strategies = [Strategy(s.strip()) for s in args.strategies.split(",") if s.strip()]
    judge: Judge | None = None
    if args.judge == "local":
        judge = RagasJudge(
            base_url=f"{qa_settings.ollama_url.rstrip('/')}/v1",
            model=args.judge_model,
            embedding_model="bge-m3",
            metrics=[m.strip() for m in args.judge_metrics.split(",") if m.strip()],
        )
    qa = build_qa_service(qa_settings, settings.docs_bucket)

    def progress(done: int, total: int, record: EvalRecord) -> None:
        status = "ERROR" if record.error else "ok"
        print(f"[{done}/{total}] {record.strategy:13} {record.item_id} {status}", file=sys.stderr)

    started = datetime.now(UTC)
    records = run_eval(
        qa,
        items,
        strategies,
        generate=not args.retrieval_only,
        judge=judge,
        progress=progress,
    )
    summaries = summarize(records)
    meta = {
        "date": started.strftime("%Y-%m-%d %H:%M UTC"),
        "provider": qa_settings.provider.value,
        "dataset": f"{args.dataset} ({len(items)} questions)",
        "index": qa_settings.index_table,
        "embedding model": qa_settings.embedding_model_id,
        "generation model": "none (retrieval only)"
        if args.retrieval_only
        else qa_settings.generation_model_id,
        "candidates / top_k": f"{qa_settings.candidates} / {qa_settings.top_k}",
        "agent": f"max_retrievals={qa_settings.agent_max_retrievals}, "
        f"verify={qa_settings.agent_verify}",
        "judge": judge.model_id if judge else "none",
    }
    report = render_report(summaries, meta)

    out: Path = args.out or Path(".data/evals") / (
        f"{started:%Y%m%d-%H%M%S}-{qa_settings.provider.value}"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in records))
    (out / "summary.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in summaries], indent=2)
    )
    (out / "report.md").write_text(report)
    print(report)
    print(f"Results: {out}", file=sys.stderr)
    return 1 if any(r.error for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
