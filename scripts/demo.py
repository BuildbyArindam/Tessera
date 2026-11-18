"""`make demo`: the whole Tessera loop, end to end, as a readable transcript (Day 19).

    TEST_DATABASE_ADMIN_URL=postgresql+psycopg://... python scripts/demo.py [--out artifacts/demo-transcript.txt]

Steps: seed a fresh database; ingest a new USD invoice (POST /invoices); run the worker; show an ESCALATED conversion;
approve it through the API; let the worker re-check policy and execute it on the MOCK Airwallex adapter; plan and submit
the supplier payout; process the signed vendor webhook; verify the audit chain. Every step asserts what it shows, so a
nonzero exit means the story broke, not just that the text changed.

What is real and what is not is spelled out in apps/worker/tessera_worker/demo_stack.py: the planning is scripted (no
model key needed); the API, policy engine, approval, execution re-check, idempotency, webhook and audit chain are real.
The clock is frozen, so the transcript is identical on every run.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / p) for p in ("core", "airwallex", "agent", "store", "audit", "evalkit")] + [
    str(ROOT / "apps" / p) for p in ("api", "worker")
]

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from tessera_worker.demo_stack import API_KEY, PAYABLE, STORED_LEVEL, DemoStack  # noqa: E402

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
LINES: list[str] = []


def say(text: str = "", indent: int = 0) -> None:
    for line in textwrap.wrap(text, 104, initial_indent=" " * indent, subsequent_indent=" " * (indent + 2)) or [""]:
        LINES.append(line)
        print(line)


def step(n: int, title: str) -> None:
    say()
    say(f"Step {n}: {title}")
    say("-" * 78)


def check(condition: bool, what: str) -> None:
    if not condition:
        say(f"!! expected: {what}")
        raise SystemExit(f"demo failed: {what}")
    say(f"[ok] {what}", 2)


def money(m: dict[str, Any]) -> str:
    return f"{m['currency']} {float(m['amount']):,.2f}"


def proposal_line(p: dict[str, Any]) -> str:
    x = p["params"]
    what = {
        "CONVERT": f"sell {x.get('currency')} {x.get('amount')} for {x.get('to_currency')}",
        "PAY_SUPPLIER": f"pay {x.get('currency')} {x.get('amount')} to {x.get('beneficiary_id')}",
    }.get(p["action_type"], p["action_type"])
    return f"{p['id']}  {p['action_type']}: {what} (for {x.get('obligation_id')})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "demo-transcript.txt")
    out = parser.parse_args().out
    try:
        stack = DemoStack(ADMIN, db_name="tessera_demo")
    except OperationalError:
        print(
            "cannot reach PostgreSQL. Start one with `make db-test-up` (port 5433) or set TEST_DATABASE_ADMIN_URL.",
            file=sys.stderr,
        )
        return 2
    client = TestClient(stack.api, headers={"X-API-Key": API_KEY})
    client.__enter__()

    say("TESSERA END-TO-END DEMO")
    say(
        "Frozen clock 2026-03-02 09:00 UTC. Vendor: MOCK Airwallex (USD/INR 80.00, zero spread). Planner: scripted stand-in, "
        "not an LLM. Real: API, policy engine, approval, execution re-check, idempotency, webhook, audit chain."
    )

    step(1, "Seed the database")
    say(
        f"Fresh PostgreSQL database, migrated. Business B0 'Demo Studio' (reports in INR). Autonomy level L{STORED_LEVEL}: "
        "payouts allowed, conversions above USD 3,000 need a person."
    )
    say(
        f"Open payable {PAYABLE['id']}: INR {PAYABLE['amount']:,} to {PAYABLE['beneficiary']} (Acme Supplies), due in {PAYABLE['due_in_days']} days."
    )
    say("Forecast: the USD interval is wide (1.4 x its point estimate); INR and EUR are narrow.")
    say(
        f"Wallet: {stack.balances()}. The INR payable is short by INR 140,000 and there is no other currency to fund it from."
    )
    check(stack.balances() == {"INR": "60,000.00"}, "the wallet holds only INR 60,000")

    step(2, "Ingest a new USD invoice")
    body = {"id": "INV-DEMO-001", "business_id": "B0", "customer_id": "CUST-ACME", "amount": {"amount": "6000.00", "currency": "USD"},
            "issued_on": "2026-03-02", "due_on": "2026-03-04", "notes": "Net 2 days. Thanks!"}  # fmt: skip
    res = client.post("/invoices", json=body)
    say(
        f"POST /invoices -> {res.status_code}: {money(res.json()['amount'])} from {res.json()['customer_id']}, due {res.json()['due_on']}, status {res.json()['status']}."
    )
    check(res.status_code == 201, "the API stored the invoice (201)")
    check(client.post("/invoices", json=body).status_code == 409, "posting the same invoice again is refused (409)")

    step(3, "Run the worker: the customer has not paid yet")
    report = stack.cycle()
    proposals = client.get("/proposals", params={"business_id": "B0"}).json()
    say(f"Worker cycle: {dict(report)}. Proposals so far: {len(proposals)}.")
    say(
        "The planner sees the receivable and the INR gap, but there is no USD in the wallet yet, so it proposes nothing."
    )
    check(not proposals and stack.balances() == {"INR": "60,000.00"}, "nothing was proposed and no money moved")

    step(4, "The customer pays")
    paid = stack.customer_pays("INV-DEMO-001")
    say(
        f"DEMO ONLY: {paid.currency.value} {paid.amount:,.2f} lands in the wallet and the invoice is marked PAID. Wallet: {stack.balances()}."
    )

    step(5, "Run the worker: an escalated conversion")
    report = stack.cycle()
    proposals = client.get("/proposals", params={"business_id": "B0"}).json()
    check(len(proposals) == 1 and proposals[0]["action_type"] == "CONVERT", "the planner proposed one conversion")
    p = proposals[0]
    d = p["decision"]
    say(proposal_line(p))
    say(f"Policy verdict: {d['verdict']} by {', '.join(d['rule_ids'])}", 2)
    for reason in d["reasons"]:
        say(f"why: {reason}", 4)
    sim = d["simulation"]
    say(
        f"Simulation ({sim['n_paths']} resampled FX paths): expected saving {money(sim['expected_saving'])}, worst case loss {money(sim['worst_case_loss'])}.",
        2,
    )
    say(f"Status: {p['status']}. Wallet is unchanged: {stack.balances()}.", 2)
    check(
        d["verdict"] == "ESCALATE" and p["status"] == "PENDING_APPROVAL",
        "policy escalated it to a person; the worker did not execute it",
    )
    check(stack.balances() == {"INR": "60,000.00", "USD": "6,000.00"}, "no money moved")

    step(6, "Approve it through the API")
    res = client.post(f"/proposals/{p['id']}/approve", json={"note": "Customer paid; fund the supplier."})
    approved = res.json()
    say(
        f"POST /proposals/{p['id']}/approve -> {res.status_code}. Status: {approved['status']}; decided by {approved['approval']['actor']}."
    )
    say("Approving only marks the proposal. The worker must re-run policy on fresh data before anything is sent.")
    check(
        res.status_code == 200 and approved["status"] == "APPROVED", "the proposal is APPROVED and still not executed"
    )
    check(stack.balances() == {"INR": "60,000.00", "USD": "6,000.00"}, "no money moved on approval")
    check(
        client.post(f"/proposals/{p['id']}/approve", json={}).status_code == 409, "a second approval is refused (409)"
    )

    step(7, "Run the worker: execute against the mock vendor")
    report = stack.cycle()
    now = {x["id"]: x for x in client.get("/proposals", params={"business_id": "B0"}).json()}
    say(f"Worker cycle: {dict(report)}.")
    say(
        "Fresh policy re-check passed (the human approval covers the rule that escalated), the idempotency key was stored, then the quote was executed."
    )
    say(f"Status: {now[p['id']]['status']}. Wallet: {stack.balances()}.")
    check(
        now[p["id"]]["status"] == "SETTLED" and stack.balances().get("INR") == "200,000.00",
        "the conversion settled and the wallet now holds INR 200,000",
    )

    step(8, "Run the worker again: plan and submit the supplier payout")
    report = stack.cycle()
    pay = next(
        x for x in client.get("/proposals", params={"business_id": "B0"}).json() if x["action_type"] == "PAY_SUPPLIER"
    )
    say(proposal_line(pay))
    say(
        f"Policy verdict: {pay['decision']['verdict']} (inside every limit, beneficiary approved and established). Worker cycle: {dict(report)}.",
        2,
    )
    say(
        f"Status: {pay['status']}. The vendor has the transfer ({stack.external_id(pay['id'])}) but has not said it is paid, so Tessera does not say so either.",
        2,
    )
    check(
        pay["decision"]["verdict"] == "APPROVE" and pay["status"] == "SUBMITTED",
        "the payout was approved by policy and submitted, not settled",
    )

    step(9, "Process the vendor webhook")
    transfer = stack.external_id(pay["id"])
    assert transfer is not None
    code, first = stack.send_webhook("payout.transfer.paid", transfer, event_id="evt-demo-paid")
    say(f"POST /webhooks/airwallex payout.transfer.paid for {transfer} (signed) -> {code} {first}.")
    code2, again = stack.send_webhook("payout.transfer.paid", transfer, event_id="evt-demo-paid")
    say(f"The same delivery again -> {code2} {again}: acknowledged, applied once.")
    bad = stack.hooks.post(
        "/webhooks/airwallex",
        content=b'{"id":"x","name":"payout.transfer.paid"}',
        headers={"x-timestamp": "1", "x-signature": "bad"},
    )
    say(f"An unsigned or wrongly signed event -> {bad.status_code}.")
    settled = client.get("/proposals", params={"business_id": "B0", "status": "SETTLED"}).json()
    check(
        first == {"status": "applied"} and again == {"status": "duplicate"} and bad.status_code == 401,
        "applied once, redelivery deduplicated, forged event refused",
    )
    check({x["action_type"] for x in settled} == {"CONVERT", "PAY_SUPPLIER"}, "both proposals are SETTLED")
    check(stack.balances() == {"USD": "4,250.00"}, f"final wallet: {stack.balances()}")

    step(10, "Verify the audit chain")
    chain = client.get("/audit/verify").json()
    events = client.get("/audit", params={"limit": 500}).json()["events"]
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["event_type"]] = kinds.get(e["event_type"], 0) + 1
    say(f"{chain['checked']} hash-chained events: " + ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items())) + ".")
    say(
        f"Head event #{chain['head_seq']}. Chain OK: {chain['ok']}.  (The head hash is not printed: it is not identical between runs.)"
    )
    check(chain["ok"] and chain["checked"] == len(events), "every event re-hashes and links to the one before it")
    need = {
        "proposal_created",
        "policy_decision",
        "approval_requested",
        "approved",
        "execution_submitted",
        "execution_result",
        "webhook_received",
    }
    check(
        need <= set(kinds),
        "the trail holds the proposal, the policy decisions, the human approval, both executions and the webhook",
    )

    say()
    say(
        "DONE. The agent proposed, the policy engine escalated, a person approved, the worker re-checked and executed, the vendor confirmed, the audit chain verified."
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LINES) + "\n")
    client.__exit__(None, None, None)
    stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
