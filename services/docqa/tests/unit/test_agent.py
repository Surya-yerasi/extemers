import json
from collections.abc import Sequence

import pytest

from docqa.domain.agent import (
    GRADE_SYSTEM,
    PLAN_SYSTEM,
    VERIFY_SYSTEM,
    AgentConfig,
    build_retry_prompt,
    parse_grade,
    parse_plan,
    parse_verify,
)
from docqa.domain.answering import NOT_FOUND, SYSTEM_PROMPT
from docqa.domain.retrieval import Strategy
from docqa.domain.tracing import Tracer
from docqa.pipelines.qa import QAService
from docqa.ports import Generation
from tests.fakes import FakeEmbedder, FakeReranker, FakeSearcher, retrieved


class ScriptedModel:
    """Replies per step (by system prompt), in order; records what each step was sent."""

    model_id = "us.amazon.nova-micro-v1:0"

    def __init__(self, **replies: Sequence[str]) -> None:
        self.replies = {name: list(texts) for name, texts in replies.items()}
        self.calls: list[tuple[str, bool]] = []
        self.prompts: dict[str, list[str]] = {}

    def generate(
        self, system: str, prompt: str, max_tokens: int, json_mode: bool = False
    ) -> Generation:
        step = {
            PLAN_SYSTEM: "plan",
            GRADE_SYSTEM: "grade",
            VERIFY_SYSTEM: "verify",
            SYSTEM_PROMPT: "generate",
        }[system]
        self.calls.append((step, json_mode))
        self.prompts.setdefault(step, []).append(prompt)
        queue = self.replies[step]
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        return Generation(text=text, input_tokens=100, output_tokens=10)


def plan(*queries: str) -> str:
    return json.dumps({"queries": list(queries)})


def grade(sufficient: bool, query: str = "") -> str:
    return json.dumps({"sufficient": sufficient, "missing": "x" if query else "", "query": query})


def verify(supported: bool, claim: str = "") -> str:
    return json.dumps({"supported": supported, "unsupported": claim})


class QuerySearcher(FakeSearcher):
    """BM25 results depend on the query text, so the plan's queries matter."""

    def __init__(self, by_word: dict[str, str]) -> None:
        super().__init__()
        self.by_word = by_word
        self.queries: list[str] = []

    def text_search(self, query: str, limit: int):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        hits = [
            retrieved(cid, f"text {cid}", doc_id=cid)
            for word, cid in self.by_word.items()
            if word in query.lower()
        ]
        return [
            c.model_copy(update={"scores": {"bm25": 1.0}, "ranks": {"bm25": i}})
            for i, c in enumerate(hits, 1)
        ]


def service(model: ScriptedModel, searcher: FakeSearcher, **config: object) -> QAService:
    return QAService(
        searcher=searcher,
        embedder=FakeEmbedder(),
        embedding_model_id="amazon.titan-embed-text-v2:0",
        generator=model,
        reranker=FakeReranker(),
        agent_config=AgentConfig.model_validate(config),
    )


# ------------------------------------------------------------------ parsers


def test_parse_plan_keeps_question_first_and_dedupes() -> None:
    planned = ["offer letter salary", "Offer letter salary", "  current   salary ", "", "a", "b"]
    raw = "Sure! " + json.dumps({"queries": planned})
    assert parse_plan(raw, "q?") == ["q?", "offer letter salary", "current salary"]
    assert parse_plan("no json", "q?") == ["q?"]
    assert parse_plan('{"queries": "not a list"}', "q?") == ["q?"]


def test_parse_grade_and_verify_fall_back_safely() -> None:
    assert parse_grade(grade(False, "better")).model_dump() == {
        "sufficient": False, "missing": "x", "query": "better"
    }  # fmt: skip
    assert parse_grade("garbage").sufficient is True  # never loop on a bad decision
    assert parse_grade('{"sufficient": "no"}').sufficient is True
    assert parse_verify(verify(False, "GPA 4.0")).unsupported == "GPA 4.0"
    assert parse_verify("{}").supported is True


