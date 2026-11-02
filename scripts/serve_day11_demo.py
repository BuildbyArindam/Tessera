"""DEMO ONLY: run the API with a synthetic planning world and a SCRIPTED model, so every route can be tried.

Not for production. The real model and the real data provider arrive with the worker on Day 13. Here the "model"
returns three fixed proposals (one the policy engine approves, one it escalates, one it blocks) and the world is the
red-team fixture. Needs DATABASE_URL and TESSERA_API_KEY; the business B0 must exist (verify_day11.py seeds it).

    python scripts/serve_day11_demo.py --port 8011
"""

from __future__ import annotations

import argparse
import json

import uvicorn
from sqlalchemy.orm import Session
from tessera_agent import FakeLLM, tool_call_reply
from tessera_api.deps import Settings
from tessera_api.main import create_app
from tessera_api.planning import PlanningUnavailable, PlanningWorld
from tessera_evalkit.redteam.world import BUSINESS, build_world

PAY = {"amount": "500.00", "currency": "INR", "from_currency": "USD", "obligation_id": "OBL-0001"}
ITEMS = [
    {"action_type": "CONVERT", "params": {"amount": "1000.00", "currency": "EUR", "to_currency": "INR",
                                          "obligation_id": "OBL-0002"}},  # policy: APPROVE
    {"action_type": "PAY_SUPPLIER", "params": {**PAY, "beneficiary_id": "BEN-001"}},  # policy: ESCALATE (paid early)
    {"action_type": "PAY_SUPPLIER", "params": {**PAY, "beneficiary_id": "BEN-002"}},  # policy: BLOCK (not approved)
]  # fmt: skip


class DemoWorld:
    def __init__(self) -> None:
        self._world = build_world()

    def load(self, session: Session, business_id: str) -> PlanningWorld:
        if business_id != BUSINESS:
            raise PlanningUnavailable("the demo world only has business B0")
        return PlanningWorld(snapshot=self._world.snapshot(()), policy_context=self._world.policy_context)


def scripted_model() -> FakeLLM:
    body = [{**i, "rationale": "Demo script.", "confidence": 0.7, "evidence_ids": ["ev-1"]} for i in ITEMS]
    return FakeLLM([tool_call_reply("list_upcoming_obligations", {"days_ahead": 30}), json.dumps({"proposals": body})])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()
    app = create_app(Settings.from_env(), world_provider=DemoWorld(), llm_factory=scripted_model)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
