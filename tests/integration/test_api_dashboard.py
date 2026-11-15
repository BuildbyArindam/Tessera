"""Day 15: the dashboard's read endpoints: businesses, balances, obligations, invoice risk, latest forecasts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from it_helpers import T0, add_business
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tessera_store import models as m
from tessera_store import models_dashboard as d
from tessera_store import repo_dashboard

Client = Callable[..., TestClient]
AS_OF = date(2026, 3, 2)
NEW_ROUTES = [
    ("GET", "/businesses"), ("GET", "/businesses/B0/balances"), ("GET", "/businesses/B0/obligations"),
    ("GET", "/invoices?business_id=B0"), ("GET", "/forecasts?business_id=B0&latest=true"),
]  # fmt: skip


@pytest.fixture
def client(make_client: Client, session: Session) -> TestClient:
    add_business(session)
    return make_client()


def add_invoice(
    session: Session, ident: str, *, due: date, amount: str = "100.00", status: str = "OPEN", currency: str = "USD"
) -> None:
    session.add(
        m.Invoice(id=ident, business_id="B0", customer_id="CUST-1", amount=Decimal(amount), currency=currency,
                  issued_on=date(2026, 1, 1), due_on=due, status=status)
    )  # fmt: skip
    session.commit()


def add_forecast(
    session: Session, *, as_of: date, currency: str, horizon: int, point: str, version: str = "v1"
) -> None:
    p = Decimal(point)
    session.add(
        m.Forecast(business_id="B0", currency=currency, horizon_weeks=horizon, as_of=as_of, point=p,
                   lower=p - 10, upper=p + 10, target_coverage=0.8, model_version=version)
    )  # fmt: skip
    session.commit()


# ---- authentication -------------------------------------------------------------------------------
@pytest.mark.parametrize(("method", "path"), NEW_ROUTES)
def test_every_new_route_is_401_without_the_key(make_client: Client, method: str, path: str) -> None:
    assert make_client(authenticated=False).request(method, path).status_code == 401


# ---- businesses and balances ------------------------------------------------------------------------
def test_businesses_are_listed_in_id_order(client: TestClient, session: Session) -> None:
    session.add(m.Business(id="A0", name="Alpha", base_currency="USD"))
    session.commit()
    body = client.get("/businesses").json()
    assert [b["id"] for b in body] == ["A0", "B0"]
    assert body[0] == {"id": "A0", "name": "Alpha", "base_currency": "USD"}


def test_balances_are_empty_until_something_is_ingested_and_money_is_a_string(
    client: TestClient, session: Session
) -> None:
    assert client.get("/businesses/B0/balances").json() == {"business_id": "B0", "balances": []}
    repo_dashboard.upsert_balances(session, "B0", {"USD": Decimal("1234.50"), "EUR": Decimal("10.00")}, T0)
    session.commit()
    body = client.get("/businesses/B0/balances").json()
    assert [b["available"] for b in body["balances"]] == [
        {"amount": "10.00", "currency": "EUR"},
        {"amount": "1234.50", "currency": "USD"},
    ]
    assert body["balances"][0]["as_of"].startswith("2026-03-02T09:00:00")


def test_a_second_ingest_replaces_a_balance_and_leaves_other_currencies(client: TestClient, session: Session) -> None:
    repo_dashboard.upsert_balances(session, "B0", {"USD": Decimal("100"), "EUR": Decimal("50")}, T0)
    repo_dashboard.upsert_balances(session, "B0", {"USD": Decimal("75.25")}, datetime(2026, 3, 3, tzinfo=UTC))
    session.commit()
    amounts = {
        b["available"]["currency"]: b["available"]["amount"]
        for b in client.get("/businesses/B0/balances").json()["balances"]
    }
    assert amounts == {"USD": "75.25", "EUR": "50.00"}


def test_unknown_business_is_404_on_every_per_business_route(client: TestClient) -> None:
    assert client.get("/businesses/NOPE/balances").status_code == 404
    assert client.get("/businesses/NOPE/obligations").status_code == 404
    assert client.get("/invoices", params={"business_id": "NOPE"}).status_code == 404


# ---- obligations ------------------------------------------------------------------------------------
def _payable(ident: str, due: date, *, status: str = "OPEN", name: str | None = "Acme Ltd") -> dict[str, Any]:
    return {"id": ident, "business_id": "B0", "kind": "supplier_invoice", "counterparty_id": "CP-1",
            "counterparty_name": name, "amount": Decimal("500.00"), "currency": "INR", "due_on": due,
            "status": status}  # fmt: skip


def test_obligations_merge_receivables_and_payables_in_due_order(client: TestClient, session: Session) -> None:
    add_invoice(session, "INV-late", due=date(2026, 2, 20))  # overdue by 10 days
    add_invoice(session, "INV-soon", due=date(2026, 3, 9), amount="3000.00")
    add_invoice(session, "INV-paid", due=date(2026, 3, 5), status="PAID")  # not owed: excluded
    add_invoice(session, "INV-far", due=date(2026, 12, 1))  # beyond the horizon: excluded
    repo_dashboard.upsert_payables(
        session, [_payable("P-1", date(2026, 3, 5)), _payable("P-paid", date(2026, 3, 6), status="PAID")]
    )
    session.commit()

    body = client.get("/businesses/B0/obligations", params={"days_ahead": 30, "as_of": AS_OF.isoformat()}).json()

    assert body["as_of"] == "2026-03-02"
    assert [(o["id"], o["days_until_due"]) for o in body["items"]] == [
        ("receivable:INV-late", -10),
        ("payable:P-1", 3),
        ("receivable:INV-soon", 7),
    ]
    soon = body["items"][2]
    assert soon["amount"] == {"amount": "3000.00", "currency": "USD"} and soon["invoice_id"] == "INV-soon"
    assert body["items"][1]["counterparty"] == "Acme Ltd" and body["items"][1]["payable_kind"] == "supplier_invoice"


def test_a_payable_without_a_name_falls_back_to_its_counterparty_id(client: TestClient, session: Session) -> None:
    repo_dashboard.upsert_payables(session, [_payable("P-1", date(2026, 3, 5), name=None)])
    session.commit()
    items = client.get("/businesses/B0/obligations", params={"as_of": AS_OF.isoformat()}).json()["items"]
    assert items[0]["counterparty"] == "CP-1"


def test_obligations_are_measured_from_the_newest_forecast_then_from_today(
    client: TestClient, session: Session
) -> None:
    add_invoice(session, "INV-1", due=date(2026, 3, 12))
    # No forecast: "today" comes from the injected clock (T0 = 2026-03-02).
    assert client.get("/businesses/B0/obligations").json()["as_of"] == "2026-03-02"
    add_forecast(session, as_of=date(2026, 3, 9), currency="USD", horizon=4, point="100")
    body = client.get("/businesses/B0/obligations").json()
    assert body["as_of"] == "2026-03-09"
    assert body["items"][0]["days_until_due"] == 3


def test_obligations_reject_a_bad_window_and_honour_the_limit(client: TestClient, session: Session) -> None:
    for bad in (0, 181):
        assert client.get("/businesses/B0/obligations", params={"days_ahead": bad}).status_code == 422
    for i in range(5):
        add_invoice(session, f"INV-{i}", due=date(2026, 3, 3 + i))
    items = client.get("/businesses/B0/obligations", params={"limit": 2, "as_of": AS_OF.isoformat()}).json()["items"]
    assert [o["id"] for o in items] == ["receivable:INV-0", "receivable:INV-1"]


# ---- invoices with risk -----------------------------------------------------------------------------
def _risk(invoice_id: str, p: float, **extra: Any) -> dict[str, Any]:
    return {"invoice_id": invoice_id, "p_late": p, "already_late": False, "model": "lgbm", "pay_date_low": None,
            "pay_date_median": None, "pay_date_high": None, "scored_as_of": AS_OF, **extra}  # fmt: skip


def test_invoices_carry_their_score_and_null_when_unscored(client: TestClient, session: Session) -> None:
    add_invoice(session, "INV-a", due=date(2026, 3, 10))
    add_invoice(session, "INV-b", due=date(2026, 3, 11))
    repo_dashboard.upsert_late_risk(session, [_risk("INV-a", 0.42, pay_date_median=date(2026, 3, 20))])
    session.commit()
    body = client.get("/invoices", params={"business_id": "B0"}).json()
    assert [i["id"] for i in body] == ["INV-a", "INV-b"]
    assert body[0]["late_risk"]["p_late"] == 0.42
    assert body[0]["late_risk"]["pay_date_median"] == "2026-03-20"
    assert body[1]["late_risk"] is None
    assert body[0]["amount"] == {"amount": "100.00", "currency": "USD"}


def test_invoices_can_be_listed_riskiest_first_with_unscored_last_and_filtered_by_status(
    client: TestClient, session: Session
) -> None:
    add_invoice(session, "INV-low", due=date(2026, 3, 1))
    add_invoice(session, "INV-high", due=date(2026, 3, 9))
    add_invoice(session, "INV-none", due=date(2026, 3, 2))
    add_invoice(session, "INV-paid", due=date(2026, 3, 3), status="PAID")
    repo_dashboard.upsert_late_risk(session, [_risk("INV-low", 0.1), _risk("INV-high", 0.9)])
    session.commit()
    ranked = client.get("/invoices", params={"business_id": "B0", "status": "OPEN", "order_by": "risk"}).json()
    assert [i["id"] for i in ranked] == ["INV-high", "INV-low", "INV-none"]
    assert [i["id"] for i in client.get("/invoices", params={"business_id": "B0", "status": "PAID"}).json()] == [
        "INV-paid"
    ]


def test_invoice_list_validates_its_query(client: TestClient) -> None:
    assert client.get("/invoices").status_code == 422  # business_id is required
    assert client.get("/invoices", params={"business_id": "B0", "status": "SOMETIMES"}).status_code == 422
    assert client.get("/invoices", params={"business_id": "B0", "order_by": "vibes"}).status_code == 422


# ---- latest forecasts -------------------------------------------------------------------------------
def test_latest_forecasts_keep_the_newest_row_per_currency_and_horizon(client: TestClient, session: Session) -> None:
    add_forecast(session, as_of=date(2026, 2, 23), currency="USD", horizon=4, point="1")
    add_forecast(session, as_of=date(2026, 3, 2), currency="USD", horizon=4, point="2", version="v1")
    add_forecast(
        session, as_of=date(2026, 3, 2), currency="USD", horizon=4, point="3", version="v2"
    )  # later write wins
    add_forecast(session, as_of=date(2026, 2, 23), currency="USD", horizon=8, point="4")  # only one: stays
    add_forecast(session, as_of=date(2026, 3, 2), currency="EUR", horizon=4, point="5")
    body = client.get("/forecasts", params={"business_id": "B0", "latest": "true"}).json()
    assert [(f["currency"], f["horizon_weeks"], f["point"]["amount"], f["as_of"]) for f in body] == [
        ("EUR", 4, "5.00", "2026-03-02"),
        ("USD", 4, "3.00", "2026-03-02"),
        ("USD", 8, "4.00", "2026-02-23"),
    ]
    only_usd = client.get("/forecasts", params={"business_id": "B0", "latest": "true", "currency": "USD"}).json()
    assert {f["currency"] for f in only_usd} == {"USD"}


def test_latest_forecasts_need_a_business(client: TestClient) -> None:
    assert client.get("/forecasts", params={"latest": "true"}).status_code == 422


def test_the_plain_forecast_list_is_unchanged(client: TestClient, session: Session) -> None:
    add_forecast(session, as_of=date(2026, 2, 23), currency="USD", horizon=4, point="1")
    add_forecast(session, as_of=date(2026, 3, 2), currency="USD", horizon=4, point="2")
    assert [f["as_of"] for f in client.get("/forecasts").json()] == ["2026-03-02", "2026-02-23"]


# ---- the database refuses what the models forbid ------------------------------------------------------
def test_the_new_tables_enforce_their_own_rules(session: Session) -> None:
    add_business(session)
    bad_rows: list[object] = [
        d.BalanceSnapshot(business_id="B0", currency="USD", available=Decimal("-1.00"), as_of=T0),
        d.BalanceSnapshot(business_id="B0", currency="XXX", available=Decimal("1.00"), as_of=T0),
        d.Payable(id="P", business_id="B0", kind="payroll", counterparty_id="C", amount=Decimal("0"),
                  currency="INR", due_on=AS_OF),
        d.Payable(id="P", business_id="B0", kind="lottery", counterparty_id="C", amount=Decimal("1"),
                  currency="INR", due_on=AS_OF),
    ]  # fmt: skip
    for row in bad_rows:
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    add_invoice(session, "INV-1", due=AS_OF)
    for p in (-0.1, 1.5):
        session.add(d.LateRiskScore(invoice_id="INV-1", p_late=p, already_late=False, model="lgbm", scored_as_of=AS_OF))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
