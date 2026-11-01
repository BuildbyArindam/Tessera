"""`python -m tessera_evalkit.redteam`: run the red-team suite and write results.

  --cases PATH   case file (default: the shipped cases_v1.jsonl)
  --out DIR      where to write results_<mode>.jsonl and summary.json (default: artifacts/redteam)
  --live         ALSO run every case against a real model (needs LLM_API_KEY); recorded, never asserted

Exit status is 1 if anything was executed in `screened` mode, or any case's outcome differs from what the
case file expects, so `make redteam` fails loudly instead of printing a number nobody checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from tessera_evalkit.redteam.cases import DEFAULT_CASES, load_cases
from tessera_evalkit.redteam.runner import CaseResult, Mode, RecordingExecutor, run_suite, summarise
from tessera_evalkit.redteam.world import build_world


def _row(result: CaseResult) -> str:
    flags = ",".join(result.detector_flags) or "-"
    ok = {True: "ok", False: "MISMATCH", None: "-"}[result.expectation_met]
    head = f"{result.case_id}  {result.category:<19} {result.outcome:<9} {result.stopped_by:<52}"
    return f"{head} flags={flags[:40]:<40} {ok}"


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(prog="tessera_evalkit.redteam", description=__doc__.split("\n")[0])
    args.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args.add_argument("--out", type=Path, default=Path("artifacts/redteam"))
    args.add_argument("--live", action="store_true")
    options = args.parse_args(argv)

    options.out.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(options.out / "flags.log", mode="w", encoding="utf-8")  # codes only, never text
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logging.getLogger("tessera.agent").addHandler(handler)
    logging.getLogger("tessera.agent").propagate = False
    cases, world, executor = load_cases(options.cases), build_world(), RecordingExecutor()
    suites = run_suite(cases, world, executor=executor)
    if options.live:
        from tessera_agent import llm_from_env

        suites["live"] = run_suite(cases, world, modes=("live",), llm=llm_from_env(), executor=RecordingExecutor())[
            "live"
        ]

    summary: dict[str, object] = {}
    for mode, results in suites.items():
        (options.out / f"results_{mode}.jsonl").write_text(
            "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8"
        )
        summary[mode] = summarise(results)
        print(f"\n== {mode} ({len(results)} cases) ==")
        print("\n".join(_row(r) for r in results))
    (options.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps(summary, indent=2, sort_keys=True))

    screened: list[CaseResult] = suites["screened"]
    modes: list[Mode] = ["screened", "blind"]
    bad = any(r.executed for r in screened) or any(r.expectation_met is False for m in modes for r in suites[m])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
