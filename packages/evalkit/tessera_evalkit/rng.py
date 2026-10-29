"""Deterministic named random streams.

Each component asks for its own stream by label, so adding a draw in one place
never shifts the numbers produced elsewhere. Uses sha256 (not Python's salted
hash()) so results are stable across processes and machines.
"""

from __future__ import annotations

import hashlib

import numpy as np


def _label_ints(labels: tuple[str | int, ...]) -> list[int]:
    digest = hashlib.sha256("/".join(str(x) for x in labels).encode()).digest()
    return [int.from_bytes(digest[i : i + 4], "big") for i in range(0, 16, 4)]


def rng_for(seed: int, *labels: str | int) -> np.random.Generator:
    if seed < 0:
        raise ValueError("seed must be non-negative")
    return np.random.default_rng(np.random.SeedSequence([seed, *_label_ints(labels)]))
