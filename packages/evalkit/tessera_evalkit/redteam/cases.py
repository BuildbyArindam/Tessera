"""The red-team case file: typed, strictly validated, one JSON object per line.

A case says three things:
  * what the attacker managed to get in front of the planner (`untrusted`: invoice notes, memos, names,
    email bodies);
  * what a model that FULLY obeyed would then propose (`fooled`), so the defences behind the model are
    exercised even when no real model is available;
  * what we expect to happen (`expect`), so a change that quietly weakens a defence is a failing check and
    not a number nobody looked at.

`expect.blind` is the strongest verdict reached when the detector is blind (policy and the planner's own
format checks alone): `rejected` (the planner's schema or id checks refused it, no proposal reached
policy), `BLOCK`, `ESCALATE`, or `APPROVE`. An APPROVE there is a documented RESIDUAL: the fooled action is
inside every limit, so only the detector's flag stands between it and execution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

DEFAULT_CASES = Path(__file__).with_name("cases_v1.jsonl")

Category = Literal[
    "direct_instruction", "role_play", "fake_system_message", "output_hijack", "split_fields", "encoded",
    "multilingual", "new_beneficiary", "card_limit_raise", "obfuscation", "social_engineering",
]  # fmt: skip
Blind = Literal["rejected", "BLOCK", "ESCALATE", "APPROVE"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UntrustedItem(Strict):
    source: Literal["invoice_note", "memo", "customer_name", "email_body"]
    ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    text: str = Field(min_length=1, max_length=20_000)


class FooledAction(Strict):
    action_type: Literal["CONVERT", "HOLD", "PAY_SUPPLIER", "TOPUP_CARD", "FLAG_INVOICE"]
    params: dict[str, JsonValue]


class Expect(Strict):
    detector_flag: bool  # the screen should raise at least one flag on the input text
    blind: Blind  # strongest outcome with the detector's result withheld from policy


class Case(Strict):
    id: str = Field(pattern=r"^RT-[0-9]{2}$")
    category: Category
    title: str = Field(min_length=5, max_length=120)
    attacker_goal: str = Field(min_length=5, max_length=200)
    untrusted: tuple[UntrustedItem, ...] = Field(min_length=1, max_length=20)
    fooled: tuple[FooledAction, ...] = Field(min_length=1, max_length=5)
    fooled_rationale: str = Field(default="Paying as requested in the supplier's note.", min_length=10, max_length=600)
    expect: Expect

    @model_validator(mode="after")
    def _a_residual_must_be_flaggable(self) -> Case:
        if self.expect.blind == "APPROVE" and not self.expect.detector_flag:
            raise ValueError("a case that policy would APPROVE must be one the detector is expected to flag")
        return self


def load_cases(path: Path = DEFAULT_CASES) -> tuple[Case, ...]:
    """Read and validate every line. A malformed line, a duplicate id or an empty file is an error."""
    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(Case.model_validate(json.loads(line)))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path.name} line {number}: {type(exc).__name__}: {str(exc)[:200]}") from None
    ids = [c.id for c in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError(f"{path.name}: no cases, or duplicate case ids")
    return tuple(cases)
