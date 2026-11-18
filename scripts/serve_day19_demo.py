"""DEMO / VERIFICATION ONLY: the Day 19 end-to-end stack for the Playwright test (and for clicking through by hand).

    python scripts/serve_day19_demo.py --port 8019 --control-port 8039 [--worker-every 2] [--database-name tessera_e2e19]

Runs, against the MOCK Airwallex adapter and a fresh, seeded database (see apps/worker/tessera_worker/demo_stack.py):
  * the real API on --port;
  * a background thread running the REAL worker cycle (plan, policy, execute approved after a policy re-check, reconcile)
    every few seconds, with the scripted planner;
  * a small demo-only CONTROL app on --control-port that plays the outside world, because a browser cannot:
        POST /customer-pays   {"invoice_id": "..."}   the customer's money lands in the wallet
        POST /webhook-paid    {"proposal_id": "..."}  the vendor sends a SIGNED payout.transfer.paid to the real webhook
        GET  /state                                   wallet balances and the vendor transfer ids
The control app is not part of Tessera and exists only here.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
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

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from tessera_worker.demo_stack import DemoStack

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
log = logging.getLogger("serve_day19_demo")


class Pays(BaseModel):
    invoice_id: str


class Paid(BaseModel):
    proposal_id: str


def control_app(stack: DemoStack) -> FastAPI:
    app = FastAPI(title="Tessera Day 19 demo control (not part of Tessera)")

    @app.post("/customer-pays")
    def customer_pays(body: Pays) -> dict[str, str]:
        paid = stack.customer_pays(body.invoice_id)
        return {"amount": str(paid.amount), "currency": paid.currency.value}

    @app.post("/webhook-paid")
    def webhook_paid(body: Paid) -> dict[str, object]:
        transfer = stack.external_id(body.proposal_id)
        if transfer is None:
            raise HTTPException(409, "that proposal has no vendor transfer yet")
        code, result = stack.send_webhook("payout.transfer.paid", transfer)
        return {"http_status": code, **result}

    @app.get("/state")
    def state() -> dict[str, object]:
        return {"balances": stack.balances()}

    return app


def run_worker(stack: DemoStack, every: float) -> None:
    while True:
        try:
            report = stack.cycle()
            if any(k.startswith(("exec_", "proposals_created")) and v for k, v in report.items()):
                log.warning("worker cycle: %s", dict(report))
        except Exception:
            log.exception("worker cycle failed")
        time.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8019)
    parser.add_argument("--control-port", type=int, default=8039)
    parser.add_argument("--worker-every", type=float, default=2.0)
    parser.add_argument("--database-name", default="tessera_e2e19")
    args = parser.parse_args()
    stack = DemoStack(
        ADMIN, db_name=args.database_name, api_key=os.environ.get("TESSERA_API_KEY", "e2e-demo-key-0123456789")
    )
    threading.Thread(target=run_worker, args=(stack, args.worker_every), daemon=True).start()
    control = uvicorn.Server(
        uvicorn.Config(control_app(stack), host="127.0.0.1", port=args.control_port, log_level="warning")
    )
    threading.Thread(target=control.run, daemon=True).start()
    print(
        f"serving the Day 19 demo API on http://127.0.0.1:{args.port} (control app on :{args.control_port})", flush=True
    )
    uvicorn.run(stack.api, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
