"""Read the second labeller's answers from the CSV sheet (`data/labeled/second_labeler_sheet.csv`, filled in).

The sheet has one row per scenario with blank answer columns. A row whose `action_type` is blank is "not labelled" and is
left out (the rubric asks for at least 15 of the 40). A filled row is validated with the same `Choice` rules as the main
labels, so a CONVERT without an amount is an error that names the scenario, not a silently dropped row.
"""

from __future__ import annotations

import csv
from pathlib import Path

from tessera_evalkit.agent_eval.scenario import Choice

FIELDS = ("amount", "currency", "to_currency", "obligation_id", "beneficiary_id", "invoice_id")


class LabelSheetError(ValueError):
    """The filled sheet cannot be read as labels. The message names the scenario and what is wrong."""


def load_second_labels(path: Path) -> dict[str, Choice]:
    out: dict[str, Choice] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            sid = (row.get("id") or "").strip()
            kind = (row.get("action_type") or "").strip().upper()
            if not sid or not kind:
                continue
            if sid in out:
                raise LabelSheetError(f"{sid}: labelled twice")
            values = {k: (row.get(k) or "").strip() or None for k in FIELDS}
            try:
                out[sid] = Choice(action_type=kind, **values)  # type: ignore[arg-type]
            except ValueError as exc:
                raise LabelSheetError(f"{sid}: {str(exc).splitlines()[0]}") from None
    return out
