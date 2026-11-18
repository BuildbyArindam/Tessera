"""DEMO / VERIFICATION ONLY (Day 19): the real API, worker loop and webhook endpoint on the MOCK Airwallex adapter, with a
world provider that reads the database.

The production world provider is still a TODO (docs/daily-log.md, Day 13/14), so this module supplies one for the demo:
obligations come from the open payables and open invoices in the database, balances from the vendor port, forecasts from
the stored forecasts. Beneficiaries and the FX history are fixtures (red-team beneficiaries; a seeded series anchored on
the mock's own USD/INR rate). Nothing here reaches the real Airwallex sandbox, and the planner is the scripted
`ThresholdLLM` (no model key is needed), so the PLANNING is scripted; the API, the policy engine, the approval, the
execution re-check, the idempotency keys, the webhook handling and the audit chain are the real code.

Fixed on purpose, so the transcript is the same every run:
  * the clock is 2026-03-02 09:00 UTC (the red-team world's NOW);
  * the mock is configured with a zero spread and USD/INR = 80.0, so converting exactly funds the payable. With the mock's
    default 50 bps spread a conversion sized from the mid rate falls a few hundred rupees short and the planner would
    ask for a second tiny conversion: a real behaviour worth fixing later, but not the story of this demo.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session
from tessera_agent import Obligation, PlannerSnapshot
from tessera_airwallex import MockAirwallex
from tessera_airwallex.webhooks import compute_signature
from tessera_api.deps import Settings
from tessera_api.main import create_app
from tessera_api.planning import PlanningUnavailable, PlanningWorld
from tessera_core.domain import Currency, Money, Proposal
from tessera_core.forecast import ForecastResult
from tessera_core.policy import PolicyContext, load_ruleset
from tessera_evalkit.agent_eval.scenario import Fx
from tessera_evalkit.agent_eval.standin import ThresholdLLM
from tessera_evalkit.agent_eval.world import make_series
from tessera_evalkit.redteam.world import AS_OF, BUSINESS, NOW, World, build_world
from tessera_store import make_engine, make_session_factory, repo, repo_dashboard
from tessera_store import models as m

from tessera_worker.dispatch import CardRef
from tessera_worker.loop import LoopDeps, run_cycle
from tessera_worker.settings import WorkerSettings
from tessera_worker.submit import ExecDeps
from tessera_worker.webhooks import create_webhook_app

ROOT = Path(__file__).resolve().parents[3]
API_KEY = "demo-key-0123456789abcdef"
WEBHOOK_SECRET = "whsec-demo-0123456789"
STORED_LEVEL = 2  # L2: payouts allowed, conversions above USD 3,000 need a person
MOCK_USD_VALUES = {
    Currency.USD: Decimal(1),
    Currency.EUR: Decimal("1.08"),
    Currency.GBP: Decimal("1.27"),
    Currency.INR: Decimal("0.0125"),
    Currency.SGD: Decimal("0.74"),
    Currency.AUD: Decimal("0.66"),
}  # fmt: skip  (INR 0.0125 USD each: USD/INR = 80.0 exactly)
OPENING = {Currency.INR: Decimal("60000")}  # no USD until the customer pays: nothing to fund the gap from yet
PAYABLE = {
    "id": "PAY-0001",
    "amount": Decimal("200000.00"),
    "currency": "INR",
    "due_in_days": 3,
    "beneficiary": "BEN-001",
}
# (point, half-width) of the 4-week net cash flow. The USD interval is deliberately WIDE (relative width 1.4 > 0.5):
# the forecast_confidence_gate sends any USD conversion to a person, which is the escalation the demo shows.
FORECASTS = {"USD": (Decimal("8000"), Decimal("5600")), "INR": (Decimal("400000"), Decimal("40000")),
             "EUR": (Decimal("3000"), Decimal("600"))}  # fmt: skip


def fresh_database(admin_url: str, name: str | None = None) -> str:
    """Drop and recreate a migrated database. A fixed NAME makes it reproducible."""
    name = name or f"tessera_demo_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = sa.engine.make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


def reset_database(url: str) -> str:
    """Empty an EXISTING database (drop the public schema) and migrate it. For hosted free databases where the role
    cannot CREATE DATABASE (PUBLIC_DEMO only: this destroys everything in `url`)."""
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


class DemoDirectory:
    """Tessera beneficiary id -> the vendor's id, filled when the beneficiaries are registered with the mock."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def beneficiary(self, business_id: str, beneficiary_id: str) -> str | None:
        return self.ids.get(beneficiary_id) if business_id == BUSINESS else None

    def card(self, business_id: str, vendor_id: str) -> CardRef | None:
        return None


