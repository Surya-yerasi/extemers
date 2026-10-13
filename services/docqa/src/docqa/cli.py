"""Backfill, maintenance and ad-hoc questions from a laptop (uses your AWS credentials).

uv run python -m docqa.cli ingest            # every document under raw/
uv run python -m docqa.cli ingest raw/a.pdf  # one document
uv run python -m docqa.cli prune            # remove documents whose file is gone from raw/
uv run python -m docqa.cli stats
uv run python -m docqa.cli ask "What was my GPA?" --strategy hybrid_rerank
uv run python -m docqa.cli eval --strategies dense,bm25 --retrieval-only
uv run python -m docqa.cli judge-traffic --window 7d --limit 20   # LLM judge on live answers
uv run python -m docqa.cli eval --prompts answer@1   # evaluate another prompt version
uv run python -m docqa.cli prompts [--lock]          # list prompt versions; update the lock
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from docqa.adapters.conversation_store import BlobConversationStore
from docqa.adapters.lancedb_index import LanceDocumentCatalog
from docqa.config import IngestSettings, QASettings
from docqa.domain.documents import STATUS_PREFIX, IngestStatus
from docqa.domain.judging import ONLINE_METRICS
from docqa.domain.prompts import (
    LOCK_FILE,
    PROMPT_NAMES,
    available,
    load_prompt,
    prompt_ids,
    render_lock,
    use_versions,
)
from docqa.domain.retrieval import Strategy
from docqa.evals.dataset import load_golden
from docqa.evals.judge import ALL_METRICS, Judge, RagasJudge
from docqa.evals.report import render_report
from docqa.evals.runner import EvalRecord, run_eval, summarize
from docqa.pipelines.chat import Turn
from docqa.pipelines.ingest import RAW_PREFIX, IngestService
from docqa.pipelines.metrics import WINDOWS, since_for
from docqa.pipelines.online_eval import judge_traffic
from docqa.pipelines.wiring import build_blob_store, build_ingest_service, build_qa_service

PROMPTS_FOR_EVAL = ("answer", "plan", "grade", "verify")  # the prompts an eval run exercises


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docqa")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="parse, chunk, embed and index documents")
    ingest.add_argument("keys", nargs="*", help="S3 keys under raw/ (default: all)")
    sub.add_parser("stats", help="show index size")
    sub.add_parser("prune", help="remove indexed documents whose file is gone from raw/")
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
    ev.add_argument("--prompts", default="", help="other prompt versions, e.g. answer@1,plan@2")
    pr = sub.add_parser("prompts", help="list prompt versions (IDs recorded on traces)")
    pr.add_argument("--lock", action="store_true", help="record every version in the lock file")
    jt = sub.add_parser("judge-traffic", help="LLM-judge recent live answers (sampled)")
    jt.add_argument("--window", choices=list(WINDOWS), default="7d")
    jt.add_argument("--limit", type=int, default=20, help="newest N unjudged answers")
    jt.add_argument("--rescore", action="store_true", help="judge already-judged answers again")
    jt.add_argument("--judge-model", default="qwen2.5:7b", help="Ollama model")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prompts":  # files only: no settings or AWS needed
        return _prompts(lock=args.lock)

    settings = IngestSettings()

    if args.command == "eval":
        return _eval(args, settings)

    if args.command == "judge-traffic":
        return _judge_traffic(args, settings)

    if args.command == "ask":
        qa = build_qa_service(QASettings(), settings.docs_bucket)
        answer = qa.ask(args.question, Strategy(args.strategy))
        print(json.dumps(answer.model_dump(mode="json"), indent=2))
        return 0

    return _ingest_commands(args, settings)


def _ingest_commands(args: argparse.Namespace, settings: IngestSettings) -> int:
    service = build_ingest_service(settings)

    if args.command == "stats":
        print(f"{settings.index_table}: {service._index.count()} chunks")
        return 0

    if args.command == "prune":
        return _prune(service, settings)

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


def _prompts(lock: bool) -> int:
    for name in PROMPT_NAMES:
        active = load_prompt(name)
        versions = sorted(p.version for p in available() if p.name == name)
        print(f"{active.id:24} versions {versions}  {active.description}")
    if lock:
        LOCK_FILE.write_text(render_lock(available()))
        print(f"wrote {LOCK_FILE}")
    return 0


def _eval(args: argparse.Namespace, settings: IngestSettings) -> int:
    if args.prompts:
        use_versions(args.prompts)  # before any prompt is read
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
        "prompts": ", ".join(prompt_ids(PROMPTS_FOR_EVAL).values()),
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
    (out / "meta.json").write_text(json.dumps(meta, indent=2))  # read by the Metrics tab
    print(report)
    print(f"Results: {out}", file=sys.stderr)
    return 1 if any(r.error for r in records) else 0


def _prune(service: IngestService, settings: IngestSettings) -> int:
    # S3 delete events do this when deployed; local files have no such event.
    catalog = LanceDocumentCatalog(settings.resolved_lancedb_uri, settings.index_table)
    gone = [d for d in catalog.documents() if not service._blobs.exists(d.source_key)]
    for doc in gone:
        service.delete(doc.source_key)
        print(f"deleted  {doc.source_key}  ({doc.chunks} chunks)")
    # Status records of deleted files that never reached the index (e.g. failed ones).
    stale = 0
    for key in service._blobs.list_keys(STATUS_PREFIX):
        source = IngestStatus.model_validate_json(service._blobs.get(key)).source_key
        if not service._blobs.exists(source):
            service.delete(source)
            stale += 1
    print(f"{len(gone)} document(s) pruned, {stale} stale status record(s) removed")
    return 0


def _judge_traffic(args: argparse.Namespace, settings: IngestSettings) -> int:
    qa_settings = QASettings()
    store = BlobConversationStore(build_blob_store(qa_settings, settings.docs_bucket))
    judge = RagasJudge(
        base_url=f"{qa_settings.ollama_url.rstrip('/')}/v1",
        model=args.judge_model,
        embedding_model="bge-m3",
        metrics=list(ONLINE_METRICS),
    )

    def progress(done: int, total: int, turn: Turn) -> None:
        scores = turn.judge.scores if turn.judge else {}
        shown = ", ".join(f"{k} {'-' if v is None else f'{v:.2f}'}" for k, v in scores.items())
        print(f"[{done}/{total}] {turn.turn_id}  {shown}", file=sys.stderr)

    stats = judge_traffic(
        store.all(),
        store.save,
        judge,
        since=since_for(args.window, datetime.now(UTC)),
        limit=args.limit,
        rescore=args.rescore,
        progress=progress,
    )
    print(
        f"judged {stats.judged} of {stats.eligible} eligible answers "
        f"({stats.failed} failed); see Metrics → Live traffic"
    )
    return 1 if stats.judged and stats.failed == stats.judged else 0


if __name__ == "__main__":
    raise SystemExit(main())
