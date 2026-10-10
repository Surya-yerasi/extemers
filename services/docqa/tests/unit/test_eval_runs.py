import json

from docqa.adapters.eval_runs import EvalRunStore
from tests.fakes import MemoryBlobStore

OLD_REPORT = b"# x\n\n- **date:** 2026-10-06 23:31 UTC\n- **provider:** local\n"


def store() -> EvalRunStore:
    summary = json.dumps([{"strategy": "hybrid", "retrieval": {"mrr": 0.9}}]).encode()
    return EvalRunStore(
        MemoryBlobStore(
            {
                "evals/20261006-233153-local/summary.json": summary,
                "evals/20261006-233153-local/report.md": OLD_REPORT,  # pre-meta.json run
                "evals/20261007-010000-local/summary.json": summary,
                "evals/20261007-010000-local/meta.json": json.dumps(
                    {"date": "2026-10-07"}
                ).encode(),
                "evals/20261007-020000-local/records.jsonl": b"",  # incomplete run: ignored
            }
        )
    )


def test_lists_newest_first_with_meta_from_json_or_report() -> None:
    runs = store().list()
    assert [r.run_id for r in runs] == ["20261007-010000-local", "20261006-233153-local"]
    assert runs[0].meta == {"date": "2026-10-07"}
    assert runs[1].meta == {"date": "2026-10-06 23:31 UTC", "provider": "local"}
    assert runs[0].strategies == ["hybrid"]


def test_get_validates_ids() -> None:
    s = store()
    run = s.get("20261007-010000-local")
    assert run is not None
    assert run.summaries[0]["retrieval"]["mrr"] == 0.9
    assert s.get("../../conversations/x") is None
    assert s.get("20261007-020000-local") is None


def test_run_without_any_metadata() -> None:
    bare = EvalRunStore(MemoryBlobStore({"evals/20261007-010000-local/summary.json": b"[]"}))
    run = bare.get("20261007-010000-local")
    assert run is not None
    assert run.meta == {}