class DemoWorld:
    """A WorldProvider that reads the database and the vendor port on every call, so a changed world is seen."""

    def __init__(self, port: MockAirwallex) -> None:
        self.port = port
        fx = (("USD/INR", 80.0, 0.3), ("EUR/INR", 86.4, 0.3), ("EUR/USD", 1.08, 0.3))
        self.base = dataclasses.replace(
            build_world(),
            fx_history={p: make_series(Fx(pair=p, spot=s, daily_vol_pct=v, seed=19)) for p, s, v in fx},
        )

    def load(self, session: Session, business_id: str) -> PlanningWorld:
        if business_id != BUSINESS:
            raise PlanningUnavailable("no planning data for this business")
        balances = tuple(self.port.get_balances())
        payables = repo_dashboard.list_open_payables(
            session, business_id, due_on_or_before=AS_OF + timedelta(days=120), limit=50
        )
        invoices = list(
            session.scalars(
                sa.select(m.Invoice).where(m.Invoice.business_id == business_id, m.Invoice.status == "OPEN")
            )
        )
        obligations = tuple(
            [Obligation(id=p.id, kind="PAYABLE", amount=Money(amount=p.amount, currency=Currency(p.currency)), due_on=p.due_on, beneficiary_id=p.counterparty_id) for p in payables]
            + [Obligation(id=i.id, kind="RECEIVABLE", amount=Money(amount=i.amount, currency=Currency(i.currency)), due_on=i.due_on, invoice_id=i.id) for i in invoices]
        )  # fmt: skip
        forecasts = tuple(
            ForecastResult(business_id=f.business_id, currency=f.currency, horizon_weeks=f.horizon_weeks, point=f.point, lower=f.lower,
                           upper=f.upper, target_coverage=f.target_coverage, model_version=f.model_version)
            for f in repo_dashboard.latest_forecasts(session, business_id)
        )  # fmt: skip
        frame = pd.DataFrame(
            {
                "business_id": [business_id] * len(invoices),
                "invoice_id": [i.id for i in invoices],
                "issue_date": pd.Series([pd.Timestamp(i.issued_on) for i in invoices], dtype="datetime64[ns]"),
                "paid_date": pd.Series([pd.NaT] * len(invoices), dtype="datetime64[ns]"),
            }
        )
        world: World = dataclasses.replace(self.base, obligations=obligations, forecasts=forecasts, invoices=frame)
        snapshot = PlannerSnapshot(
            business_id=business_id, as_of=AS_OF, balances=balances, forecasts=forecasts, fx_history=world.fx_history,
            invoices=frame, late_scorer=None, obligations=obligations, beneficiaries=world.beneficiaries,
            card_vendor_ids=frozenset(),
        )  # fmt: skip
        held = {b.currency: b.available for b in balances}

        def context(proposal: Proposal, flags: tuple[str, ...]) -> PolicyContext:
            return world.policy_context(proposal, flags).model_copy(update={"balances": held})

        return PlanningWorld(snapshot=snapshot, policy_context=context)


