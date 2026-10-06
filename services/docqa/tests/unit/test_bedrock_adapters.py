import io
import json
from typing import Any

import pytest

from docqa.adapters.bedrock import (
    BedrockMetadataExtractor,
    BedrockVisionTranscriber,
    TitanEmbedder,
    parse_metadata,
)


class StubRuntime:
    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.converse_calls: list[dict[str, Any]] = []
        self.invoke_calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.converse_calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}}}

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.invoke_calls.append(kwargs)
        dims = json.loads(kwargs["body"])["dimensions"]
        return {"body": io.BytesIO(json.dumps({"embedding": [0.1] * dims}).encode())}


def test_vision_sends_image_and_sets_max_tokens() -> None:
    runtime = StubRuntime("  # Page\n\ntext  ")
    out = BedrockVisionTranscriber("vision-model", runtime).transcribe(b"PNG", "png")
    assert out == "# Page\n\ntext"
    call = runtime.converse_calls[0]
    assert call["modelId"] == "vision-model"
    assert call["inferenceConfig"]["maxTokens"] == 4000
    image = call["messages"][0]["content"][0]["image"]
    assert image == {"format": "png", "source": {"bytes": b"PNG"}}


def test_vision_returns_empty_when_no_text_block() -> None:
    runtime = StubRuntime()
    runtime.converse = lambda **_: {"output": {"message": {"content": [{"image": {}}]}}}  # type: ignore[method-assign]
    assert BedrockVisionTranscriber("m", runtime).transcribe(b"x", "png") == ""


def test_metadata_extractor_truncates_input_and_parses() -> None:
    runtime = StubRuntime('{"doc_type": "transcript", "person": "Alex Rivera"}')
    meta = BedrockMetadataExtractor("meta-model", runtime, max_input_chars=10).extract("x" * 100)
    assert meta.doc_type == "transcript"
    assert meta.person == "Alex Rivera"
    prompt = runtime.converse_calls[0]["messages"][0]["content"][0]["text"]
    assert "x" * 10 in prompt
    assert "x" * 11 not in prompt
    assert runtime.converse_calls[0]["inferenceConfig"]["maxTokens"] == 300


@pytest.mark.parametrize(
    ("raw", "doc_type"),
    [
        ('```json\n{"doc_type": "letter"}\n```', "letter"),
        ('Here you go: {"doc_type": "certificate", "unknown": 1}', "certificate"),
        ("no json at all", "document"),
        ("{not valid json}", "document"),
        ('{"doc_type": null}', "document"),
    ],
)
def test_parse_metadata_tolerates_model_output(raw: str, doc_type: str) -> None:
    assert parse_metadata(raw).doc_type == doc_type


def test_parse_metadata_non_object() -> None:
    assert parse_metadata("[1, 2]").doc_type == "document"


def test_titan_embedder() -> None:
    runtime = StubRuntime()
    embedder = TitanEmbedder("titan", 256, runtime)
    vectors = embedder.embed(["a", "b"])
    assert embedder.dimensions == 256
    assert [len(v) for v in vectors] == [256, 256]
    body = json.loads(runtime.invoke_calls[0]["body"])
    assert body == {"inputText": "a", "dimensions": 256, "normalize": True}


def test_titan_rejects_unsupported_dimensions() -> None:
    with pytest.raises(ValueError, match="256, 512 or 1024"):
        TitanEmbedder("titan", 300, StubRuntime())
