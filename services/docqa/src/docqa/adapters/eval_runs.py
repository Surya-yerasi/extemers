"""Eval runs written by `docqa.cli eval`, read back for the Metrics tab.

Runs live under evals/<run id>/ in a BlobStore: .data/evals/ locally (LocalBlobStore is
rooted at .data). Each has summary.json (per-strategy aggregates) and, from Phase 7 on,
meta.json; older runs carry their metadata only in report.md, which is parsed as a fallback.
"""

import json
import re
from typing import Any

from pydantic import BaseModel

from docqa.ports import BlobStore

PREFIX = "evals/"
_META_LINE = re.compile(r"^- \*\*(?P<key>[^*]+):\*\* (?P<value>.+)$", re.MULTILINE)
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z]+$")


class EvalRunInfo(BaseModel):
    run_id: str
    meta: dict[str, str]
    strategies: list[str]


class EvalRun(EvalRunInfo):
    summaries: list[dict[str, Any]]


class EvalRunStore:
    def __init__(self, blobs: BlobStore) -> None:
        self._blobs = blobs

    def _meta(self, run_id: str) -> dict[str, str]:
        meta_key = f"{PREFIX}{run_id}/meta.json"
        if self._blobs.exists(meta_key):
            data = json.loads(self._blobs.get(meta_key))
            return {str(k): str(v) for k, v in data.items()}
        report_key = f"{PREFIX}{run_id}/report.md"
        if self._blobs.exists(report_key):
            text = self._blobs.get(report_key).decode()
            return {m["key"]: m["value"] for m in _META_LINE.finditer(text)}
        return {}

    def get(self, run_id: str) -> EvalRun | None:
        if not _RUN_ID.match(run_id):
            return None
        key = f"{PREFIX}{run_id}/summary.json"
        if not self._blobs.exists(key):
            return None
        summaries: list[dict[str, Any]] = json.loads(self._blobs.get(key))
        return EvalRun(
            run_id=run_id,
            meta=self._meta(run_id),
            strategies=[str(s["strategy"]) for s in summaries],
            summaries=summaries,
        )

    def list(self) -> list[EvalRunInfo]:
        """Newest first. Run IDs start with the UTC time, so they sort chronologically."""
        run_ids = sorted(
            {
                key.removeprefix(PREFIX).split("/", 1)[0]
                for key in self._blobs.list_keys(PREFIX)
                if key.endswith("/summary.json")
            },
            reverse=True,
        )
        runs = []
        for run_id in run_ids:
            run = self.get(run_id)
            if run is not None:
                runs.append(
                    EvalRunInfo(run_id=run.run_id, meta=run.meta, strategies=run.strategies)
                )
        return runs
