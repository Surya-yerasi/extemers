"""Golden question sets (JSONL). The synthetic set lives in the repo; a private set over real
documents belongs under .data/ and is never committed."""

from pathlib import Path

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """A fact the answer needs: found in any chunk from one of `docs` containing `text`.

    Matching on text rather than chunk IDs keeps the golden set valid across chunking variants.
    """

    docs: list[str]  # file names, e.g. "transcript_northfield_state.pdf"
    text: str


class GoldenItem(BaseModel):
    id: str
    category: str  # fact, table, paraphrase, multi_doc, unanswerable
    question: str
    answer: str  # reference answer (for the LLM judge)
    # Each entry must appear in the answer; a list entry means "any one of these".
    must_contain: list[str | list[str]] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.evidence)


def load_golden(path: Path) -> list[GoldenItem]:
    items = [
        GoldenItem.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    ids = [item.id for item in items]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate golden ids: {', '.join(duplicates)}")
    return items
