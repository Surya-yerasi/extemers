"""Versioned prompts, stored as files in docqa/prompts/ and tracked by git.

    prompts/answer.md      the active version of the "answer" prompt
    prompts/answer.v1.md   an older (or draft) version, kept so evals can compare against it

Each file has a small header (name, version, description, changelog) and then the prompt text.
Every prompt has an ID, "answer@2#3f9c1a": name, version and the first 6 hex digits of the
SHA-256 of its text. The ID is recorded on every trace and eval run, so any number on the
dashboard can be traced to the exact prompt text behind it. The hash catches an edit that
forgot to bump the version; tests/unit/test_prompts.py fails on such an edit, and
prompts.lock.json records the hash of every version ever committed.

DOCQA_PROMPTS="answer@1,plan@2" switches prompts to other versions (an eval of a draft, or a
before/after comparison) without editing files.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from functools import cache
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

PROMPT_NAMES = (
    "answer", "rewrite", "plan", "grade", "verify", "transcribe", "metadata", "coherence",
)  # fmt: skip
ENV_OVERRIDES = "DOCQA_PROMPTS"
_DIR = files("docqa") / "prompts"
LOCK_FILE = Path(str(_DIR)) / "prompts.lock.json"  # written by `docqa.cli prompts --lock`


class Prompt(BaseModel):
    name: str
    version: int
    description: str
    changelog: list[str]
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}#{self.digest[:6]}"


def parse_prompt_file(content: str) -> Prompt:
    """Header between '---' lines ("key: value", changelog as "  - entry" lines), then text."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in (line.strip() for line in lines[1:]):
        raise ValueError("a prompt file starts with a '---' header block")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    header: dict[str, str] = {}
    changelog: list[str] = []
    for line in lines[1:end]:
        if line.startswith("  - "):
            changelog.append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            header[key.strip()] = value.strip()
    missing = {"name", "version", "description"} - header.keys()
    if missing:
        raise ValueError(f"prompt header is missing {', '.join(sorted(missing))}")
    return Prompt(
        name=header["name"],
        version=int(header["version"]),
        description=header["description"],
        changelog=changelog,
        text="\n".join(lines[end + 1 :]).strip(),
    )


def available() -> list[Prompt]:
    """Every prompt file: active versions and older ones."""
    return [
        parse_prompt_file(entry.read_text())
        for entry in sorted(_DIR.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".md")
    ]


def load_prompt(name: str, version: int | None = None) -> Prompt:
    """The active version (prompts/<name>.md), or a specific one (<name>.md or <name>.vN.md)."""
    if name not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt {name!r}")
    active = parse_prompt_file((_DIR / f"{name}.md").read_text())
    if version is None or version == active.version:
        return active
    other = _DIR / f"{name}.v{version}.md"
    if not other.is_file():
        raise ValueError(f"no version {version} of prompt {name!r} (expected prompts/{other.name})")
    return parse_prompt_file(other.read_text())


def parse_overrides(value: str) -> dict[str, int]:
    """'answer@1, plan@2' -> {"answer": 1, "plan": 2}."""
    overrides: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, sep, version = item.partition("@")
        if not sep or not version.isdigit() or name not in PROMPT_NAMES:
            raise ValueError(f"bad prompt override {item!r}: use name@version, e.g. answer@1")
        overrides[name] = int(version)
    return overrides


@cache
def prompt(name: str) -> Prompt:
    """The prompt this process uses: the active file, unless DOCQA_PROMPTS picks a version."""
    return load_prompt(name, parse_overrides(os.environ.get(ENV_OVERRIDES, "")).get(name))


def prompt_ids(names: tuple[str, ...] = PROMPT_NAMES) -> dict[str, str]:
    return {name: prompt(name).id for name in names}


def use_versions(overrides: str) -> None:
    """Switch prompt versions for the rest of this process (the eval CLI's --prompts)."""
    parse_overrides(overrides)  # fail fast on a typo
    os.environ[ENV_OVERRIDES] = overrides
    prompt.cache_clear()


def render_lock(prompts: Sequence[Prompt]) -> str:
    """Every committed version's full hash: an edit without a version bump changes a hash."""
    lock: dict[str, dict[str, str]] = {}
    for p in sorted(prompts, key=lambda p: (p.name, p.version)):
        lock.setdefault(p.name, {})[str(p.version)] = p.digest
    return json.dumps(lock, indent=2) + "\n"


def read_lock() -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = json.loads(LOCK_FILE.read_text())
    return data
