"""Output parsing for document extraction, shared by every model provider.
Pure: no AWS or HTTP imports."""

import json
import re
from typing import Any

from docqa.domain.models import DocMetadata

# The prompts are prompts/transcribe.md and prompts/metadata.md (versioned).


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
