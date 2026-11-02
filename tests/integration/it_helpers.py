"""Shared builders for the Day 11 integration tests: a fixture planning world and scripted model replies."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from tessera_agent import FakeLLM, UntrustedText, tool_call_reply
from tessera_api.planning import PlanningUnavailable, PlanningWorld
from tessera_evalkit.redteam.world import BUSINESS, World, build_world
from tessera_store import models as m

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-key-0123456789abcdef"
HEADERS = {"X-API-Key": API_KEY}
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # the fixture world's NOW


# One proposal per verdict in the fixture world (checked against the real engine, not assumed).
def _pay(beneficiary_id: str) -> dict[str, Any]:
    return {
        "action_type": "PAY_SUPPLIER",
        "params": {"amount": "500.00", "currency": "INR", "from_currency": "USD",
                   "beneficiary_id": beneficiary_id, "obligation_id": "OBL-0001"},
    }  # fmt: skip


APPROVE_ITEM: dict[str, Any] = {
    "action_type": "CONVERT",
    "params": {"amount": "1000.00", "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-0002"},
}
ESCALATE_ITEM = _pay("BEN-001")  # approved payee, but paid 10 days early: early_payment_gate escalates
BLOCK_ITEM = _pay("BEN-002")  # a known payee that is not approved: beneficiary_allowlist blocks


class FixtureWorldProvider:
    """The red-team world as a WorldProvider. Only business B0 exists in it."""

    def __init__(self, untrusted: tuple[UntrustedText, ...] = ()) -> None:
        self.world: World = build_world()
        self._untrusted = untrusted

    def load(self, session: Session, business_id: str) -> PlanningWorld:
        if business_id != BUSINESS:
            raise PlanningUnavailable("no planning data for this business")
        return PlanningWorld(snapshot=self.world.snapshot(self._untrusted), policy_context=self.world.policy_context)


def scripted_llm(*items: dict[str, Any]) -> FakeLLM:
    """A model that grounds itself with one tool call, then proposes `items` (each citing that tool's evidence)."""
    body = [{**i, "rationale": "Scripted for the test.", "confidence": 0.7, "evidence_ids": ["ev-1"]} for i in items]
    return FakeLLM([tool_call_reply("list_upcoming_obligations", {"days_ahead": 30}), json.dumps({"proposals": body})])


def add_business(session: Session, business_id: str = BUSINESS, name: str = "Test Studio") -> None:
    session.add(m.Business(id=business_id, name=name, base_currency="INR"))
    session.commit()
