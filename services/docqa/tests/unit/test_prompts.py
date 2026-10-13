"""Versioned prompts: the files, the lock that catches unversioned edits, overrides, and the
prompt IDs recorded on traces, ingest records, eval runs and the dashboard."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docqa import cli
from docqa.domain import prompts
from docqa.domain.answering import NOT_FOUND, NOT_FOUND_MESSAGE, parse_answer
from docqa.domain.conversation import Exchange
from docqa.domain.models import doc_id_for
from docqa.domain.prompts import (
    ENV_OVERRIDES,
    PROMPT_NAMES,
    available,
    load_prompt,
    parse_overrides,
    parse_prompt_file,
    prompt,
    prompt_ids,
    read_lock,
    render_lock,
    use_versions,
)
from docqa.domain.retrieval import Strategy
from docqa.pipelines.ingest import IngestService, parsed_key
from docqa.pipelines.metrics import UNTRACKED_ANSWER_PROMPT, compute_live_metrics
from docqa.pipelines.qa import QAService
from tests.conftest_samples import sample
from tests.fakes import (
    FakeEmbedder,
    FakeGenerator,
    FakeMetadata,
    FakeReranker,
    FakeSearcher,
    FakeVision,
    MemoryBlobStore,
    MemoryIndex,
    chat_service,
    retrieved,
)

PROMPT_DIR = Path(prompts.__file__).resolve().parents[1] / "prompts"


@pytest.fixture(autouse=True)
def _active_prompts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts and ends on the active versions."""
    monkeypatch.delenv(ENV_OVERRIDES, raising=False)
    prompt.cache_clear()
    yield
    prompt.cache_clear()


# ------------------------------------------------------------------ the files


def test_every_prompt_has_an_active_file_named_after_it() -> None:
    files = {p.name for p in PROMPT_DIR.glob("*.md")}
    for name in PROMPT_NAMES:
        assert f"{name}.md" in files, name
    for path in PROMPT_DIR.glob("*.md"):
        p = parse_prompt_file(path.read_text())
        match = re.fullmatch(rf"{p.name}(?:\.v(\d+))?\.md", path.name)
        assert match, f"{path.name}: header name is {p.name!r}"
        if match.group(1):
            assert int(match.group(1)) == p.version, f"{path.name}: header says v{p.version}"
        assert p.text
        assert p.changelog, f"{path.name}: add a changelog entry"
        assert p.changelog[0].startswith(f"{p.version}:"), f"{path.name}: newest entry first"


def test_active_version_is_the_newest() -> None:
    for name in PROMPT_NAMES:
        versions = [p.version for p in available() if p.name == name]
        assert len(versions) == len(set(versions)), f"{name}: duplicate versions"
        assert load_prompt(name).version == max(versions), name


def test_no_prompt_changed_without_a_new_version() -> None:
    """The guard against silent prompt edits. If this fails:
    - you edited a prompt: bump its version, keep the old text as prompts/<name>.vN.md, then
    - record the new version: make docqa-prompts LOCK=1 (and commit prompts.lock.json)."""
    lock = read_lock()
    for p in available():
        locked = lock.get(p.name, {}).get(str(p.version))
        assert locked is not None, (
            f"{p.id} is not in prompts.lock.json: run make docqa-prompts LOCK=1"
        )
        assert locked == p.digest, (
            f"{p.name} v{p.version} changed after it was recorded: bump the version "
            "(keep the old text as prompts/<name>.vN.md), then make docqa-prompts LOCK=1"
        )


def test_prompts_keep_the_markers_the_code_depends_on() -> None:
    for p in available():
        if p.name == "answer":
            assert NOT_FOUND in p.text, p.id
        if p.name == "metadata":
            assert "{text}" in p.text, p.id


# ------------------------------------------------------------------ parsing and IDs


def test_parse_prompt_file_and_id() -> None:
    p = parse_prompt_file(
        "---\nname: answer\nversion: 3\ndescription: d: with colon\nchangelog:\n"
        "  - 3: newer: detail\n  - 2: older\n---\n\nLine one\nLine two\n\n"
    )
    assert (p.name, p.version, p.description) == ("answer", 3, "d: with colon")
    assert p.changelog == ["3: newer: detail", "2: older"]
    assert p.text == "Line one\nLine two"
    assert re.fullmatch(r"answer@3#[0-9a-f]{6}", p.id)
    assert p.digest.startswith(p.id.split("#")[1])


@pytest.mark.parametrize(
    "content",
    ["no header", "---\nname: x\nversion: 1\n", "---\nname: x\nversion: 1\n---\ntext"],
)
def test_parse_prompt_file_rejects_bad_headers(content: str) -> None:
    with pytest.raises(ValueError, match="header"):
        parse_prompt_file(content)


def test_load_prompt_versions() -> None:
    assert load_prompt("answer", 1).version == 1
    assert load_prompt("answer", load_prompt("answer").version) == load_prompt("answer")
    with pytest.raises(ValueError, match="no version 99"):
        load_prompt("answer", 99)
    with pytest.raises(ValueError, match="unknown prompt"):
        load_prompt("vibes")


def test_overrides() -> None:
    assert parse_overrides("answer@1, plan@2") == {"answer": 1, "plan": 2}
    assert parse_overrides("") == {}
    for bad in ("answer", "answer@x", "vibes@1"):
        with pytest.raises(ValueError, match="bad prompt override"):
            parse_overrides(bad)


