"""LLM-as-judge definitions shared by offline evals and online (sampled traffic) judging.

Online there is no reference answer, so only reference-free metrics can run there:
faithfulness, answer relevancy, context precision (judged against the answer instead of a
reference) and coherence. Context recall and answer correctness stay offline.
"""

from datetime import datetime

from pydantic import BaseModel

from docqa.domain.extraction import parse_json_object

ONLINE_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "coherence")
HALLUCINATION_BELOW = 1.0  # any claim the context does not support makes it a hallucination

# The rubric is prompts/coherence.md (versioned).


def build_coherence_prompt(question: str, answer: str) -> str:
    return f"Question:\n{question}\n\nAnswer:\n{answer}"


def parse_coherence(raw: str) -> float | None:
    """The 1-5 score scaled to 0-1 (so every judge metric reads the same way), or None."""
    data = parse_json_object(raw) or {}
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float):
        return None
    if not 1 <= score <= 5:  # the rubric's range
        return None
    return (float(score) - 1) / 4


def is_hallucination(faithfulness: float | None) -> bool | None:
    return None if faithfulness is None else faithfulness < HALLUCINATION_BELOW


class TurnJudgement(BaseModel):
    """Judge scores for one answered question from live traffic (0-1 each, None = failed)."""

    scores: dict[str, float | None]
    model: str
    judged_at: datetime
