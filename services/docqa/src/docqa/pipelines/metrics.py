"""Online metrics: computed from stored turns and their traces (real traffic, no answer key).

Offline metrics (scored against the golden set) come from eval runs; see evals/. Every
number here can be traced back to the questions behind it, and a new metric works on all
past traffic, because it is derived from the traces rather than counted at write time.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from docqa.domain.agent_trace import AgentTraceStats, agent_trace_stats
from docqa.domain.judging import ONLINE_METRICS, is_hallucination
from docqa.domain.retrieval import Strategy
from docqa.domain.stats import mean, percentile, rate
from docqa.pipelines.chat import Turn

Window = Literal["24h", "7d", "30d", "all"]
WINDOWS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
LLM_STEPS = ("rewrite", "plan", "grade", "generate", "verify")  # spans that call a chat model
# Traces from before prompt versioning carry no IDs; the answer prompt then was answer@1 (its
# text is byte-identical to prompts/answer.v1.md, checked when the files were created).
UNTRACKED_ANSWER_PROMPT = "answer@1 (before tracking)"


class Distribution(BaseModel):
    count: int
    mean: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    max: float | None


def distribution(values: Sequence[float]) -> Distribution:
    return Distribution(
        count=len(values),
        mean=mean(values),
        p50=percentile(values, 50),
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        max=max(values) if values else None,
    )


class StageLatency(BaseModel):
    name: str
    depth: int  # 0: a top-level step; 1: inside an agent step (e.g. a search in "retrieve")
    latency_ms: Distribution
    share: float | None  # of total time, top-level steps only (they partition the time)


class TokenStep(BaseModel):
    step: str
    input_per_question: float | None
    output_per_question: float | None
    questions: int


class DailyPoint(BaseModel):
    date: str  # YYYY-MM-DD, UTC
    questions: int
    errors: int
    p95_ms: float | None


class StrategyRow(BaseModel):
    strategy: Strategy
    questions: int
    answer_rate: float | None
    not_found_rate: float | None
    error_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    model_calls: float | None
    cost_per_question: float | None
    helpful_rate: float | None


class PromptRow(BaseModel):
    """Outcomes per answer-prompt version: the before/after view for a prompt change."""

    prompt: str
    questions: int
    answer_rate: float | None
    not_found_rate: float | None
    uncited_rate: float | None  # answers flagged by the uncited-answer guardrail
    judged: int
    faithfulness: float | None
    hallucination_rate: float | None
    csat: float | None


class LiveMetrics(BaseModel):
    window: str
    strategy: str | None
    generated_at: datetime
    kpis: dict[str, float | None]
    latency_total_ms: Distribution
    stages: list[StageLatency]
    daily: list[DailyPoint]
    retrieval: dict[str, float | None]
    generation: dict[str, float | None]
    tokens_by_step: list[TokenStep]
    by_strategy: list[StrategyRow]
    models: dict[str, int]  # generation model -> questions
    judge: dict[str, float | None]  # sampled LLM-judge scores (make docqa-local-judge)
    agent: dict[str, float | None]  # agent strategy only
    conversation: dict[str, float | None]
    safety: dict[str, float | None]
    guardrail_rules: dict[str, int]  # rule -> questions where it fired
    by_prompt: list[PromptRow]


def since_for(window: str, now: datetime) -> datetime | None:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    span = WINDOWS[window]
    return now - span if span else None


def _per_name_ms(turn: Turn) -> dict[tuple[str, int], float]:
    """Total time per (step name, depth) within one question (agents repeat steps)."""
    totals: dict[tuple[str, int], float] = defaultdict(float)
    for span in turn.trace.spans:
        totals[(span.name, span.depth)] += span.duration_ms
    return totals


def _dense_top(turn: Turn) -> list[float]:
    """Scores of the first dense search's top results (best first), if there was one."""
    for span in turn.trace.spans:
        if span.name == "dense_search":
            return [
                t["score"] for t in span.attributes.get("top", []) if t.get("score") is not None
            ]
    return []


def _model_calls(turn: Turn) -> int:
    return sum(1 for s in turn.trace.spans if s.name in LLM_STEPS and s.status == "ok")


