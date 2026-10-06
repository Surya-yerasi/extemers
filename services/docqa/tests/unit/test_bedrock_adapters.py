import io
import json
from typing import Any

import pytest

from docqa.adapters.bedrock import (
    BedrockGenerator,
    BedrockMetadataExtractor,
    BedrockVisionTranscriber,
    CohereReranker,
    TitanEmbedder,
)
from docqa.domain.extraction import parse_metadata
from tests.fakes import retrieved


class StubRuntime:
    def __init__(
        self,
        text: str = "ok",
        usage: dict[str, int] | None = None,
        rerank_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.text = text
        self.usage = usage
        self.rerank_results = rerank_results
        self.converse_calls: list[dict[str, Any]] = []
        self.invoke_calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.converse_calls.append(kwargs)
        response: dict[str, Any] = {"output": {"message": {"content": [{"text": self.text}]}}}
        if self.usage is not None:
            response["usage"] = self.usage
        return response

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.invoke_calls.append(kwargs)
        if self.rerank_results is not None:
            payload: dict[str, Any] = {"results": self.rerank_results}
        else:
            payload = {"embedding": [0.1] * json.loads(kwargs["body"])["dimensions"]}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


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


def test_generator_sends_system_prompt_and_reports_usage() -> None:
    runtime = StubRuntime("  Answer [1].  ", usage={"inputTokens": 812, "outputTokens": 9})
    generation = BedrockGenerator("gen-model", runtime).generate("be good", "question", 600)
    assert generation.model_dump() == {
        "text": "Answer [1].",
        "input_tokens": 812,
        "output_tokens": 9,
    }
    call = runtime.converse_calls[0]
    assert call["system"] == [{"text": "be good"}]
    assert call["inferenceConfig"] == {"maxTokens": 600, "temperature": 0}


def test_generator_tolerates_missing_usage() -> None:
    generation = BedrockGenerator("m", StubRuntime("x")).generate("s", "p", 10)
    assert (generation.input_tokens, generation.output_tokens) == (0, 0)


def test_cohere_reranker_reorders_and_keeps_earlier_scores() -> None:
    chunks = [
        retrieved("a").model_copy(update={"scores": {"rrf": 0.03}}),
        retrieved("b"),
        retrieved("c"),
    ]
    runtime = StubRuntime(
        rerank_results=[
            {"index": 2, "relevance_score": 0.91},
            {"index": 0, "relevance_score": 0.4},
        ]
    )
    out = CohereReranker("cohere.rerank-v3-5:0", runtime).rerank("gpa", chunks, top_n=2)
    assert [c.chunk_id for c in out] == ["c", "a"]
    assert out[0].scores == {"rerank": 0.91}
    assert out[1].scores == {"rrf": 0.03, "rerank": 0.4}
    assert [c.ranks["rerank"] for c in out] == [1, 2]
    body = json.loads(runtime.invoke_calls[0]["body"])
    assert body == {
        "query": "gpa",
        "documents": ["text of a", "text of b", "text of c"],
        "top_n": 2,
        "api_version": 2,
    }


def test_cohere_reranker_skips_call_without_chunks() -> None:
    runtime = StubRuntime()
    assert CohereReranker("m", runtime).rerank("q", [], 5) == []
    assert runtime.invoke_calls == []