def test_retry_prompt_names_the_unsupported_claim() -> None:
    prompt = build_retry_prompt("q?", [retrieved("a")], "GPA 4.0")
    assert "unsupported by the sources: GPA 4.0" in prompt
    assert f"or reply {NOT_FOUND}" in prompt


# ------------------------------------------------------------------ graph paths


def test_happy_path_plans_retrieves_grades_generates_verifies() -> None:
    model = ScriptedModel(
        plan=[plan("offer salary", "current salary")],
        grade=[grade(True)],
        generate=["Up $17,500 [1][2]."],
        verify=[verify(True)],
    )
    searcher = QuerySearcher({"offer": "offer", "current": "current"})
    tracer = Tracer("c_0123456789abcdef.1")
    result = service(model, searcher, verify=True).ask(
        "How much did my salary rise?", Strategy.AGENT, tracer=tracer
    )

    assert result.answer == "Up $17,500 [1][2]."
    assert {c.chunk_id for c in result.citations} == {"offer", "current"}
    assert searcher.queries == ["How much did my salary rise?", "offer salary", "current salary"]
    assert model.calls == [("plan", True), ("grade", True), ("generate", False), ("verify", True)]
    assert result.chunks[0].ranks["agent"] == 1
    names = [(s.depth, s.name) for s in result.trace.spans]  # type: ignore[union-attr]
    assert names[0] == (0, "plan")
    assert (0, "retrieve") in names
    assert (1, "embed") in names  # retrieval nested under the agent's retrieve step
    assert [n for d, n in names if d == 0] == ["plan", "retrieve", "grade", "generate", "verify"]
    assert result.tokens["plan_input"] == 100
    assert result.tokens["verify_output"] == 10
    assert result.cost.usd > 0
    assert result.trace.attributes["agent_queries"][0] == "How much did my salary rise?"  # type: ignore[union-attr]


def test_insufficient_results_trigger_one_corrective_search() -> None:
    model = ScriptedModel(
        plan=[plan()],
        grade=[grade(False, "current salary"), grade(False, "still missing")],
        generate=["$96,000 [1]."],
        verify=[verify(True)],
    )
    searcher = QuerySearcher({"current": "current"})
    result = service(model, searcher, max_retrievals=2).ask("What do I earn?", Strategy.AGENT)
    assert searcher.queries == ["What do I earn?", "current salary"]  # no third round
    assert [c for c, _ in model.calls] == ["plan", "grade", "grade", "generate"]  # verify off
    assert result.chunks[0].chunk_id == "current"
    rounds = [s.attributes["round"] for s in result.trace.spans if s.name == "retrieve"]  # type: ignore[union-attr]
    assert rounds == [1, 2]


def test_no_retry_when_the_grader_repeats_a_searched_query() -> None:
    model = ScriptedModel(
        plan=[plan()],
        grade=[grade(False, "What do I earn?")],
        generate=["x [1]"],
        verify=[verify(True)],
    )
    searcher = QuerySearcher({"earn": "a"})
    service(model, searcher).ask("What do I earn?", Strategy.AGENT)
    assert searcher.queries == ["What do I earn?"]


def test_unsupported_answer_is_regenerated_once() -> None:
    model = ScriptedModel(
        plan=[plan()],
        grade=[grade(True)],
        generate=["GPA 4.0 [1].", "GPA 3.86 [1]."],
        verify=[verify(False, "GPA 4.0"), verify(False, "still bad")],
    )
    result = service(model, QuerySearcher({"gpa": "t"}), verify=True).ask("my gpa?", Strategy.AGENT)
    assert result.answer == "GPA 3.86 [1]."
    assert [c for c, _ in model.calls] == ["plan", "grade", "generate", "verify", "generate"]
    assert "unsupported by the sources: GPA 4.0" in model.prompts["generate"][1]
    retry = [s for s in result.trace.spans if s.name == "generate"]  # type: ignore[union-attr]
    assert [s.attributes["retry"] for s in retry] == [False, True]


