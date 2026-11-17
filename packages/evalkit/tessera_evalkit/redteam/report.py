"""Totals and the per-category tables behind docs/redteam-report.md."""

# ruff: noqa: E501  (markdown table rows and prose are not wrapped)

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tessera_evalkit.redteam import report_text as text
from tessera_evalkit.redteam.scenario import CATEGORIES, Outcome, attack_row, benign_row, by_category

CASES_V2 = Path(__file__).with_name("cases_v2.jsonl")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def summarise_all(outcomes: Sequence[Outcome], *, stack_ran: bool) -> dict[str, Any]:
    attacks = [o for o in outcomes if o.scenario.kind == "attack"]
    benigns = [o for o in outcomes if o.scenario.kind == "benign"]
    atk = by_category(outcomes, "attack")
    ben = by_category(outcomes, "benign")
    return {
        "stack_ran": stack_ran,
        "totals": {"attack": attack_row(attacks), "benign": benign_row(benigns)},
        "attack_by_category": {c: attack_row(atk[c]) for c in CATEGORIES if c in atk},
        "benign_by_category": {c: benign_row(ben[c]) for c in CATEGORIES if c in ben},
    }


def _attack_table(rows: dict[str, dict[str, Any]], total: dict[str, Any]) -> list[str]:
    head = [
        "| Category | Cases | Attack success | Block rate | Escalation rate | No effect |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    body = [
        f"| {c} | {r['cases']} | {r['attack_success']} | {_pct(r['block_rate'])} | {_pct(r['escalation_rate'])} | {r['no_effect']} |"
        for c, r in rows.items()
    ]
    r = total
    foot = f"| **All** | **{r['cases']}** | **{r['attack_success']}** | **{_pct(r['block_rate'])}** | **{_pct(r['escalation_rate'])}** | **{r['no_effect']}** |"
    return [*head, *body, foot]


def _benign_table(rows: dict[str, dict[str, Any]], total: dict[str, Any]) -> list[str]:
    head = ["| Category | Cases | Should run alone | False escalations | False blocks |", "|---|---:|---:|---:|---:|"]
    body = [
        f"| {c} | {r['cases']} | {r['should_run_on_their_own']} | {len(r['false_escalations'])} | {len(r['false_blocks'])} |"
        for c, r in rows.items()
    ]
    r = total
    foot = (
        f"| **All** | **{r['cases']}** | **{r['should_run_on_their_own']}** | "
        f"**{len(r['false_escalations'])} ({_pct(r['false_escalation_rate'])})** | "
        f"**{len(r['false_blocks'])} ({_pct(r['false_block_rate'])})** |"
    )
    return [*head, *body, foot]


def _stopped_by(outcomes: Sequence[Outcome]) -> list[str]:
    """Which layer ended each attack, grouped coarsely (policy rules, planner, edge, dispatch, ...)."""
    count: Counter[str] = Counter()
    for o in outcomes:
        if o.scenario.kind == "attack" and o.attempt is not None and not o.attempt.harmful:
            by = o.attempt.stopped_by
            layer = by.split(":")[0].split(" ")[0]
            count["policy" if layer.startswith("policy") else layer] += 1
    lines = ["| Stopped by | Attacks |", "|---|---:|"]
    return lines + [f"| {k} | {v} |" for k, v in count.most_common()]


def _policy_rules(outcomes: Sequence[Outcome]) -> list[str]:
    count: Counter[str] = Counter()
    for o in outcomes:
        a = o.attempt
        if o.scenario.kind == "attack" and a is not None and a.stopped_by.startswith("policy"):
            for rule in a.stopped_by.partition(":")[2].split("+"):
                count[rule.split(" ")[0]] += 1
    return [
        "| Policy rule | Attacks it took part in stopping |",
        "|---|---:|",
        *(f"| `{k}` | {v} |" for k, v in count.most_common()),
    ]


def _detector_misses() -> list[str]:
    if not CASES_V2.exists():
        return []
    rows = [json.loads(line) for line in CASES_V2.read_text(encoding="utf-8").splitlines() if line.strip()]
    misses = [r for r in rows if "KNOWN MISS" in r["title"]]
    flagged = sum(1 for r in rows if r["expect"].get("detector_flag"))
    out = [
        f"The detector flags {flagged} of the {len(rows)} new planner-layer cases. Known misses, each stopped later:",
        "",
    ]
    return out + [f"* {r['id']}: {r['title'].replace('KNOWN MISS (detector): ', '')}" for r in misses]


def _unexpected(summary: dict[str, Any]) -> list[str]:
    bad = summary["totals"]["attack"]["unexpected"] + summary["totals"]["benign"]["unexpected"]
    return (
        [f"**Unexpected results:** {', '.join(bad)}"]
        if bad
        else ["Every scenario ended the way its documentation says."]
    )


def _benign_notes(outcomes: Sequence[Outcome]) -> list[str]:
    lines = [
        f"* {o.scenario.id}: {o.scenario.title}"
        for o in outcomes
        if o.scenario.kind == "benign"
        and o.attempt is not None
        and o.attempt.terminal == "ESCALATED"
        and o.scenario.legit == "execute"
    ]
    return ["Benign cases that went to a person although they were meant to run alone:", "", *lines] if lines else []


def write_report(path: Path, outcomes: Sequence[Outcome], summary: dict[str, Any], *, stack_ran: bool) -> None:
    t = summary["totals"]
    a, b = t["attack"], t["benign"]
    parts = [
        "# Red-team report (Day 18)", "",
        f"{a['cases']} attacks and {b['cases']} benign cases. **Attacks that executed a harmful action: {a['attack_success']}.** "
        f"Block rate {_pct(a['block_rate'])}, escalation rate {_pct(a['escalation_rate'])}. "
        f"Benign false-escalation rate {_pct(b['false_escalation_rate'])}, false-block rate {_pct(b['false_block_rate'])}.", "",
        "" if stack_ran else "> The stack layer did not run (no `--stack`); only planner and policy scenarios are counted.", "",
        "Regenerate with `python -m tessera_evalkit.redteam.suite --stack tessera_worker.redteam_stack:open_stack --report docs/redteam-report.md`.", "",
        "## How it runs", "", text.HOW_IT_RUNS, "## Metric definitions", "", text.DEFINITIONS, "",
        "## Attacks, by category", "", *_attack_table(summary["attack_by_category"], a), "",
        "## Benign cases (the usability cost)", "", *_benign_table(summary["benign_by_category"], b), "",
        *_benign_notes(outcomes), "",
        "## Which layer stopped each attack", "", *_stopped_by(outcomes), "", *_policy_rules(outcomes), "",
        "A rule can stop an attack alongside others, so the second table can add up to more than the policy row above.", "",
        "## Findings", "", text.FINDING, "## Detector coverage", "", *_detector_misses(), "",
        "## Open weaknesses and limits", "", text.OPEN, "## Result check", "", *_unexpected(summary), "",
    ]  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
