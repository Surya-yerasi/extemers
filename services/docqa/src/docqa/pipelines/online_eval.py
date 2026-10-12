"""Online evaluation: an LLM judge scores a sample of real answers, in the background.

Judging every answer as it is given would add 10-30 s per question locally (several judge
calls each). Instead a batch job scores the most recent unjudged answers and stores the
scores on each turn, next to its trace; the Metrics tab averages them. This is how
production teams evaluate live traffic: sampled, asynchronous, and kept with the evidence.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from pydantic import BaseModel

from docqa.domain.judging import TurnJudgement
from docqa.evals.judge import Judge
from docqa.pipelines.chat import Conversation, Turn

Progress = Callable[[int, int, Turn], None]


class JudgeRunStats(BaseModel):
    eligible: int  # answered, in the window, not yet judged (or all, with rescore)
    judged: int
    failed: int  # every metric came back None (judge model unavailable, bad output)


def judgeable(turn: Turn, since: datetime | None, rescore: bool) -> bool:
    """Answers only: a refusal or a blocked question has no claims to check."""
    result = turn.result
    return (
        result is not None
        and not result.not_found
        and not result.blocked
        and (since is None or turn.created_at >= since)
        and (rescore or turn.judge is None)
    )


def judge_traffic(  # noqa: PLR0913 - each option is a sampling knob
    conversations: Iterable[Conversation],
    save: Callable[[Conversation], None],
    judge: Judge,
    *,
    since: datetime | None,
    limit: int,
    rescore: bool = False,
    progress: Progress | None = None,
) -> JudgeRunStats:
    """Score the newest `limit` eligible answers; save each conversation as it is scored."""
    candidates = [
        (conversation, turn)
        for conversation in conversations
        for turn in conversation.turns
        if judgeable(turn, since, rescore)
    ]
    candidates.sort(key=lambda pair: pair[1].created_at, reverse=True)
    chosen = candidates[:limit]
    failed = 0
    for done, (conversation, turn) in enumerate(chosen, start=1):
        result = turn.result
        if result is None:  # excluded by judgeable(); narrows the type
            continue
        scores = judge.score(
            result.standalone_question or turn.question,
            result.answer,
            [c.text for c in result.chunks],
            "",  # no reference answer in live traffic
        )
        failed += all(v is None for v in scores.values())
        turn.judge = TurnJudgement(scores=scores, model=judge.model_id, judged_at=datetime.now(UTC))
        save(conversation)
        if progress:
            progress(done, len(chosen), turn)
    return JudgeRunStats(eligible=len(candidates), judged=len(chosen), failed=failed)