def _strategy_row(strategy: Strategy, turns: Sequence[Turn]) -> StrategyRow:
    answered = [t for t in turns if _answered(t)]
    rated = [t for t in turns if t.feedback]
    totals = [t.trace.duration_ms for t in turns]
    ok = [t for t in turns if t.result]
    return StrategyRow(
        strategy=strategy,
        questions=len(turns),
        answer_rate=rate(len(answered), len(turns)),
        not_found_rate=rate(sum(1 for t in ok if t.result and t.result.not_found), len(turns)),
        error_rate=rate(len(turns) - len(ok), len(turns)),
        p50_ms=percentile(totals, 50),
        p95_ms=percentile(totals, 95),
        model_calls=mean([float(_model_calls(t)) for t in ok]),
        cost_per_question=mean([t.result.cost.usd for t in ok if t.result]),
        helpful_rate=rate(sum(1 for t in rated if t.feedback == "up"), len(rated)),
    )


def _answered(t: Turn) -> bool:
    return t.result is not None and not t.result.not_found and not t.result.blocked


def _judge_metrics(answered: Sequence[Turn]) -> dict[str, float | None]:
    judged = [t.judge for t in answered if t.judge]
    out: dict[str, float | None] = {
        "judged": float(len(judged)),
        "judge_coverage": rate(len(judged), len(answered)),
    }
    for name in ONLINE_METRICS:
        out[name] = mean([j.scores.get(name) for j in judged])
    flags = [is_hallucination(j.scores.get("faithfulness")) for j in judged]
    scored = [f for f in flags if f is not None]
    out["hallucination_rate"] = rate(sum(scored), len(scored))
    return out


def _agent_metrics(turns: Sequence[Turn]) -> dict[str, float | None]:
    """Agent turns only. Failed turns count against completion; the rest need a trace."""
    agent = [t for t in turns if t.strategy == Strategy.AGENT]
    stats: list[tuple[Turn, AgentTraceStats]] = []
    for t in agent:
        st = agent_trace_stats(t.trace) if t.result else None
        if st is not None:
            stats.append((t, st))
    recoverable = [(t, st) for t, st in stats if st.first_search_failed]
    costs = [t.result.cost.usd for t in agent if t.result]
    return {
        "tasks": float(len(agent)),
        "task_completion_rate": rate(
            sum(1 for t in agent if _answered(t) and t.feedback != "down"), len(agent)
        ),
        "step_count": mean([float(st.steps) for _, st in stats]),
        "step_count_p95": percentile([float(st.steps) for _, st in stats], 95),
        "trajectory_efficiency": mean([st.efficiency for _, st in stats]),
        "search_accuracy": rate(
            sum(st.grades_sufficient for _, st in stats), sum(st.grades for _, st in stats)
        ),
        "error_recovery_rate": rate(
            sum(1 for t, _ in recoverable if _answered(t)), len(recoverable)
        ),
        "cost_per_task": mean(costs),
        "cost_per_task_p95": percentile(costs, 95),
    }


def _conversation_metrics(turns: Sequence[Turn]) -> dict[str, float | None]:
    """A conversation is complete when its last question was answered and not rated 👎."""
    by_conversation: dict[str, list[Turn]] = defaultdict(list)
    for t in turns:
        by_conversation[t.turn_id.split(".")[0]].append(t)
    last = [max(ts, key=lambda t: t.created_at) for ts in by_conversation.values()]
    rated = [t for t in turns if t.feedback]
    return {
        "conversations": float(len(by_conversation)),
        "turns_per_conversation": mean([float(len(ts)) for ts in by_conversation.values()]),
        "conversation_completion": rate(
            sum(1 for t in last if _answered(t) and t.feedback != "down"), len(last)
        ),
        "csat": rate(sum(1 for t in rated if t.feedback == "up"), len(rated)),
        "feedback_coverage": rate(len(rated), sum(1 for t in turns if _answered(t))),
    }


def _safety_metrics(turns: Sequence[Turn]) -> tuple[dict[str, float | None], dict[str, int]]:
    ok = [t for t in turns if t.result]
    rules: Counter[str] = Counter()
    for t in ok:
        if t.result:
            rules.update({f.rule for f in t.result.guardrails})
    return (
        {
            "guardrail_violation_rate": rate(
                sum(1 for t in ok if t.result and t.result.guardrails), len(ok)
            ),
            "guardrail_block_rate": rate(
                sum(1 for t in ok if t.result and t.result.blocked), len(ok)
            ),
        },
        dict(rules.most_common()),
    )


