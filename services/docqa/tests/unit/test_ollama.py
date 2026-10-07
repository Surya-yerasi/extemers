import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from docqa.adapters.ollama import (
    OllamaClient,
    OllamaEmbedder,
    OllamaGenerator,
    OllamaMetadataExtractor,
    OllamaVisionTranscriber,
    PassthroughReranker,
)
from docqa.domain.costs import rerank_cost, token_cost
from docqa.ports import ModelUnavailableError
from tests.fakes import retrieved

Handler = Callable[[httpx.Request], httpx.Response]


def client_with(handler: Handler, sent: list[dict[str, Any]] | None = None) -> OllamaClient:
    def record(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append({"path": request.url.path, **json.loads(request.content)})
        return handler(request)

    return OllamaClient("http://ollama:11434/", httpx.Client(transport=httpx.MockTransport(record)))


def chat_reply(content: str, **extra: Any) -> Handler:
    return lambda _r: httpx.Response(200, json={"message": {"content": content}, **extra})


def test_embedder_batches_and_checks_dimensions() -> None:
    sent: list[dict[str, Any]] = []
    client = client_with(lambda _r: httpx.Response(200, json={"embeddings": [[0.1] * 4] * 2}), sent)
    embedder = OllamaEmbedder(client, "bge-m3", 4)
    assert embedder.embed(["a", "b"]) == [[0.1] * 4] * 2
    assert sent == [{"path": "/api/embed", "model": "bge-m3", "input": ["a", "b"]}]
    assert embedder.model_id == "local/bge-m3"
    assert embedder.dimensions == 4
    with pytest.raises(ValueError, match="returns 4 dimensions, the index expects 8"):
        OllamaEmbedder(client, "bge-m3", 8).embed(["a"])


def test_generator_sends_system_and_limits_and_reports_tokens() -> None:
    sent: list[dict[str, Any]] = []
    client = client_with(chat_reply(" GPA 3.86 [1]. ", prompt_eval_count=900, eval_count=12), sent)
    generation = OllamaGenerator(client, "qwen2.5:7b").generate("rules", "question", 600)
    assert generation.model_dump() == {
        "text": "GPA 3.86 [1].",
        "input_tokens": 900,
        "output_tokens": 12,
    }
    request = sent[0]
    assert request["path"] == "/api/chat"
    assert request["stream"] is False
    assert request["messages"][0] == {"role": "system", "content": "rules"}
    assert request["options"] == {"temperature": 0, "num_predict": 600, "num_ctx": 8192}
    assert "format" not in request


def test_vision_sends_base64_image() -> None:
    sent: list[dict[str, Any]] = []
    client = client_with(chat_reply("# Page"), sent)
    assert OllamaVisionTranscriber(client, "qwen2.5vl:7b").transcribe(b"PNG", "png") == "# Page"
    assert sent[0]["messages"][0]["images"] == [base64.b64encode(b"PNG").decode()]


def test_metadata_uses_json_mode_and_tolerant_parsing() -> None:
    sent: list[dict[str, Any]] = []
    client = client_with(chat_reply('{"doc_type": "transcript", "person": "Alex"}'), sent)
    meta = OllamaMetadataExtractor(client, "qwen2.5:7b", max_input_chars=5).extract("x" * 50)
    assert (meta.doc_type, meta.person) == ("transcript", "Alex")
    assert sent[0]["format"] == "json"
    assert "x" * 6 not in sent[0]["messages"][0]["content"]


def test_missing_reply_fields_are_tolerated() -> None:
    client = client_with(lambda _r: httpx.Response(200, json={}))
    generation = OllamaGenerator(client, "m").generate("s", "p", 10)
    assert (generation.text, generation.input_tokens, generation.output_tokens) == ("", 0, 0)


def test_missing_model_is_explained() -> None:
    client = client_with(lambda _r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ModelUnavailableError, match=r"ollama pull qwen2.5:7b"):
        OllamaGenerator(client, "qwen2.5:7b").generate("s", "p", 10)


def test_ollama_not_running_is_explained() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelUnavailableError, match="brew services start ollama"):
        OllamaEmbedder(client_with(refuse), "bge-m3", 4).embed(["a"])


def test_other_http_errors_propagate() -> None:
    client = client_with(lambda _r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        OllamaGenerator(client, "m").generate("s", "p", 10)


def test_passthrough_reranker_keeps_order() -> None:
    out = PassthroughReranker().rerank("q", [retrieved("a"), retrieved("b"), retrieved("c")], 2)
    assert [c.chunk_id for c in out] == ["a", "b"]
    assert [c.ranks["rerank"] for c in out] == [1, 2]


def test_local_models_cost_nothing() -> None:
    assert token_cost("local/qwen2.5:7b", 10**6, 10**6).model_dump() == {
        "usd": 0.0,
        "unpriced_models": [],
    }
    assert rerank_cost(PassthroughReranker.model_id).usd == 0


def test_generator_json_mode_asks_ollama_for_json() -> None:
    sent: list[dict[str, Any]] = []
    client = client_with(chat_reply('{"queries": []}'), sent)
    OllamaGenerator(client, "qwen2.5:7b").generate("s", "p", 50, json_mode=True)
    assert sent[0]["format"] == "json"