def test_use_versions_switches_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    active = prompt("answer")
    use_versions("answer@1")
    assert prompt("answer").version == 1
    assert prompt_ids(("answer",))["answer"] == load_prompt("answer", 1).id
    use_versions("")
    assert prompt("answer") == active


def test_render_lock_lists_every_version() -> None:
    lock = render_lock(available())
    assert '"answer": {' in lock
    assert load_prompt("answer", 1).digest in lock


# ------------------------------------------------------------------ the GPA fix: parsing


@pytest.mark.parametrize(
    ("raw", "not_found", "text"),
    [
        # general knowledge with a stray marker and no citation: not found
        ("GPA stands for Grade Point Average [NOT_FOUND].", True, NOT_FOUND_MESSAGE),
        ("GPA stands for Grade Point Average. NOT_FOUND", True, NOT_FOUND_MESSAGE),
        # a cited fact plus a marker for a missing part: keep the cited part
        (
            "Your GPA was 3.86 [1]. The honours: NOT_FOUND",
            False,
            "Your GPA was 3.86 [1]. The honours:",
        ),
        ("NOT_FOUND", True, NOT_FOUND_MESSAGE),
        ("Your GPA was 3.86 [1].", False, "Your GPA was 3.86 [1]."),
    ],
)
def test_stray_not_found_markers(raw: str, not_found: bool, text: str) -> None:
    parsed = parse_answer(raw, [retrieved("a", "GPA 3.86")])
    assert parsed.not_found is not_found
    assert parsed.text == text


# ------------------------------------------------------------------ IDs on traces


def qa() -> QAService:
    return QAService(
        searcher=FakeSearcher(
            dense=[retrieved("a", "GPA 3.86")], bm25=[retrieved("a", "GPA 3.86")]
        ),
        embedder=FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=FakeGenerator("GPA 3.86 [1]."),
        reranker=FakeReranker(),
    )


def test_trace_records_prompt_ids() -> None:
    result = qa().ask("GPA?", Strategy.HYBRID, [Exchange(question="q", answer="a")])
    assert result.trace is not None
    assert result.trace.attributes["prompts"] == prompt_ids(("rewrite", "answer"))
    spans = {s.name: s for s in result.trace.spans}
    assert spans["generate"].attributes["prompt"] == prompt("answer").id
    assert spans["rewrite"].attributes["prompt"] == prompt("rewrite").id


def test_agent_trace_lists_the_agent_prompts() -> None:
    from docqa.pipelines.qa import _prompts_for  # noqa: PLC0415

    assert _prompts_for(Strategy.AGENT, []) == ("plan", "grade", "answer", "verify")
    assert _prompts_for(Strategy.BM25, [Exchange(question="q", answer="a")]) == (
        "rewrite",
        "answer",
    )


def test_ingest_records_the_parse_prompts() -> None:
    blobs = MemoryBlobStore({"raw/award.pdf": sample("scholarship_award_letter_scanned.pdf")})
    service = IngestService(
        blobs=blobs,
        vision=FakeVision(),
        metadata=FakeMetadata(),
        embedder=FakeEmbedder(),
        index=MemoryIndex(),
    )
    service.ingest("raw/award.pdf")
    from docqa.domain.models import ParsedDocument  # noqa: PLC0415

    doc = ParsedDocument.model_validate_json(blobs.objects[parsed_key(doc_id_for("raw/award.pdf"))])
    assert doc.prompts == prompt_ids(("transcribe", "metadata"))


def test_live_metrics_group_by_answer_prompt() -> None:
    chat = chat_service()
    _, tracked = chat.ask("u1", "GPA?", Strategy.HYBRID)
    _, legacy = chat.ask("u1", "GPA again?", Strategy.HYBRID)
    del legacy.trace.attributes["prompts"]  # as stored before prompt versioning
    rows = {r.prompt: r for r in compute_live_metrics([tracked, legacy], "all", None).by_prompt}
    assert set(rows) == {prompt("answer").id, UNTRACKED_ANSWER_PROMPT}
    assert rows[prompt("answer").id].questions == 1
    assert rows[prompt("answer").id].answer_rate == 1.0


# ------------------------------------------------------------------ CLI


def test_cli_prompts_lists_and_locks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "prompts.lock.json"
    monkeypatch.setattr(cli, "LOCK_FILE", lock)
    assert cli.main(["prompts", "--lock"]) == 0
    out = capsys.readouterr().out
    assert prompt("answer").id in out
    assert lock.read_text() == render_lock(available())
    assert datetime.now(UTC)  # no settings or AWS needed: nothing else was configured


def test_cli_eval_records_the_prompts_and_can_use_an_older_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json  # noqa: PLC0415

    from tests.unit.evals.test_runner import ITEMS  # noqa: PLC0415
    from tests.unit.evals.test_runner import qa as eval_qa  # noqa: PLC0415

    monkeypatch.setenv("DOCQA_DOCS_BUCKET", "bucket")
    monkeypatch.setattr(cli, "build_qa_service", lambda _s, _b: eval_qa())
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text("".join(i.model_dump_json() + "\n" for i in ITEMS))
    args = ["eval", "--dataset", str(dataset), "--strategies", "dense"]
    cli.main([*args, "--out", str(tmp_path / "now")])
    cli.main([*args, "--out", str(tmp_path / "before"), "--prompts", "answer@1"])
    now = json.loads((tmp_path / "now" / "meta.json").read_text())["prompts"]
    before = json.loads((tmp_path / "before" / "meta.json").read_text())["prompts"]
    assert load_prompt("answer").id in now
    assert load_prompt("answer", 1).id in before
