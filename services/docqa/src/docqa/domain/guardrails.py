"""Guardrails: rule-based checks on the question (input) and the answer (output).

Deliberately simple and deterministic: free, fast (microseconds) and explainable, so every
finding can be shown with the rule that fired. Production systems layer model-based
classifiers on top (e.g. Bedrock Guardrails); the findings and metrics stay the same shape.

Actions:
- blocked: the question is refused before any retrieval or model call
- redacted: part of the answer was replaced with "[redacted]"
- flagged: allowed through, but recorded (a quality or safety signal to review)
"""

import re
from typing import Literal

from pydantic import BaseModel

Stage = Literal["input", "output"]
Action = Literal["blocked", "redacted", "flagged"]

BLOCKED_MESSAGE = "I can only answer questions about your documents."
REDACTED = "[redacted]"

# Common prompt-injection phrasings: attempts to override the instructions or extract them.
_INJECTION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|the)\b"
        r".{0,20}\b(instructions?|rules|prompts?|directions)\b",
        r"\b(system|developer|hidden|initial)\s+(prompt|instructions?|message)\b",
        r"\b(reveal|show|print|repeat|output)\b.{0,30}\b(your|the)\s+(prompt|instructions|rules)\b",
        r"\byou\s+are\s+now\b",
        r"\b(jailbreak|DAN mode|developer mode|god mode)\b",
        r"\bpretend\s+(to\s+be|you\s+are)\b",
        r"\bact\s+as\s+(an?\s+)?(unrestricted|unfiltered|uncensored|different)\b",
    )
]
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


class Finding(BaseModel):
    rule: str  # prompt_injection | sensitive_id | uncited_answer
    stage: Stage
    action: Action
    detail: str  # what fired, without echoing the sensitive value


def check_input(question: str) -> list[Finding]:
    for pattern in _INJECTION:
        if pattern.search(question):
            return [
                Finding(
                    rule="prompt_injection",
                    stage="input",
                    action="blocked",
                    detail="the question tries to change or reveal the assistant's instructions",
                )
            ]
    return []


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2  # Luhn: doubled digits above 9 lose 9
        total += d
    return total % 10 == 0


def check_output(answer: str, not_found: bool, citations: int) -> tuple[str, list[Finding]]:
    """The answer to show (possibly redacted) and what fired."""
    findings: list[Finding] = []
    redacted, ssns = _SSN.subn(REDACTED, answer)

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        return REDACTED if _luhn(digits) else match.group()

    redacted_cards = _CARD.sub(card, redacted)
    cards = redacted_cards.count(REDACTED) - ssns
    if ssns or cards:
        kinds = [f"{n} {k}" for n, k in ((ssns, "SSN"), (cards, "card number")) if n]
        findings.append(
            Finding(
                rule="sensitive_id",
                stage="output",
                action="redacted",
                detail=f"removed {' and '.join(kinds)} from the answer",
            )
        )
    if not not_found and citations == 0:
        findings.append(
            Finding(
                rule="uncited_answer",
                stage="output",
                action="flagged",
                detail="the answer cites no source, so nothing shows where it came from",
            )
        )
    return redacted_cards, findings
