"""Agent metrics read from one trace. Pure; shared by live metrics and the eval runner.

The agent's trajectory is its sequence of top-level steps: plan → retrieve → grade →
(retrieve → grade)* → generate → (verify). The shortest successful trajectory has no retry.
"""

from pydantic import BaseModel

from docqa.domain.tracing import Trace

AGENT_STEPS = ("plan", "retrieve", "grade", "generate", "verify")
MIN_STEPS = 4  # plan, retrieve, grade, generate: what a question needs when the first search works


class AgentTraceStats(BaseModel):
    steps: int  # top-level agent steps taken
    min_steps: int  # the fewest this configuration could have taken
    search_rounds: int
    grades: int
    grades_sufficient: int  # searches the agent's own grader accepted ("tool call accuracy")
    first_search_failed: bool  # the first grade asked for another search

    @property
    def efficiency(self) -> float:
        """Fewest steps needed / steps taken: 1.0 = no wasted steps."""
        return self.min_steps / self.steps if self.steps else 0.0


def agent_trace_stats(trace: Trace) -> AgentTraceStats | None:
    """None for traces that are not agent runs (no plan step)."""
    steps = [s for s in trace.spans if s.depth == 0 and s.name in AGENT_STEPS]
    if not any(s.name == "plan" for s in steps):
        return None
    grades = [s for s in steps if s.name == "grade"]
    # Verify (when on) runs once on any answer; a regenerated answer adds generate again.
    verify_on = any(s.name == "verify" for s in steps)
    return AgentTraceStats(
        steps=len(steps),
        min_steps=MIN_STEPS + int(verify_on),
        search_rounds=sum(1 for s in steps if s.name == "retrieve"),
        grades=len(grades),
        grades_sufficient=sum(1 for g in grades if g.attributes.get("sufficient") is True),
        first_search_failed=bool(grades) and grades[0].attributes.get("sufficient") is not True,
    )
