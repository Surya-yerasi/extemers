"""The agent strategy: a LangGraph state machine over the existing retrieval and models.

    plan ─► retrieve ─► grade ─┬─(sufficient, or out of attempts)─► generate ─► verify ─┬─► end
               ▲               │                                       ▲               │
               └──(missing: a better query)                            └─(unsupported, once)

LangGraph only decides *what runs next*. Model calls go through the Generator port and
retrieval through QAService.retrieve, so providers, tracing and cost tracking are shared with
every other strategy. This module is imported only when the agent strategy is used.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from docqa.domain.agent import (
    GRADE_SYSTEM,
    PLAN_SYSTEM,
    VERIFY_SYSTEM,
    AgentConfig,
    build_grade_prompt,
    build_plan_prompt,
    build_retry_prompt,
    build_verify_prompt,
    parse_grade,
    parse_plan,
    parse_verify,
)
from docqa.domain.answering import SYSTEM_PROMPT, ParsedAnswer, build_user_prompt, parse_answer
from docqa.domain.costs import CostEstimate, token_cost
from docqa.domain.retrieval import RetrievedChunk, Strategy, ranked, rrf_fuse
from docqa.domain.tracing import Tracer
from docqa.ports import Generator

DECISION_MAX_TOKENS = 200
ANSWER_MAX_TOKENS = 600

# (question, strategy, tracer) -> Retrieval; QAService.retrieve, passed in to avoid a cycle.
RetrieveFn = Callable[[str, Strategy, Tracer], Any]


@dataclass
class _Run:
    """Per-question accumulators, kept out of the graph state (not serialisable)."""

    tracer: Tracer
    tokens: dict[str, int] = field(default_factory=dict)
    cost: CostEstimate = field(default_factory=CostEstimate)


class AgentState(TypedDict, total=False):
    run: _Run
    question: str
    answer_wanted: bool
    queries: list[str]  # to search in the next retrieve step
    searched: list[str]
    chunks: list[RetrievedChunk]
    retrievals: int
    sufficient: bool
    answer: ParsedAnswer
    unsupported: str
    regenerations: int


@dataclass
class AgentOutcome:
    chunks: list[RetrievedChunk]
    answer: ParsedAnswer | None  # None when only retrieval was asked for
    tokens: dict[str, int]
    cost: CostEstimate
    queries: list[str]


class AgentRunner:
    def __init__(self, retrieve: RetrieveFn, generator: Generator, config: AgentConfig) -> None:
        self._retrieve = retrieve
        self._generator = generator
        self._config = config
        self._graph = self._build()

    # ---------------------------------------------------------------- public

    def run(self, question: str, tracer: Tracer, answer: bool = True) -> AgentOutcome:
        run = _Run(tracer)
        final: AgentState = self._graph.invoke(
            {
                "run": run,
                "question": question,
                "answer_wanted": answer,
                "searched": [],
                "chunks": [],
                "retrievals": 0,
                "regenerations": 0,
            }
        )
        return AgentOutcome(
            chunks=final["chunks"],
            answer=final.get("answer"),
            tokens=run.tokens,
            cost=run.cost,
            queries=final["searched"],
        )

    def mermaid(self) -> str:
        """The compiled graph as Mermaid, for the docs."""
        return str(self._graph.get_graph().draw_mermaid())

    # ---------------------------------------------------------------- graph

    def _build(self) -> Any:
        graph = StateGraph(AgentState)
        graph.add_node("plan", self._plan)
        graph.add_node("retrieve", self._retrieve_node)
        graph.add_node("grade", self._grade)
        graph.add_node("generate", self._generate)
        graph.add_node("verify", self._verify)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "retrieve")
        graph.add_edge("retrieve", "grade")
        graph.add_conditional_edges(
            "grade", self._after_grade, {"retrieve": "retrieve", "generate": "generate", END: END}
        )
        graph.add_conditional_edges(
            "generate", self._after_generate, {"verify": "verify", END: END}
        )
        graph.add_conditional_edges(
            "verify", self._after_verify, {"generate": "generate", END: END}
        )
        return graph.compile()

    def _call(self, run: _Run, step: str, system: str, prompt: str) -> str:
        """One model call, with its tokens and cost added to the run. Decisions are JSON."""
        answer_step = step == "generate"
        generation = self._generator.generate(
            system,
            prompt,
            ANSWER_MAX_TOKENS if answer_step else DECISION_MAX_TOKENS,
            json_mode=not answer_step,
        )
        for kind, count in (
            ("input", generation.input_tokens),
            ("output", generation.output_tokens),
        ):
            run.tokens[f"{step}_{kind}"] = run.tokens.get(f"{step}_{kind}", 0) + count
        run.cost = run.cost.add(
            token_cost(self._generator.model_id, generation.input_tokens, generation.output_tokens)
        )
        return generation.text

    def _plan(self, state: AgentState) -> AgentState:
        run, question = state["run"], state["question"]
        with run.tracer.span("plan", model=self._generator.model_id) as span:
            raw = self._call(run, "plan", PLAN_SYSTEM, build_plan_prompt(question))
            queries = parse_plan(raw, question)
            span.set(queries=queries)
        return {"queries": queries}

    def _retrieve_node(self, state: AgentState) -> AgentState:
        run = state["run"]
        new = [q for q in state["queries"] if q not in state["searched"]]
        with run.tracer.span("retrieve", round=state["retrievals"] + 1, queries=new) as span:
            lists: list[Sequence[RetrievedChunk]] = []
            if state["chunks"]:  # keep what earlier rounds found in the running
                lists.append(state["chunks"])
            for query in new:
                retrieval = self._retrieve(query, Strategy.HYBRID, run.tracer)
                lists.append(retrieval.chunks)
                for key, value in retrieval.tokens.items():
                    run.tokens[key] = run.tokens.get(key, 0) + value
                run.cost = run.cost.add(retrieval.cost)
            fused = rrf_fuse(lists, limit=self._config.top_k) if len(lists) > 1 else lists[0]
            # One fused ranking across queries; "agent" rank = position in the final list.
            chunks = ranked(list(fused)[: self._config.top_k], "agent")
            span.set(chunks=[f"{c.source_key.rsplit('/', 1)[-1]}#{c.chunk_id}" for c in chunks])
        return {
            "chunks": chunks,
            "searched": [*state["searched"], *new],
            "retrievals": state["retrievals"] + 1,
        }

    def _grade(self, state: AgentState) -> AgentState:
        run, chunks = state["run"], state["chunks"]
        with run.tracer.span("grade", model=self._generator.model_id) as span:
            # Asked even when nothing was found: that is when a reworded search helps most.
            raw = self._call(
                run, "grade", GRADE_SYSTEM, build_grade_prompt(state["question"], chunks)
            )
            grade = parse_grade(raw)
            if not chunks:  # nothing to answer from, whatever the model says
                grade = grade.model_copy(update={"sufficient": False})
                span.set(reason="no results")
            span.set(**grade.model_dump())
        retry = [grade.query] if grade.query and grade.query not in state["searched"] else []
        return {"sufficient": grade.sufficient, "queries": retry}

    def _after_grade(self, state: AgentState) -> str:
        can_retry = state["retrievals"] < self._config.max_retrievals and bool(state["queries"])
        if not state["sufficient"] and can_retry:
            return "retrieve"
        return "generate" if state["answer_wanted"] else END

    def _generate(self, state: AgentState) -> AgentState:
        run, question, chunks = state["run"], state["question"], state["chunks"]
        retry = "unsupported" in state
        with run.tracer.span(
            "generate", model=self._generator.model_id, sources=len(chunks), retry=retry
        ) as span:
            if not chunks:
                answer = parse_answer("", chunks)  # nothing found: "not found", no model call
            else:
                prompt = (
                    build_retry_prompt(question, chunks, state["unsupported"])
                    if retry
                    else build_user_prompt(question, chunks)
                )
                answer = parse_answer(self._call(run, "generate", SYSTEM_PROMPT, prompt), chunks)
            span.set(not_found=answer.not_found, citations=[c.number for c in answer.citations])
        return {"answer": answer, "regenerations": state["regenerations"] + int(retry)}

    def _after_generate(self, state: AgentState) -> str:
        needs_check = self._config.verify and not state["answer"].not_found
        return "verify" if needs_check and state["regenerations"] == 0 else END

    def _verify(self, state: AgentState) -> AgentState:
        run = state["run"]
        with run.tracer.span("verify", model=self._generator.model_id) as span:
            raw = self._call(
                run,
                "verify",
                VERIFY_SYSTEM,
                build_verify_prompt(state["question"], state["chunks"], state["answer"].text),
            )
            verification = parse_verify(raw)
            span.set(**verification.model_dump())
        if verification.supported:
            return {}
        return {"unsupported": verification.unsupported}

    def _after_verify(self, state: AgentState) -> str:
        return "generate" if "unsupported" in state and state["regenerations"] == 0 else END
