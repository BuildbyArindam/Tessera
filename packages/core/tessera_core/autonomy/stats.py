"""A seeded bootstrap lower confidence bound on a mean, in exact Decimal arithmetic.

Realised saving is a money-derived figure, so it stays Decimal end to end. The resampling uses `random.Random(seed)`:
the same samples and seed always give the same bound, so a promotion decision can be re-derived from the audit log.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import Decimal


def mean(samples: Sequence[Decimal]) -> Decimal:
    if not samples:
        raise ValueError("mean of no samples")
    return sum(samples, Decimal(0)) / len(samples)


def bootstrap_lower_bound(samples: Sequence[Decimal], *, resamples: int, confidence: Decimal, seed: int) -> Decimal:
    """The one-sided lower bound: the (1 - confidence) quantile of the means of `resamples` resamples.

    Needs at least two samples; with fewer there is no spread to resample.
    """
    n = len(samples)
    if n < 2:
        raise ValueError("a bootstrap bound needs at least two samples")
    if resamples < 1 or not (0 < confidence < 1):
        raise ValueError("resamples must be positive and confidence must be between 0 and 1")
    rng = random.Random(seed)  # noqa: S311 - statistical resampling, not security
    means = sorted(mean(rng.choices(samples, k=n)) for _ in range(resamples))
    index = int((1 - confidence) * resamples)  # floor: the quantile position, never past the end
    return means[min(index, resamples - 1)]
