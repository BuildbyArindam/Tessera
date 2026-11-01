"""Red-team suite: injection cases run through sanitiser, planner and the real policy engine (Day 10).

Needs the `redteam` extra (tessera-core, tessera-agent, pandas). Never imports tessera_airwallex: an
executed action is an injected callable, called only on APPROVE.
"""

from tessera_evalkit.redteam.cases import Case, load_cases
from tessera_evalkit.redteam.runner import CaseResult, CompromisedLLM, RecordingExecutor, run_case, run_suite, summarise
from tessera_evalkit.redteam.world import World, build_world

__all__ = [
    "Case",
    "CaseResult",
    "CompromisedLLM",
    "RecordingExecutor",
    "World",
    "build_world",
    "load_cases",
    "run_case",
    "run_suite",
    "summarise",
]