class DemoStack:
    def __init__(
        self, admin_url: str | None = None, *, db_name: str | None = None, api_key: str = API_KEY,
        database_url: str | None = None,
    ) -> None:  # fmt: skip
        """`admin_url`: create a fresh database on that server. `database_url`: wipe and reuse that one database."""
        if database_url is not None:
            self.url = reset_database(database_url)
        elif admin_url is not None:
            self.url = fresh_database(admin_url, db_name)
        else:
            raise ValueError("give admin_url (create a database) or database_url (reuse one)")
        self.engine = make_engine(self.url)
        self.factory = make_session_factory(self.engine)
        self.port = MockAirwallex(OPENING, spread_bps=0, usd_values=MOCK_USD_VALUES)
        self.directory = DemoDirectory()
        self.world = DemoWorld(self.port)
        for b in self.world.base.beneficiaries:  # register each payee with the mock once, as onboarding would
            ref = self.port.create_beneficiary(
                name=b.name,
                currency=b.currency,
                account_identifier=SecretStr("000111222"),
                idempotency_key=f"seed-{b.id}",
            )
            self.directory.ids[b.id] = ref.beneficiary_id
        self.llm = ThresholdLLM(model="scripted-demo-v1", convert_fraction=Decimal(1), convert_when_due_within=3)
        self.api: FastAPI = create_app(
            Settings(database_url=self.url, api_key=api_key),
            world_provider=self.world,
            llm_factory=lambda: self.llm,
            clock=lambda: NOW,
        )
        self.hooks = TestClient(
            create_webhook_app(
                WorkerSettings(database_url=self.url, webhook_secret=SecretStr(WEBHOOK_SECRET)),
                session_factory=self.factory,
                clock=lambda: NOW,
            )
        )
        self.exec = ExecDeps(
            session_factory=self.factory, port=self.port, directory=self.directory, world_provider=self.world,
            ruleset=load_ruleset(), clock=lambda: NOW, max_age=timedelta(hours=24),
        )  # fmt: skip
        self.deps = LoopDeps(exec=self.exec, llm_factory=lambda: self.llm)
        self._events = 0
        self.seed()

    def seed(self) -> None:
        with self.factory() as s:
            s.add(m.Business(id=BUSINESS, name="Demo Studio", base_currency="INR"))
            s.flush()
            repo_dashboard.upsert_payables(s, [{
                "id": PAYABLE["id"], "business_id": BUSINESS, "kind": "supplier_invoice", "counterparty_id": PAYABLE["beneficiary"],
                "counterparty_name": "Acme Supplies", "amount": PAYABLE["amount"], "currency": PAYABLE["currency"],
                "due_on": AS_OF + timedelta(days=int(str(PAYABLE["due_in_days"]))), "status": "OPEN",
            }])  # fmt: skip
            results = [
                ForecastResult(business_id=BUSINESS, currency=Currency(c), horizon_weeks=h, point=p * h / 4, lower=p * h / 4 - w * h / 4,
                               upper=p * h / 4 + w * h / 4, target_coverage=0.8, model_version="demo-v1")
                for c, (p, w) in FORECASTS.items() for h in (4, 8, 13)
            ]  # fmt: skip
            repo.save_forecasts(s, AS_OF, results)
            s.commit()
        with self.engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE autonomy_state SET level = :l, since = :t WHERE id = 1"), {"l": STORED_LEVEL, "t": NOW}
            )
        self.sync_balances()

    # ---- the worker, the vendor and the outside world ----------------------------------------------------------
    def sync_balances(self) -> None:
        """Copy the vendor's balances into the table the dashboard's Overview reads (the real worker's job, still a TODO)."""
        with self.factory() as s:
            repo_dashboard.upsert_balances(
                s, BUSINESS, {b.currency.value: b.available.amount for b in self.port.get_balances()}, NOW
            )
            s.commit()

    def cycle(self) -> Counter[str]:
        """One full worker cycle: plan, run policy, execute what is APPROVED (fresh policy first), reconcile webhooks."""
        report = run_cycle(self.deps)
        self.sync_balances()
        return report

    def customer_pays(self, invoice_id: str) -> Money:
        """DEMO ONLY: the customer's payment lands in the wallet (the mock is credited) and the invoice is marked PAID."""
        with self.factory() as s:
            inv = s.get(m.Invoice, invoice_id)
            assert inv is not None and inv.status == "OPEN"
            paid = Money(amount=inv.amount, currency=Currency(inv.currency))
            inv.status = "PAID"
            s.commit()
        self.port._credit(paid)  # noqa: SLF001  (no port method receives money; this is the mock's own bookkeeping)
        self.sync_balances()
        return paid

    def send_webhook(self, name: str, resource_id: str, *, event_id: str | None = None) -> tuple[int, dict[str, Any]]:
        """Deliver a SIGNED vendor event to the real webhook endpoint (signature, freshness and dedupe all apply).
        The same `event_id` twice is a redelivery; a new id is a new event."""
        self._events += 1
        body = json.dumps({"id": event_id or f"evt-demo-{self._events}", "name": name, "created_at": "2026-03-02T09:00:00+0000",
                           "data": {"object": {"id": resource_id}}}).encode()  # fmt: skip
        stamp = str(int(NOW.timestamp() * 1000))
        sig = compute_signature(SecretStr(WEBHOOK_SECRET), stamp, body)
        res = self.hooks.post("/webhooks/airwallex", content=body, headers={"x-timestamp": stamp, "x-signature": sig})
        return res.status_code, res.json()

    def external_id(self, proposal_id: str) -> str | None:
        with self.factory() as s:
            row = s.scalars(sa.select(m.Execution).where(m.Execution.proposal_id == proposal_id)).first()
            return None if row is None else row.external_id

    def balances(self) -> dict[str, str]:
        return {b.currency.value: f"{b.available.amount:,.2f}" for b in self.port.get_balances() if b.available.amount}

    def close(self) -> None:
        self.engine.dispose()
