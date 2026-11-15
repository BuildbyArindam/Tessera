"""Day 16: what the Proposals and Policies screens read. The decision's evidence (simulation, forecast width) and the
read-only rules in force. Planner and policy are real; the model is scripted."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from it_helpers import APPROVE_ITEM, BLOCK_ITEM, ESCALATE_ITEM, FixtureWorldProvider, add_business, scripted_llm
from sqlalchemy.orm import Session
from tessera_core.policy import load_ruleset
from tessera_store import repo

Client = Callable[..., TestClient]


@pytest.fixture
def planned(make_client: Client, session: Session) -> tuple[TestClient, dict[str, dict[str, Any]]]:
    """A client, and the proposals of one plan run keyed by status (one per verdict)."""
    add_business(session)
    client = make_client(
        world_provider=FixtureWorldProvider(), llm=lambda: scripted_llm(APPROVE_ITEM, ESCALATE_ITEM, BLOCK_ITEM)
    )
    assert client.post("/plan/run", json={"business_id": "B0"}).status_code == 200
    return client, {p["status"]: p for p in client.get("/proposals").json()}


# ---- GET /policies ----------------------------------------------------------------------------------
def test_policies_need_the_key(make_client: Client) -> None:
    assert make_client(authenticated=False).get("/policies").status_code == 401


def test_policies_list_the_rules_in_force_with_the_hash_decisions_carry(
    planned: tuple[TestClient, dict[str, dict[str, Any]]],
) -> None:
    client, proposals = planned
    body = client.get("/policies").json()
    expected = load_ruleset()
    assert body["ruleset_hash"] == expected.hash
    assert [r["id"] for r in body["rules"]] == [r.rule_id for r in expected.rules]  # same order the engine runs them
    assert {"kill_switch", "worst_case_loss_gate", "forecast_confidence_gate"} <= {r["id"] for r in body["rules"]}
    # The screen's hash is the hash on every decision, so a person can tie a verdict to the rules shown.
    assert {p["decision"]["ruleset_hash"] for p in proposals.values()} == {body["ruleset_hash"]}


def test_policy_params_are_exactly_what_the_hash_covers_and_money_is_never_a_float(
    client_with_rules: TestClient,
) -> None:
    rules = {r["id"]: r for r in client_with_rules.get("/policies").json()["rules"]}
    assert rules["forecast_confidence_gate"]["params"] == {"max_relative_width": "0.5000"}
    # Money inside params keeps its decimal-string amount and names its currency, so the screen never guesses one.
    assert rules["worst_case_loss_gate"]["params"]["max_worst_case_loss"]["USD"] == {
        "amount": "500.00",
        "currency": "USD",
    }
    assert rules["kill_switch"]["params"] == {}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_policies_have_no_write_path(client_with_rules: TestClient, method: str) -> None:
    assert getattr(client_with_rules, method)("/policies").status_code == 405


@pytest.fixture
def client_with_rules(make_client: Client) -> TestClient:
    return make_client()


# ---- the evidence on a decision ---------------------------------------------------------------------
def test_a_decision_carries_the_simulation_and_forecast_width_it_was_made_on(
    planned: tuple[TestClient, dict[str, dict[str, Any]]],
) -> None:
    _, proposals = planned
    decision = proposals["APPROVED"]["decision"]  # a CONVERT of 1000.00 EUR in the fixture world
    sim = decision["simulation"]
    assert sim["amount"] == {"amount": "1000.00", "currency": "EUR"}
    assert sim["n_paths"] == 5000
    assert sim["worst_case_loss"]["currency"] == "EUR" and float(sim["worst_case_loss"]["amount"]) >= 0
    assert set(decision["forecast_relative_width"]) == {"EUR", "INR", "USD"}
    assert all(isinstance(w, str) for w in decision["forecast_relative_width"].values())  # decimals, not floats


def test_a_payout_simulation_is_labelled_in_its_own_currencies(
    planned: tuple[TestClient, dict[str, dict[str, Any]]],
) -> None:
    """The amount is in payout units (INR); the saving and loss are in the currency that funds it (USD)."""
    _, proposals = planned
    sim = proposals["PENDING_APPROVAL"]["decision"]["simulation"]
    assert sim["amount"]["currency"] == "INR"
    assert sim["expected_saving"]["currency"] == sim["worst_case_loss"]["currency"] == "USD"


@pytest.mark.parametrize("context", [None, {"balances": "not a mapping"}, {"simulation": {"proposal_id": ""}}])
def test_a_missing_or_unreadable_context_shows_the_verdict_and_no_evidence(
    planned: tuple[TestClient, dict[str, dict[str, Any]]], session: Session, context: dict[str, Any] | None
) -> None:
    client, proposals = planned
    blocked = proposals["BLOCKED"]
    repo.add_decision(
        session,
        proposal_id=blocked["id"],
        phase="execution",
        verdict="BLOCK",
        reasons=("policy_error",),
        rule_ids=("engine",),
        ruleset_hash=blocked["decision"]["ruleset_hash"],
        context=context,
    )
    session.commit()
    shown = next(p for p in client.get("/proposals").json() if p["id"] == blocked["id"])["decision"]
    assert shown["verdict"] == "BLOCK" and shown["reasons"] == ["policy_error"]
    assert shown["simulation"] is None and shown["forecast_relative_width"] is None
