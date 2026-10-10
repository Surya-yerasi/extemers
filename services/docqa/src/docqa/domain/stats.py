"""Small, dependency-free statistics shared by online metrics and the eval harness. Pure."""

import math
from collections.abc import Sequence


def mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (no interpolation): P95 of 20 samples is the 19th smallest.

    Nearest-rank always returns a value that actually occurred, which keeps small samples
    honest: P99 of 30 requests is simply the slowest one."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
