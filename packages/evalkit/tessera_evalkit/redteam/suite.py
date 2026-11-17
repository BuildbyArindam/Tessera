"""Run every red-team scenario, write the results and docs/redteam-report.md.

  python -m tessera_evalkit.redteam.suite --stack tessera_worker.redteam_stack:open_stack

Needs a PostgreSQL for the stack scenarios (TEST_DATABASE_ADMIN_URL, see tessera_worker.redteam_stack). Without
`--stack` only the planner and policy scenarios run, and the report says so. Exit status 1 if any attack succeeded,
any scenario ended differently from its documented outcome, or any scenario errored.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import traceback
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tessera_core.policy import Ruleset, load_ruleset

from tessera_evalkit.redteam import scn_authority, scn_benign, scn_money, scn_ops, scn_planner  # noqa: F401  (registers the scenarios)
from tessera_evalkit.redteam.scenario import Outcome, Scenario
from tessera_evalkit.redteam.scn_support import REGISTRY
from tessera_evalkit.redteam.world import World, build_world


@dataclass
class Runtime:
    world: World
    ruleset: Ruleset
    open_stack: Callable[[], AbstractContextManager[Any]] | None

    def stack(self) -> AbstractContextManager[Any]:
        if self.open_stack is None:
            raise RuntimeError("no stack configured (pass --stack)")
        return self.open_stack()


def all_scenarios() -> list[Scenario]:
    return [*scn_planner.planner_scenarios(), *scn_planner.amount_scenarios(), *REGISTRY]


def run_scenarios(rt: Runtime, scenarios: Sequence[Scenario]) -> list[Outcome]:
    done: list[Outcome] = []
    for s in scenarios:
        if s.layer == "stack" and rt.open_stack is None:
            continue
        try:
            done.append(Outcome(s, s.run(rt)))
        except Exception as exc:  # a broken scenario is an error to look at, never a pass
            traceback.print_exc()
            done.append(Outcome(s, None, error=f"{type(exc).__name__}: {str(exc)[:300]}"))
    return done


def _load(spec: str) -> Callable[[], AbstractContextManager[Any]]:
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tessera_evalkit.redteam.suite", description=__doc__.split("\n")[0])
    ap.add_argument("--stack", help="module:callable that opens a stack (default: planner and policy scenarios only)")
    ap.add_argument("--out", type=Path, default=Path("artifacts/redteam"))
    ap.add_argument("--report", type=Path, default=None, help="write the markdown report here")
    ap.add_argument("--only", help="run scenarios whose id starts with this prefix")
    options = ap.parse_args(argv)

    logging.getLogger("tessera.agent").propagate = False  # flag codes go to the log file only
    options.out.mkdir(parents=True, exist_ok=True)
    logging.getLogger("tessera.agent").addHandler(
        logging.FileHandler(options.out / "flags.log", mode="w", encoding="utf-8")
    )
    rt = Runtime(build_world(), load_ruleset(), _load(options.stack) if options.stack else None)
    chosen = [s for s in all_scenarios() if not options.only or s.id.startswith(options.only)]
    outcomes = run_scenarios(rt, chosen)

    from tessera_evalkit.redteam.report import summarise_all, write_report

    summary = summarise_all(outcomes, stack_ran=rt.open_stack is not None)
    (options.out / "redteam_results.jsonl").write_text(
        "".join(
            json.dumps({"id": o.scenario.id, "category": o.scenario.category, "kind": o.scenario.kind,
                        "title": o.scenario.title, "layer": o.scenario.layer, "expected": o.scenario.expect,
                        "attempt": None if o.attempt is None else o.attempt.model_dump(), "error": o.error}) + "\n"
            for o in outcomes
        ),
        encoding="utf-8",
    )  # fmt: skip
    (options.out / "redteam_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    if options.report:
        write_report(options.report, outcomes, summary, stack_ran=rt.open_stack is not None)
    print(json.dumps(summary["totals"], indent=2))
    for o in outcomes:
        if not o.as_expected:
            got = (
                o.error
                or f"{o.attempt.terminal if o.attempt else '?'} harmful={o.attempt.harmful if o.attempt else '?'}"
            )
            print(
                f"UNEXPECTED {o.scenario.id} [{o.scenario.kind}] {o.scenario.title}: "
                f"expected {o.scenario.expect}, got {got}"
            )
    return 0 if all(o.as_expected for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
