"""python -m tessera_evalkit.agent_eval [--planner noop --planner threshold --planner llm] [--second PATH]

Writes docs/agent-eval-report.md and docs/agent-eval/disagreements.csv. `llm` needs LLM_API_KEY (see .env.example) and
the Anthropic SDK; without them the command says so and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tessera_agent import LLMClient, LLMError, llm_from_env
from tessera_core.policy import load_ruleset

from tessera_evalkit.agent_eval.decide import AgentOutcome, decide
from tessera_evalkit.agent_eval.labels import LabelSheetError, load_second_labels
from tessera_evalkit.agent_eval.report import render, write_disagreements
from tessera_evalkit.agent_eval.scenario import SCENARIO_COUNT, load_scenarios
from tessera_evalkit.agent_eval.standin import NoopLLM, ThresholdLLM


def _planner(name: str) -> LLMClient:
    if name == "noop":
        return NoopLLM()
    if name == "threshold":
        return ThresholdLLM()
    return llm_from_env()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenarios", type=Path, default=Path("data/labeled/scenarios.jsonl"))
    p.add_argument("--second", type=Path, default=Path("data/labeled/labels_second.csv"))
    p.add_argument("--planner", action="append", choices=["noop", "threshold", "llm"])
    p.add_argument("--out", type=Path, default=Path("docs/agent-eval"))
    p.add_argument("--report", type=Path, default=Path("docs/agent-eval-report.md"))
    args = p.parse_args(argv)

    rows = load_scenarios(args.scenarios)
    if len(rows) != SCENARIO_COUNT:
        print(f"expected {SCENARIO_COUNT} scenarios, found {len(rows)}", file=sys.stderr)
        return 2
    ruleset = load_ruleset()
    runs: dict[str, list[AgentOutcome]] = {}
    for name in args.planner or ["noop", "threshold"]:
        try:
            llm = _planner(name)
        except (LLMError, ImportError) as exc:
            print(f"planner '{name}' is not available here ({type(exc).__name__}); nothing written", file=sys.stderr)
            return 2
        runs[llm.model if name == "llm" else name] = [decide(s, llm, ruleset) for s in rows]
    second = None
    if args.second.exists():
        try:
            second = load_second_labels(args.second)
        except LabelSheetError as exc:
            print(f"cannot read {args.second}: {exc}", file=sys.stderr)
            return 2
        unknown = sorted(set(second) - {s.id for s in rows})
        if unknown:
            print(f"{args.second} labels unknown scenarios: {unknown}", file=sys.stderr)
            return 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(rows, runs, second, str(args.second)))
    write_disagreements(args.out / "disagreements.csv", rows, runs)
    print(f"wrote {args.report} and {args.out / 'disagreements.csv'} for planners: {', '.join(runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