def answer_prompt(turn: Turn) -> str:
    prompts = turn.trace.attributes.get("prompts")
    if isinstance(prompts, dict) and prompts.get("answer"):
        return str(prompts["answer"])
    return UNTRACKED_ANSWER_PROMPT


def _prompt_rows(turns: Sequence[Turn]) -> list[PromptRow]:
    groups: dict[str, list[Turn]] = defaultdict(list)
    for t in turns:
        if t.result and t.result.blocked:
            continue  # never reached the answer prompt
        groups[answer_prompt(t)].append(t)
    rows = []
    for name, ts in groups.items():
        answered = [t for t in ts if _answered(t)]
        judged = [t.judge for t in answered if t.judge]
        flags = [is_hallucination(j.scores.get("faithfulness")) for j in judged]
        scored = [f for f in flags if f is not None]
        rated = [t for t in ts if t.feedback]
        rows.append(
            PromptRow(
                prompt=name,
                questions=len(ts),
                answer_rate=rate(len(answered), len(ts)),
                not_found_rate=rate(sum(1 for t in ts if t.result and t.result.not_found), len(ts)),
                uncited_rate=rate(
                    sum(
                        1
                        for t in answered
                        if t.result and any(f.rule == "uncited_answer" for f in t.result.guardrails)
                    ),
                    len(answered),
                ),
                judged=len(judged),
                faithfulness=mean([j.scores.get("faithfulness") for j in judged]),
                hallucination_rate=rate(sum(scored), len(scored)),
                csat=rate(sum(1 for t in rated if t.feedback == "up"), len(rated)),
            )
        )
    return sorted(rows, key=lambda r: r.prompt)


