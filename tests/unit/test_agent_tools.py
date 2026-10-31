"""Day 9: the read-only tools. Pure functions of a frozen snapshot; bad calls return errors, never raise."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest
from agent_helpers import AS_OF, BUSINESS, INJECTION, first_open_invoice, fx_series, make_snapshot
from tessera_agent import PlannerSnapshot, ReadOnlyTools, build_context
from tessera_agent.canon import canonical_json

pytestmark = pytest.mark.usefixtures("no_network")

SIM: dict[str, Any] = {
    "action_type": "CONVERT", "amount": "3000.00", "currency": "USD", "to_currency": "INR", "obligation_id": "OBL-0001",
}  # fmt: skip


@pytest.fixture(scope="module")
def snapshot() -> PlannerSnapshot:
    return make_snapshot()


@pytest.fixture(scope="module")
def tools(snapshot: PlannerSnapshot) -> ReadOnlyTools:
    return ReadOnlyTools(snapshot)


def digest(s: PlannerSnapshot) -> str:
    """A fingerprint of everything in the snapshot, to prove a tool run changed none of it."""
    h = hashlib.sha256()
    h.update(repr((s.forecasts, s.obligations, s.beneficiaries, s.balances, s.as_of, s.card_vendor_ids)).encode())
    h.update(pd.util.hash_pandas_object(s.invoices, index=True).to_numpy().tobytes())
    for pair in sorted(s.fx_history):
        h.update(s.fx_history[pair].days.tobytes() + s.fx_history[pair].values.tobytes())
    return h.hexdigest()


def is_error(result: dict[str, Any]) -> bool:
    return "error" in result


# ---- every tool answers, deterministically, in JSON --------------------------------------------
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("get_forecast", {}),
    ("get_forecast", {"currency": "USD", "horizon_weeks": 4}),
    ("get_fx_range", {"pair": "USD/INR", "horizon_days": 30}),
    ("get_late_risk", {}),
    ("get_late_risk", {"invoice_ids": ["<OPEN>"]}),
    ("simulate_proposal", SIM),
    ("list_upcoming_obligations", {"days_ahead": 30}),
]


@pytest.mark.parametrize(("name", "args"), CALLS)
def test_each_tool_returns_json_and_the_same_answer_twice(
    tools: ReadOnlyTools, name: str, args: dict[str, Any]
) -> None:
    if args.get("invoice_ids") == ["<OPEN>"]:
        args = {"invoice_ids": [first_open_invoice()]}
    first, second = tools.call(name, args), tools.call(name, args)
    assert not is_error(first), first
    assert canonical_json(first) == canonical_json(second)  # also proves it is serialisable, with no NaN
    json.loads(canonical_json(first))


def test_tools_leave_the_snapshot_byte_identical(snapshot: PlannerSnapshot) -> None:
    before = digest(snapshot)
    fresh = ReadOnlyTools(snapshot)
    for name, args in CALLS:
        fresh.call(name, {"invoice_ids": [first_open_invoice()]} if args.get("invoice_ids") else args)
    fresh.call("get_forecast", {"currency": "BTC"})  # error paths too
    assert digest(snapshot) == before


def test_the_five_tools_are_exactly_the_specified_ones(tools: ReadOnlyTools) -> None:
    names = {s.name for s in tools.specs()}
    assert names == {"get_forecast", "get_fx_range", "get_late_risk", "simulate_proposal", "list_upcoming_obligations"}
    for spec in tools.specs():
        assert spec.input_schema["type"] == "object" and spec.input_schema.get("additionalProperties") is False


# ---- bad calls are answers, not crashes ---------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "args", "kind"),
    [
        ("transfer_money", {}, "unknown_tool"),
        ("get_forecast", {"currency": "BTC"}, "invalid_arguments"),
        ("get_forecast", {"horizon_weeks": 99}, "invalid_arguments"),
        ("get_forecast", {"business_id": "B1"}, "invalid_arguments"),  # cannot be pointed at another business
        ("get_fx_range", {"pair": "usd/inr", "horizon_days": 5}, "invalid_arguments"),
        ("get_fx_range", {"pair": "USD/INR", "horizon_days": 0}, "invalid_arguments"),
        ("get_fx_range", {"pair": "USD/INR", "horizon_days": True}, "invalid_arguments"),
        ("get_fx_range", {"pair": "USD/NOK", "horizon_days": 5}, "ToolError"),
        ("get_late_risk", {"invoice_ids": ["x" * 80]}, "invalid_arguments"),
        ("get_late_risk", {"invoice_ids": ["NOPE-1"]}, "InvoiceSelectionError"),
        ("simulate_proposal", {**SIM, "due_date": "2026-03-02"}, "invalid_arguments"),  # the model cannot set a date
        ("simulate_proposal", {**SIM, "obligation_id": "OBL-9999"}, "ToolError"),
        ("simulate_proposal", {**SIM, "to_currency": None}, "invalid_arguments"),
        ("simulate_proposal", {**SIM, "amount": "1e3"}, "invalid_arguments"),
        ("list_upcoming_obligations", {"days_ahead": 1000}, "invalid_arguments"),
    ],
)
def test_bad_calls_return_a_readable_error(tools: ReadOnlyTools, name: str, args: dict[str, Any], kind: str) -> None:
    result = tools.call(name, args)
    assert result["error"]["type"] == kind, result  # type: ignore[index]


def test_error_messages_do_not_echo_what_the_model_sent(tools: ReadOnlyTools) -> None:
    result = tools.call("get_forecast", {"currency": "SECRET-MARKER-123"})
    assert "SECRET-MARKER-123" not in json.dumps(result)


# ---- the model cannot steer what matters --------------------------------------------------------
def test_the_due_date_comes_from_the_obligation_not_the_model(tools: ReadOnlyTools) -> None:
    near = tools.call("simulate_proposal", {**SIM, "obligation_id": "OBL-0003"})  # due 7 days out
    far = tools.call("simulate_proposal", {**SIM, "obligation_id": "OBL-0002"})  # due 18 days out
    assert near["due_date"] == "2026-03-09" and far["due_date"] == "2026-03-20"  # type: ignore[index]
    assert near["horizon_days"] == 7 and far["horizon_days"] == 18  # type: ignore[index]


def test_overdue_obligations_simulate_with_a_zero_horizon(snapshot: PlannerSnapshot) -> None:
    overdue = snapshot.obligations[0].model_copy(update={"id": "OBL-LATE", "due_on": date(2026, 2, 1)})
    snap = make_snapshot(obligations=(overdue,))
    out = ReadOnlyTools(snap).call("simulate_proposal", {**SIM, "obligation_id": "OBL-LATE"})
    assert out["due_date"] == AS_OF.isoformat() and out["horizon_days"] == 0


def test_simulation_reports_its_cost_assumptions_and_a_flat_worst_case_sign(tools: ReadOnlyTools) -> None:
    out = tools.call("simulate_proposal", SIM)
    assert out["assumptions"] == {"spread_bps": "50", "fee": "0.00"}  # type: ignore[comparison-overlap]
    assert float(out["worst_case_loss"]["amount"]) >= 0  # type: ignore[index]
    assert out["n_paths"] == 5000 and out["seed"] == 0


def test_tools_only_see_their_own_business(snapshot: PlannerSnapshot, tools: ReadOnlyTools) -> None:
    other = snapshot.invoices[snapshot.invoices["business_id"] != BUSINESS]["invoice_id"].iloc[0]
    assert other not in snapshot.known_ids()["invoice"]
    assert tools.call("get_late_risk", {"invoice_ids": [other]})["error"]["type"] == "InvoiceSelectionError"  # type: ignore[index]
    shown = {a["invoice_id"] for a in tools.call("get_late_risk", {"limit": 20})["invoices"]}  # type: ignore[union-attr]
    own = snapshot.invoices[snapshot.invoices["business_id"] == BUSINESS]["invoice_id"]
    assert shown <= set(own)


def test_a_snapshot_with_another_business_forecast_is_refused() -> None:
    snap = make_snapshot()
    stray = snap.forecasts[0].model_copy(update={"business_id": "B1"})
    with pytest.raises(ValueError, match="other businesses"):
        make_snapshot(forecasts=(stray,))


def test_no_observation_after_as_of_reaches_the_fx_range_or_the_simulation(snapshot: PlannerSnapshot) -> None:
    """Scaling every rate after AS_OF by 25x must change nothing the tools return."""
    future = {}
    for pair, series in snapshot.fx_history.items():
        later = series.days > np.datetime64(AS_OF, "D")
        assert later.any(), "the fixture must contain a future to be meaningful"
        future[pair] = type(series)(pair, series.days.copy(), np.where(later, series.values * 25.0, series.values))
    a, b = ReadOnlyTools(snapshot), ReadOnlyTools(make_snapshot(fx_history=future))
    for name, args in [("get_fx_range", {"pair": "USD/INR", "horizon_days": 30}), ("simulate_proposal", SIM)]:
        assert canonical_json(a.call(name, args)) == canonical_json(b.call(name, args))


def test_the_fx_range_changes_when_the_past_changes(snapshot: PlannerSnapshot) -> None:
    """The check above can fail: different history before AS_OF does change the answer."""
    other = make_snapshot(fx_history={**snapshot.fx_history, "USD/INR": fx_series("USD/INR", 83.0, seed=99, vol=0.02)})
    args = {"pair": "USD/INR", "horizon_days": 30}
    assert canonical_json(ReadOnlyTools(snapshot).call("get_fx_range", args)) != canonical_json(
        ReadOnlyTools(other).call("get_fx_range", args)
    )


# ---- no free text reaches the model --------------------------------------------------------------
def test_names_and_other_free_text_never_appear_in_the_context_or_any_tool_result(
    snapshot: PlannerSnapshot, tools: ReadOnlyTools
) -> None:
    calls = [(n, a) for n, a in CALLS if not a.get("invoice_ids")]
    blob = canonical_json(build_context(snapshot)) + "".join(canonical_json(tools.call(n, a)) for n, a in calls)
    assert INJECTION not in blob and "Acme" not in blob and "12345678901234" not in blob
    assert "BEN-002" in blob  # the id is there; the name is not


def test_the_forecast_tool_reports_the_width_the_policy_gate_uses(tools: ReadOnlyTools) -> None:
    rows = {(f["currency"], f["horizon_weeks"]): f for f in tools.call("get_forecast", {})["forecasts"]}  # type: ignore[union-attr]
    assert rows[("USD", 4)]["relative_width"] == "0.6667"  # (16000 - 8000) / 12000
    assert rows[("INR", 4)]["relative_width"] is None  # point estimate 0: undefined, never a divide-by-zero
