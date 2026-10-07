"""Prompts and output parsing for the agent's decisions. Pure: no model or graph imports.

Every parser is tolerant: a malformed decision falls back to the safe default (search the
original question; treat results as sufficient; treat the answer as supported), so a weak
model can never send the agent into a loop.
"""

from collections.abc import Sequence

from pydantic import BaseModel

from docqa.domain.answering import NOT_FOUND, build_user_prompt
from docqa.domain.extraction import parse_json_object
from docqa.domain.retrieval import RetrievedChunk

MAX_PLANNED_QUERIES = 3
MAX_QUERY_CHARS = 200

PLAN_SYSTEM = """You plan searches over one person's documents: transcripts, diplomas,
certificates, letters, employment records.
Write 1 to 3 short search queries that together find every fact the question needs.
- One fact needed: one query.
- Several facts (comparing two documents, a difference, a list): one query per fact.
Prefer words that would appear in the document itself ("paid time off", not "vacation").
Return only JSON: {"queries": ["...", "..."]}"""

GRADE_SYSTEM = """You check whether search results are enough to answer a question about
one person's documents. Set "sufficient" to true only if the numbered sources state every
fact the question needs. If a fact is missing, name it and write one better search query for it.
Return only JSON: {"sufficient": true or false, "missing": "...", "query": "..."}"""

VERIFY_SYSTEM = """You check an answer against its numbered sources. A claim is supported
only if a source states it (simple arithmetic on stated numbers is fine).
Return only JSON: {"supported": true or false, "unsupported": "first unsupported claim or empty"}"""


class AgentConfig(BaseModel):
    max_retrievals: int = 2  # search rounds: the plan, plus at most one corrective round
    # Check the answer against its sources and regenerate once if needed. Off by default:
    # on the golden set it changed no answers and added ~0.7 s at P50 (Phase 6 results).
    verify: bool = False
    top_k: int = 5


class Grade(BaseModel):
    sufficient: bool
    missing: str = ""
    query: str = ""


class Verification(BaseModel):
    supported: bool
    unsupported: str = ""


def _clean_query(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:MAX_QUERY_CHARS]


def build_plan_prompt(question: str) -> str:
    return f"Question: {question}"


def parse_plan(raw: str, question: str) -> list[str]:
    """The original question first (a safe baseline), then up to 3 planned queries, deduplicated."""
    data = parse_json_object(raw) or {}
    planned = data.get("queries")
    queries = [question]
    if isinstance(planned, list):
        for item in planned[:MAX_PLANNED_QUERIES]:
            query = _clean_query(item)
            if query and query.lower() not in {q.lower() for q in queries}:
                queries.append(query)
    return queries


def build_grade_prompt(question: str, chunks: Sequence[RetrievedChunk]) -> str:
    return build_user_prompt(question, chunks)


def parse_grade(raw: str) -> Grade:
    data = parse_json_object(raw)
    if data is None or not isinstance(data.get("sufficient"), bool):
        return Grade(sufficient=True)  # cannot tell: do not loop
    return Grade(
        sufficient=data["sufficient"],
        missing=_clean_query(data.get("missing", "")),
        query=_clean_query(data.get("query", "")),
    )


def build_verify_prompt(question: str, chunks: Sequence[RetrievedChunk], answer: str) -> str:
    return f"{build_user_prompt(question, chunks)}\n\nAnswer to check: {answer}"


def parse_verify(raw: str) -> Verification:
    data = parse_json_object(raw)
    if data is None or not isinstance(data.get("supported"), bool):
        return Verification(supported=True)  # cannot tell: keep the answer
    return Verification(
        supported=data["supported"], unsupported=str(data.get("unsupported", ""))[:300]
    )


def build_retry_prompt(question: str, chunks: Sequence[RetrievedChunk], unsupported: str) -> str:
    """Second generation attempt after verification flagged a claim."""
    return (
        f"{build_user_prompt(question, chunks)}\n\n"
        f"A reviewer found this claim in your previous answer unsupported by the sources: "
        f"{unsupported or '(unspecified)'}\n"
        f"Answer again using only what the sources state, or reply {NOT_FOUND}."
    )