def _peak_per_minute(turns: Sequence[Turn]) -> float | None:
    if not turns:
        return None
    minutes = Counter(t.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M") for t in turns)
    return float(max(minutes.values()))


def compute_live_metrics(
    turns: Iterable[Turn], window: str, strategy: Strategy | None, now: datetime | None = None
) -> LiveMetrics:
    now = now or datetime.now(UTC)
    since = since_for(window, now)
    selected = [
        t
        for t in turns
        if (since is None or t.created_at >= since) and (strategy is None or t.strategy == strategy)
    ]
    ok = [t for t in selected if t.result is not None]
    answered = [t for t in ok if _answered(t)]
    not_found = [t for t in ok if t.result and t.result.not_found and not t.result.blocked]
    rated = [t for t in selected if t.feedback]
    totals = [t.trace.duration_ms for t in selected]

    # Latency per step. Shares use top-level steps only: they partition each question's time.
    per_turn = [_per_name_ms(t) for t in selected]
    names: dict[tuple[str, int], list[float]] = defaultdict(list)
    for timings in per_turn:
        for key, ms in timings.items():
            names[key].append(ms)
    top_total = sum(ms for timings in per_turn for (n, d), ms in timings.items() if d == 0)
    stages = sorted(
        (
            StageLatency(
                name=name,
                depth=depth,
                latency_ms=distribution(values),
                share=sum(values) / top_total if depth == 0 and top_total else None,
            )
            for (name, depth), values in names.items()
        ),
        key=lambda s: (s.depth, -(s.latency_ms.p50 or 0)),
    )

    # Tokens per step ("generate_input" -> step "generate").
    step_tokens: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in ok:
        if t.result:
            for token_key, value in t.result.tokens.items():
                step, _, kind = token_key.rpartition("_")
                step_tokens[step][kind].append(float(value))
    tokens_by_step = [
        TokenStep(
            step=step,
            input_per_question=mean(kinds.get("input", [])),
            output_per_question=mean(kinds.get("output", [])),
            questions=max(len(kinds.get("input", [])), len(kinds.get("output", []))),
        )
        for step, kinds in sorted(step_tokens.items())
    ]

    # Inference speed of the answer step.
    tps: list[float] = []
    for t, timings in zip(selected, per_turn, strict=True):
        generate_ms = sum(ms for (n, _), ms in timings.items() if n == "generate")
        output = t.result.tokens.get("generate_output", 0) if t.result else 0
        if generate_ms > 0 and output > 0:
            tps.append(output / (generate_ms / 1000))

    dense_tops = [_dense_top(t) for t in ok]
    agent = [t for t in ok if t.strategy == Strategy.AGENT]
    llm_input = [
        float(
            sum(
                v for k, v in t.result.tokens.items() if k.endswith("_input") and k != "embed_input"
            )
        )
        for t in ok
        if t.result
    ]
    llm_output = [
        float(sum(v for k, v in t.result.tokens.items() if k.endswith("_output")))
        for t in ok
        if t.result
    ]
    p50, p95 = percentile(totals, 50), percentile(totals, 95)
    costs = [t.result.cost.usd for t in ok if t.result]
    safety, rules = _safety_metrics(selected)

    daily: dict[str, list[Turn]] = defaultdict(list)
    for t in selected:
        daily[t.created_at.astimezone(UTC).strftime("%Y-%m-%d")].append(t)

    return LiveMetrics(
        window=window,
        strategy=strategy.value if strategy else None,
        generated_at=now,
        kpis={
            "questions": float(len(selected)),
            "conversations": float(
                len({t.trace.attributes.get("conversation_id") for t in selected})
            ),
            "answer_rate": rate(len(answered), len(selected)),
            "not_found_rate": rate(len(not_found), len(selected)),
            "error_rate": rate(len(selected) - len(ok), len(selected)),
            "helpful_rate": rate(sum(1 for t in rated if t.feedback == "up"), len(rated)),
            "feedback_coverage": rate(len(rated), len(answered)),
            "latency_p50": p50,
            "latency_p95": p95,
            "latency_p99": percentile(totals, 99),
            "tail_ratio": (p95 / p50) if p50 and p95 else None,
            "cost_per_question": mean(costs),
            "cost_p95": percentile(costs, 95),
            "total_cost": sum(costs),
            "throughput_peak_qpm": _peak_per_minute(selected),
        },
        latency_total_ms=distribution(totals),
        stages=stages,
        daily=[
            DailyPoint(
                date=day,
                questions=len(day_turns),
                errors=sum(1 for t in day_turns if t.result is None),
                p95_ms=percentile([t.trace.duration_ms for t in day_turns], 95),
            )
            for day, day_turns in sorted(daily.items())
        ],
        retrieval={
            "top1_similarity": mean([top[0] for top in dense_tops if top]),
            "similarity_margin": mean([top[0] - top[1] for top in dense_tops if len(top) > 1]),
            "source_diversity": mean(
                [float(len({c.doc_id for c in t.result.chunks})) for t in ok if t.result]
            ),
            "empty_retrieval_rate": rate(
                sum(1 for t in ok if t.result and not t.result.chunks), len(ok)
            ),
            "rewrite_rate": rate(
                sum(1 for t in selected if any(s.name == "rewrite" for s in t.trace.spans)),
                len(selected),
            ),
            "agent_retry_rate": rate(
                sum(1 for t in agent if sum(s.name == "retrieve" for s in t.trace.spans) > 1),
                len(agent),
            ),
        },
        generation={
            "input_tokens": mean(llm_input),
            "output_tokens": mean(llm_output),
            "context_tokens": mean(
                [
                    float(t.result.tokens["generate_input"])
                    for t in ok
                    if t.result and "generate_input" in t.result.tokens
                ]
            ),
            "output_tps": percentile(tps, 50),
            "model_calls": mean([float(_model_calls(t)) for t in ok]),
            "cited_answer_rate": rate(
                sum(1 for t in answered if t.result and t.result.citations), len(answered)
            ),
            "citations_per_answer": mean(
                [float(len(t.result.citations)) for t in answered if t.result]
            ),
            "answer_length": mean([float(len(t.result.answer)) for t in answered if t.result]),
        },
        tokens_by_step=tokens_by_step,
        by_strategy=[
            _strategy_row(s, [t for t in selected if t.strategy == s])
            for s in Strategy
            if any(t.strategy == s for t in selected)
        ],
        models=dict(
            Counter(t.result.models.get("generation", "?") for t in ok if t.result).most_common()
        ),
        judge=_judge_metrics(answered),
        agent=_agent_metrics(selected),
        conversation=_conversation_metrics(selected),
        safety=safety,
        guardrail_rules=rules,
        by_prompt=_prompt_rows(selected),
    )
