"""PUBLIC DEMO ONLY: the Day 19 demo stack as one web process, for a free host (Render) and free Postgres (Neon).

    DATABASE_URL=postgresql://... TESSERA_API_KEY=<16+ chars> python scripts/serve_public_demo.py  (PORT: 8000)

What it is: the real API, policy engine, worker loop and webhook endpoint on the MOCK Airwallex adapter and synthetic
data, with the scripted planner (no model key). On every start it WIPES the database in DATABASE_URL, seeds the Day 19
world, ingests a USD invoice, lets the customer "pay" and runs the worker once, so the dashboard opens on an ESCALATED
conversion. Two background threads then keep it alive: the worker cycle, and a stand-in vendor that sends the signed
`payout.transfer.paid` webhook for any transfer the mock accepted (otherwise a payout would stay SUBMITTED forever).
Nothing here reaches the real Airwallex sandbox or a real model. Never put real credentials in this process.
"""

# ruff: noqa: E402
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("packages/core", "packages/airwallex", "packages/agent", "packages/store", "packages/audit",
            "packages/evalkit", "apps/api", "apps/worker"):  # fmt: skip
    sys.path.insert(0, str(ROOT / sub))

import sqlalchemy as sa
import uvicorn
from fastapi.testclient import TestClient
from tessera_store import models as m
from tessera_worker.demo_stack import DemoStack

log = logging.getLogger("serve_public_demo")
INVOICE = {
    "id": "INV-DEMO-001", "business_id": "B0", "customer_id": "CUST-ACME",
    "amount": {"amount": "6000.00", "currency": "USD"}, "issued_on": "2026-03-02", "due_on": "2026-03-04",
    "notes": "Net 2 days. Thanks!",
}  # fmt: skip


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    for old in ("postgres://", "postgresql://"):
        if url.startswith(old):
            return "postgresql+psycopg://" + url[len(old) :]
    return url


def api_key() -> str:
    key = os.environ.get("TESSERA_API_KEY", "")
    if len(key) < 16:
        raise SystemExit("TESSERA_API_KEY must be set and at least 16 characters")
    return key


def seed_story(stack: DemoStack, key: str) -> None:
    """Invoice in, customer pays, one worker cycle: the dashboard opens on an escalated conversion."""
    client = TestClient(stack.api, headers={"X-API-Key": key})
    res = client.post("/invoices", json=INVOICE)
    if res.status_code != 201:
        raise SystemExit(f"could not ingest the demo invoice: {res.status_code} {res.text}")
    stack.cycle()  # nothing to fund from yet: no proposal
    stack.customer_pays(INVOICE["id"])
    stack.cycle()  # USD 6,000 in the wallet: the planner proposes a conversion and the forecast gate escalates it


def run_worker(stack: DemoStack, every: float) -> None:
    while True:
        try:
            stack.cycle()
        except Exception:
            log.exception("worker cycle failed")
        time.sleep(every)


def run_vendor(stack: DemoStack, every: float) -> None:
    """Stand-in for the vendor: confirm each submitted transfer once, through the real signed webhook endpoint."""
    told: set[str] = set()
    while True:
        try:
            with stack.factory() as s:
                rows = s.scalars(sa.select(m.Execution).where(m.Execution.status == "SUBMITTED")).all()
                pending = [r.external_id for r in rows if r.external_id and r.external_id not in told]
            for transfer in pending:
                code, _ = stack.send_webhook("payout.transfer.paid", transfer, event_id=f"evt-public-{transfer}")
                told.add(transfer)
                log.warning("vendor confirmed %s -> %s", transfer, code)
        except Exception:
            log.exception("vendor loop failed")
        time.sleep(every)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    key = api_key()
    stack = DemoStack(database_url=database_url(), api_key=key)
    seed_story(stack, key)
    threading.Thread(target=run_worker, args=(stack, 3.0), daemon=True).start()
    threading.Thread(target=run_vendor, args=(stack, 2.0), daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(stack.api, host="0.0.0.0", port=port, log_level="warning")  # noqa: S104  (the host's router needs it)


if __name__ == "__main__":
    main()