def test_verify_can_be_switched_off_and_not_found_skips_it() -> None:
    model = ScriptedModel(
        plan=[plan()], grade=[grade(True)], generate=["x [1]"], verify=[verify(True)]
    )
    service(model, QuerySearcher({"q": "a"}), verify=False).ask("q", Strategy.AGENT)
    assert "verify" not in [c for c, _ in model.calls]

    model = ScriptedModel(
        plan=[plan()], grade=[grade(True)], generate=[NOT_FOUND], verify=[verify(True)]
    )
    result = service(model, QuerySearcher({"q": "a"}), verify=True).ask("q", Strategy.AGENT)
    assert result.not_found
    assert "verify" not in [c for c, _ in model.calls]


def test_nothing_found_answers_not_found_without_generating() -> None:
    model = ScriptedModel(
        plan=[plan("passport")], grade=[grade(True)], generate=["x"], verify=[verify(True)]
    )
    result = service(model, QuerySearcher({})).ask("passport number?", Strategy.AGENT)
    assert result.not_found
    assert [c for c, _ in model.calls] == [
        "plan",
        "grade",
    ]  # grade short-circuits; no generate call
    grade_span = next(s for s in result.trace.spans if s.name == "grade")  # type: ignore[union-attr]
    assert grade_span.attributes["reason"] == "no results"
    assert grade_span.attributes["sufficient"] is False  # overrides the model's "true"


def test_retrieval_only_stops_before_generating() -> None:
    model = ScriptedModel(
        plan=[plan("current salary")], grade=[grade(True)], generate=["x"], verify=["x"]
    )
    searcher = QuerySearcher({"current": "current"})
    retrieval = service(model, searcher).retrieve("What do I earn?", Strategy.AGENT)
    assert [c.chunk_id for c in retrieval.chunks] == ["current"]
    assert [c for c, _ in model.calls] == ["plan", "grade"]
    assert retrieval.cost.usd > 0
    assert {"plan", "retrieve", "grade"} <= set(retrieval.timings_ms)


def test_agent_works_inside_a_conversation() -> None:
    from docqa.adapters.conversation_store import BlobConversationStore  # noqa: PLC0415
    from docqa.pipelines.chat import ChatService  # noqa: PLC0415
    from tests.fakes import MemoryBlobStore  # noqa: PLC0415

    model = ScriptedModel(
        plan=[plan()], grade=[grade(True)], generate=["GPA 3.86 [1]."], verify=[verify(True)]
    )
    chat = ChatService(
        service(model, QuerySearcher({"gpa": "t"})), BlobConversationStore(MemoryBlobStore())
    )
    _, turn = chat.ask("u1", "my gpa?", Strategy.AGENT)
    assert turn.result is not None
    assert turn.result.answer == "GPA 3.86 [1]."
    assert turn.trace.trace_id == turn.turn_id


def test_mermaid_export_shows_the_graph() -> None:
    from docqa.pipelines.agent import AgentRunner  # noqa: PLC0415

    model = ScriptedModel(plan=[plan()], grade=[grade(True)], generate=["x"], verify=[verify(True)])
    qa = service(model, QuerySearcher({}))
    diagram = AgentRunner(qa.retrieve, model, AgentConfig()).mermaid()
    for node in ("plan", "retrieve", "grade", "generate", "verify"):
        assert node in diagram


@pytest.mark.parametrize("strategy", [s for s in Strategy if s != Strategy.AGENT])
def test_other_strategies_never_load_langgraph(strategy: Strategy) -> None:
    model = ScriptedModel(generate=["x [1]"])
    qa = service(model, QuerySearcher({"q": "a"}))
    qa.ask("q", strategy)
    assert qa._agent_runner is None
