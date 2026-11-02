"""Day 11 verification: migrations, tamper evidence, and every API route over real HTTP with curl.

Needs a PostgreSQL role with CREATEDB:  TEST_DATABASE_ADMIN_URL=postgresql+psycopg://user:pass@host:port/postgres
Makes scratch databases and drops them. Exit 0 only if every check holds.

    python scripts/verify_day11.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from tessera_audit import AuditEventType, AuditLog
from tessera_store import make_engine

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
KEY = "verify-day11-key-0123456789"
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def url_for(name: str) -> str:
    return sa.engine.make_url(ADMIN).set(database=name).render_as_string(hide_password=False)


def make_db(admin: sa.Engine) -> tuple[str, str]:
    name = f"tessera_v11_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url_for(name)
    command.upgrade(cfg, "head")
    return name, url_for(name)


def curl(method: str, path: str, *, key: bool = True, body: str | None = None) -> tuple[int, str]:
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, BASE + path, "-H", "Content-Type: application/json"]
    if key:
        args += ["-H", f"X-API-Key: {KEY}"]
    if body is not None:
        args += ["--data", body]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    text, _, code = out.rpartition("\n")
    return int(code), text


def tamper_evidence(db_url: str) -> None:
    print("\n1. Tamper evidence (10 events)")
    engine = make_engine(db_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    t0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    with factory() as s:
        log = AuditLog(s)
        for i in range(10):
            log.append(AuditEventType.PROPOSAL_CREATED, {"i": i}, at=t0 + timedelta(seconds=i))
        s.commit()
        status = log.verify_chain()
    check("verify_chain on 10 events is OK", status.ok and status.checked == 10, f"head seq {status.head_seq}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE audit_events SET payload = '{}' WHERE seq = 4"))
        check("raw SQL UPDATE is rejected by the trigger", False, "it was accepted")
    except DBAPIError as exc:
        check(
            "raw SQL UPDATE is rejected by the trigger", "append-only" in str(exc.orig), str(exc.orig).splitlines()[0]
        )
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM audit_events WHERE seq = 4"))
        check("raw SQL DELETE is rejected by the trigger", False, "it was accepted")
    except DBAPIError as exc:
        check("raw SQL DELETE is rejected by the trigger", "append-only" in str(exc.orig))
    with engine.begin() as conn:  # a table owner switches the trigger off in this scratch database and edits one row
        conn.execute(sa.text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        conn.execute(sa.text("UPDATE audit_events SET payload = :p WHERE seq = 7"), {"p": '{"i":999}'})
        conn.execute(sa.text("ALTER TABLE audit_events ENABLE TRIGGER USER"))
    with factory() as s:
        status = AuditLog(s).verify_chain()
    check(
        "after tampering with seq 7, verify_chain names seq 7",
        not status.ok and status.broken_seq == 7,
        status.reason or "",
    )
    engine.dispose()


def routes(db_url: str) -> None:
    print("\n2. Every route over HTTP (curl)")
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO businesses (id, name, base_currency) VALUES ('B0', 'Demo Studio', 'INR')"))
    engine.dispose()
    env = {**os.environ, "DATABASE_URL": db_url, "TESSERA_API_KEY": KEY}
    server = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve_day11_demo.py"), "--port", str(PORT)], env=env)
    try:
        for _ in range(60):
            try:
                if curl("GET", "/openapi.json", key=False)[0] == 200:
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.5)
        invoice = json.dumps(
            {
                "id": "INV-9",
                "business_id": "B0",
                "customer_id": "CUST-1",
                "amount": {"amount": "2500.00", "currency": "USD"},
                "issued_on": "2026-02-01",
                "due_on": "2026-03-01",
                "notes": "March milestone",
            }
        )
        plan = curl("POST", "/plan/run", body='{"business_id": "B0"}')
        proposals = {p["status"]: p["id"] for p in json.loads(plan[1])["created"]} if plan[0] == 200 else {}
        escalated = proposals.get("PENDING_APPROVAL", "none")
        cases = [
            ("GET /docs (no key)", curl("GET", "/docs", key=False), 200),
            ("POST /invoices (no key)", curl("POST", "/invoices", key=False, body=invoice), 401),
            ("GET /audit (no key)", curl("GET", "/audit", key=False), 401),
            ("POST /invoices", curl("POST", "/invoices", body=invoice), 201),
            ("POST /invoices (duplicate id)", curl("POST", "/invoices", body=invoice), 409),
            (
                "POST /invoices (malformed: float amount)",
                curl("POST", "/invoices", body=invoice.replace('"2500.00"', "2500.5")),
                422,
            ),
            (
                "POST /invoices (unknown business)",
                curl("POST", "/invoices", body=invoice.replace('"B0"', '"NOPE"')),
                404,
            ),
            ("GET /forecasts", curl("GET", "/forecasts"), 200),
            ("GET /forecasts?limit=0 (malformed)", curl("GET", "/forecasts?limit=0"), 422),
            ("POST /plan/run", plan, 200),
            ("GET /proposals", curl("GET", "/proposals"), 200),
            (
                "POST /proposals/{blocked}/approve",
                curl("POST", f"/proposals/{proposals.get('BLOCKED', 'none')}/approve"),
                409,
            ),
            (
                "POST /proposals/{escalated}/approve",
                curl("POST", f"/proposals/{escalated}/approve", body='{"note":"ok"}'),
                200,
            ),
            ("POST /proposals/{escalated}/approve (again)", curl("POST", f"/proposals/{escalated}/approve"), 409),
            ("POST /proposals/unknown/reject", curl("POST", "/proposals/prp-unknown/reject"), 404),
            ("GET /audit", curl("GET", "/audit?limit=3"), 200),
            ("GET /audit/verify", curl("GET", "/audit/verify"), 200),
        ]
        for label, (code, _), expected in cases:
            check(f"{label} -> {code}", code == expected, f"expected {expected}" if code != expected else "")
        verify = json.loads(curl("GET", "/audit/verify")[1])
        check("audit chain verifies after the whole run", verify["ok"] is True, f"{verify['checked']} events")
        statuses = sorted(p["status"] for p in json.loads(curl("GET", "/proposals")[1]))
        check(
            "proposals: one APPROVED by policy, one APPROVED by a human, one BLOCKED",
            statuses == ["APPROVED", "APPROVED", "BLOCKED"],
            str(statuses),
        )
    finally:
        server.terminate()
        server.wait(timeout=10)


def main() -> int:
    admin = sa.create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    made: list[str] = []
    try:
        for step in (tamper_evidence, routes):
            name, url = make_db(admin)
            made.append(name)
            step(url)
    finally:
        with admin.connect() as conn:
            for name in made:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All Day 11 checks passed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
