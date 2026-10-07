"""Prompts and output parsing for document extraction, shared by every model provider.
Pure: no AWS or HTTP imports."""

import json
import re
from typing import Any

from docqa.domain.models import DocMetadata

TRANSCRIBE_PROMPT = (
    "Transcribe this document page to Markdown. Reproduce all text exactly, in reading order. "
    "Render tables as Markdown tables with a header row. Use '#' headings for titles. "
    "Do not add commentary, summaries or text that is not on the page."
)

METADATA_PROMPT = """Read the start of this document and return ONLY a JSON object with keys:
doc_type (one of: transcript, degree_certificate, certificate, letter, id_document, other),
title, institution, person, date. Use "" when unknown. No other text.

Document:
{text}"""


_FENCE = re.compile(r"^```[\w-]*\n(.*?)\n?```$", re.DOTALL)


def clean_transcription(raw: str) -> str:
    """Vision models sometimes wrap the whole page in a ```markdown fence; drop it."""
    text = raw.strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """The first {...} in model output, tolerating code fences and surrounding prose."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_metadata(raw: str) -> DocMetadata:
    """Tolerant JSON parsing: models sometimes wrap JSON in prose or code fences."""
    data = parse_json_object(raw)
    if data is None:
        return DocMetadata()
    fields = DocMetadata.model_fields
    return DocMetadata(**{k: str(v) for k, v in data.items() if k in fields and v is not None})
