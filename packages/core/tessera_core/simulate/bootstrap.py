"""Moving-block bootstrap of historical daily log returns, vectorised.

Why blocks: resampling single days destroys the clustering of calm and stormy stretches, which
makes multi-week ranges too narrow. Resampling short consecutive runs keeps some of it. The default
block of 5 business days is one trading week.

Each path is built from ceil(steps / block_length) blocks, each block a run of `block_length`
consecutive returns starting at a uniformly random position, then cut to exactly `steps` returns.
The whole draw is one integer array of shape (n_paths, n_blocks), so there is no Python loop over
paths or days.

`demean` removes the sample mean first. A long history carries whatever trend the pair happened to
have (a currency that fell 3% a year), and bootstrapping that trend forward would make waiting look
better or worse than converting now purely because of the past. Zero drift is the neutral default;
callers who want to keep the historical drift can switch it off.
"""

import numpy as np

from tessera_core.simulate.series import FloatArray, SimulationInputError


def block_bootstrap_log_returns(
    returns: FloatArray,
    steps: int,
    n_paths: int,
    block_length: int,
    rng: np.random.Generator,
    demean: bool = True,
) -> FloatArray:
    """Cumulative log return over `steps` business days for each of `n_paths` simulated paths."""
    if steps < 0 or n_paths < 1:
        raise SimulationInputError("steps must be >= 0 and n_paths >= 1")
    if steps == 0:
        return np.zeros(n_paths)
    n = len(returns)
    if not 1 <= block_length <= n:
        raise SimulationInputError(f"block_length must be between 1 and the {n} available returns")

    source = returns - returns.mean() if demean else returns
    n_blocks = -(-steps // block_length)  # ceil
    starts = rng.integers(0, n - block_length + 1, size=(n_paths, n_blocks))
    index = (starts[:, :, None] + np.arange(block_length)).reshape(n_paths, n_blocks * block_length)[:, :steps]
    total: FloatArray = source[index].sum(axis=1)
    return total
