"""DEMO / VERIFICATION ONLY: the real API and real worker execution steps, on the mock adapter, for Day 16.

    python scripts/serve_day16_demo.py --port 8016 [--worker-every 2] [--database-name tessera_e2e]

What it runs:
  * a fresh, migrated database at autonomy level L1, with business B0 and a stored 30/60/90 day forecast per currency;
  * the real API. POST /plan/run uses a SCRIPTED model that returns five proposals the real policy engine decides as
    APPROVE (a conversion within the L1 cap), ESCALATE (two conversions above it) and BLOCK (two payouts, which L1 does
    not permit), so the Proposals screen has every kind;
  * a background thread running the worker's execute and reconcile steps (never its planning step) against the MOCK
    adapter, every few seconds. An approval in the dashboard is therefore picked up, RE-CHECKED against policy, and
    executed by the same code the production worker uses, and the status the screen shows moves because of that.

Nothing here reaches the real Airwallex sandbox. Needs a PostgreSQL admin URL (TEST_DATABASE_ADMIN_URL, default :5433).
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("packages/core", "packages/airwallex", "packages/agent", "packages/store", "packages/audit",
            "packages/evalkit", "apps/api", "apps/worker", "tests", "tests/integration"):  # fmt: skip
    sys.path.insert(0, str(ROOT / sub))

import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config
from it_helpers import (
    APPROVE_ITEM,
    BLOCK_ITEM,
    ESCALATE_ITEM,
    FixtureWorldProvider,
    add_business,
    scripted_llm,
    set_stored_level,
)  # fmt: skip
from tessera_airwallex import MockAirwallex
from tessera_api.deps import Settings
from tessera_api.main import create_app
from tessera_core.domain import Currency
from tessera_core.forecast import ForecastResult
from tessera_store import make_engine, make_session_factory, repo
from tessera_worker.loop import _execute_approved, _reconcile
from worker_helpers import BALANCES, DictDirectory, exec_deps, loop_deps

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
API_KEY = os.environ.get("TESSERA_API_KEY", "e2e-demo-key-0123456789")
log = logging.getLogger("serve_day16_demo")

# (point, half-width) of the weekly-net forecast per currency and horizon: invented numbers, not a model.
FORECASTS = {Currency.EUR: (Decimal("4200"), Decimal("1500")), Currency.INR: (Decimal("-90000"), Decimal("60000")),
             Currency.USD: (Decimal("8800"), Decimal("2100"))}  # fmt: skip
HORIZONS = (4, 8, 13)


# At L1 a conversion above the small cap is escalated to a person (the real engine decides; nothing here fakes it).
# Two of them, so one can be approved and one rejected. Payouts are not permitted at L1: those items are BLOCKED.
ESCALATE_CONVERT = {**APPROVE_ITEM, "params": {**APPROVE_ITEM["params"], "amount": "3000.00"}}
ESCALATE_CONVERT_2 = {**APPROVE_ITEM, "params": {**APPROVE_ITEM["params"], "amount": "3500.00"}}
LEVEL = 1  # L1 Low-risk: a conversion above the small cap is escalated to a person; payouts are not permitted yet.


def new_db(name: str | None = None) -> str:
    """A fresh, migrated database. A NAME makes it reproducible: any database of that name is dropped first."""
    name = name or f"demo16_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(ADMIN).set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


def seed(url: str) -> None:
    factory = make_session_factory(make_engine(url))
    with factory() as session:
        add_business(session)  # business B0
        results = []
        for currency, (point, half) in FORECASTS.items():
            for weeks in HORIZONS:
                spread = half * Decimal(weeks) / Decimal(4)  # intervals widen with the horizon
                results.append(ForecastResult(
                    business_id="B0", currency=currency, horizon_weeks=weeks,
                    point=point * weeks, lower=point * weeks - spread,
                    upper=point * weeks + spread, target_coverage=0.8, model_version="demo-v1",
                ))  # fmt: skip
        repo.save_forecasts(session, date.today(), results)
        session.commit()
    set_stored_level(url, LEVEL, at=datetime.now(UTC))


def run_worker(url: str, every: float) -> None:
    """Execute approved proposals (re-checking policy first) and reconcile webhooks, forever. Planning is NOT run."""
    factory = make_session_factory(make_engine(url))
    port = MockAirwallex(BALANCES)
    directory = DictDirectory()  # no payees: payouts are blocked at L1, so only conversions are ever executed
    clock = lambda: datetime.now(UTC)  # noqa: E731
    deps = loop_deps(
        exec_deps(factory, port, FixtureWorldProvider(), directory=directory, clock=clock), lambda: scripted_llm()
    )
    while True:
        try:
            report: Counter[str] = Counter()
            _execute_approved(deps, report)
            _reconcile(deps, report)
            if report:
                log.warning("worker cycle: %s", dict(report))
        except Exception:
            log.exception("worker cycle failed")
        time.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--worker-every", type=float, default=2.0)
    parser.add_argument("--database-url", default=None, help="use an existing database as it is (no seeding)")
    parser.add_argument("--database-name", default=None, help="drop, recreate and seed a database with this name")
    args = parser.parse_args()
    url = args.database_url or new_db(args.database_name)
    if args.database_url is None:
        seed(url)
    threading.Thread(target=run_worker, args=(url, args.worker_every), daemon=True).start()
    app = create_app(
        Settings(database_url=url, api_key=API_KEY),
        world_provider=FixtureWorldProvider(),
        llm_factory=lambda: scripted_llm(APPROVE_ITEM, ESCALATE_CONVERT, ESCALATE_CONVERT_2, ESCALATE_ITEM, BLOCK_ITEM),
    )
    print(
        f"serving the Day 16 demo API on http://127.0.0.1:{args.port} (database {sa.engine.make_url(url).database})",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
