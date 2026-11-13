"""Day 13 verification. Usage: python scripts/verify_day13.py [a|b|c|d|e|f|all]

(c) SIGKILLs a real worker process, so this script also runs itself as that worker ("worker" mode).
Everything uses a throwaway PostgreSQL (TEST_DATABASE_ADMIN_URL, default localhost:5433), the mock port, and for (f)
the sandbox adapter over the respx fake server. Nothing here reaches the real Airwallex sandbox.
"""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
import pickle
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("packages/core", "packages/airwallex", "packages/agent", "packages/store", "packages/audit",
            "packages/evalkit", "apps/api", "apps/worker", "tests", "tests/integration"):  # fmt: skip
    sys.path.insert(0, str(ROOT / sub))

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from it_helpers import APPROVE_ITEM, FixtureWorldProvider, add_business, scripted_llm
from tessera_airwallex import MockAirwallex
from tessera_audit import AuditEvent, AuditLog
from tessera_core.domain import Currency
from tessera_core.domain.lifecycle import IllegalTransitionError, transition_action, transition_execution
from tessera_store import make_engine, make_session_factory
from tessera_store import models as m
from worker_helpers import BALANCES, ChangingWorld, DictDirectory, exec_deps, loop_deps

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
PY = str(ROOT / ".venv/bin/python")
STATE_FIELDS = ("_balances", "_quotes", "_used_quotes", "_beneficiaries", "_cards", "_idempotency", "_counter")


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def new_db() -> str:
    name = f"verify13_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(ADMIN).set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


class PersistentMock(MockAirwallex):
    """The mock whose state survives a process kill (pickled to a file), and which can die at a chosen point."""

    def __init__(self, path: Path, kill_at: str | None = None) -> None:
        super().__init__(BALANCES)
        self.path, self.kill_at = path, kill_at
        if path.exists():
            for k, v in pickle.loads(path.read_bytes()).items():
                setattr(self, k, v)

    def save(self) -> None:
        self.path.write_bytes(pickle.dumps({k: getattr(self, k) for k in STATE_FIELDS}))

    def _die(self, point: str) -> None:
        if self.kill_at == point:
            self.save()
            print(f"   !! worker pid {os.getpid()} SIGKILLed at: {point}", flush=True)
            os.kill(os.getpid(), signal.SIGKILL)

    def create_fx_quote(self, **kw):  # type: ignore[no-untyped-def]
        out = super().create_fx_quote(**kw)
        self.save()  # the quote exists at the vendor; keep it across the kill
        return out

    def execute_conversion(self, *, quote_id: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        self._die("before_vendor_call")  # key is stored, the vendor has not been asked
        out = super().execute_conversion(quote_id=quote_id, idempotency_key=idempotency_key)
        self._die("after_vendor_call_before_result_recorded")  # the vendor acted; Tessera never heard
        self.save()
        return out


def eur(port: MockAirwallex) -> str:
    return str(next(b.available.amount for b in port.get_balances() if b.available.currency == Currency.EUR))


def table(url: str) -> None:
    eng = sa.create_engine(url)
    with eng.connect() as c:
        rows = c.execute(sa.text("SELECT e.id, e.status, e.idempotency_key, e.external_id, p.status "
                                 "FROM executions e JOIN proposals p ON p.id = e.proposal_id")).all()  # fmt: skip
    print("   executions (id, exec status, idempotency_key, external_id, proposal status):")
    for r in rows:
        print("    ", tuple(r))
    print(f"   rows: {len(rows)}")


# ---------------------------------------------------------------------------------------------- worker mode
def worker(url: str, pickle_path: str, kill_at: str) -> None:
    from tessera_worker.loop import run_cycle

    port = PersistentMock(Path(pickle_path), None if kill_at == "none" else kill_at)
    factory = make_session_factory(make_engine(url))
    deps = loop_deps(exec_deps(factory, port, ChangingWorld(FixtureWorldProvider())), lambda: scripted_llm(APPROVE_ITEM))
    report = run_cycle(deps)
    port.save()
    print(f"   worker pid {os.getpid()} finished a cycle: {dict(report)}   mock EUR = {eur(port)}")


def spawn(url: str, path: Path, kill_at: str) -> int:
    r = subprocess.run([PY, __file__, "worker", url, str(path), kill_at], capture_output=True, text=True)
    print(r.stdout.rstrip())
    if r.returncode not in (0, -signal.SIGKILL):
        print(r.stderr[-1500:])
    print(f"   exit code: {r.returncode}" + ("  (killed by SIGKILL)" if r.returncode == -signal.SIGKILL else ""))
    return r.returncode


# ---------------------------------------------------------------------------------------------------- (a)
def part_a() -> None:
    banner("(a) import-linter: passing, then a forbidden import in agent, then revert")
    env = {**os.environ, "PYTHONPATH": ":".join(str(ROOT / p) for p in
           ("packages/core", "packages/airwallex", "packages/agent", "packages/store", "packages/audit",
            "packages/evalkit", "apps/api", "apps/worker"))}  # fmt: skip

    def lint() -> int:
        r = subprocess.run([str(ROOT / ".venv/bin/lint-imports")], capture_output=True, text=True, env=env, cwd=ROOT)
        keep = [ln for ln in r.stdout.splitlines() if ("KEPT" in ln or "BROKEN" in ln or "Contracts:" in ln
                                                       or "->" in ln or "imports" in ln.lower() and "tessera_" in ln)]  # fmt: skip
        print("\n".join(keep[:14]))
        return r.returncode

    print("-- 1. clean tree:")
    print(f"   exit code {lint()}")
    probe = ROOT / "packages/agent/tessera_agent/_forbidden_probe.py"
    probe.write_text("import tessera_airwallex  # deliberately forbidden\n")
    try:
        print("-- 2. after adding `import tessera_airwallex` to packages/agent/tessera_agent/_forbidden_probe.py:")
        print(f"   exit code {lint()}")
    finally:
        probe.unlink()
    print("-- 3. reverted (probe file removed):")
    print(f"   exit code {lint()}   probe exists: {probe.exists()}")


# ---------------------------------------------------------------------------------------------------- (b)
def part_b() -> None:
    banner("(b) webhook: bad signature, replayed (stale) event, duplicate event id")
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from tessera_airwallex.webhooks import compute_signature
    from tessera_worker.settings import WorkerSettings
    from tessera_worker.webhooks import create_webhook_app
    from worker_helpers import seed_proposal

    url, secret = new_db(), "whsec-verify-0123456789"
    factory = make_session_factory(make_engine(url))
    with factory() as s:
        seed_proposal(s, status="SUBMITTED")
        s.add(m.Execution(proposal_id="prp-1", idempotency_key="tsr-k1", vendor_request_id="req-1",
                          status="SUBMITTED", external_id="tr-1"))  # fmt: skip
        s.commit()
    now = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    client = TestClient(create_webhook_app(WorkerSettings(database_url=url, webhook_secret=SecretStr(secret)),
                                           session_factory=factory, clock=lambda: now))  # fmt: skip
    body = json.dumps({"id": "evt-1", "name": "payout.transfer.paid", "created_at": "2026-03-02T09:00:00+0000",
                       "data": {"object": {"id": "tr-1", "request_id": "req-1"}}}).encode()  # fmt: skip

    def headers(at: datetime, key: str = secret) -> dict[str, str]:
        ts = str(int(at.timestamp() * 1000))
        return {"x-timestamp": ts, "x-signature": compute_signature(SecretStr(key), ts, body)}

    def state() -> str:
        with factory() as s:
            return f"proposal={s.scalars(sa.select(m.Proposal.status)).one()} " \
                   f"execution={s.scalars(sa.select(m.Execution.status)).one()} " \
                   f"webhook_events={s.scalar(sa.select(sa.func.count()).select_from(m.WebhookEvent))}"  # fmt: skip

    r = client.post("/webhooks/airwallex", content=body, headers=headers(now, "the-wrong-secret"))
    print(f"-- bad signature          -> HTTP {r.status_code} {r.json()}   | {state()}")
    r = client.post("/webhooks/airwallex", content=body, headers=headers(now - timedelta(minutes=30)))
    print(f"-- replayed (30 min old, validly signed) -> HTTP {r.status_code} {r.json()}   | {state()}")
    r = client.post("/webhooks/airwallex", content=body, headers=headers(now))
    print(f"-- first delivery         -> HTTP {r.status_code} {r.json()}   | {state()}")
    r = client.post("/webhooks/airwallex", content=body, headers=headers(now))
    print(f"-- same event id again    -> HTTP {r.status_code} {r.json()}   | {state()}")
    tampered = body.replace(b"evt-1", b"evt-2").replace(b"paid", b"failed")
    r = client.post("/webhooks/airwallex", content=tampered, headers=headers(now))
    print(f"-- tampered body, old signature -> HTTP {r.status_code} {r.json()}")
    late = json.dumps({"id": "evt-3", "name": "payout.transfer.failed", "data": {"object": {"id": "tr-1"}}}).encode()
    ts = str(int(now.timestamp() * 1000))
    sig = {"x-timestamp": ts, "x-signature": compute_signature(SecretStr(secret), ts, late)}
    r = client.post("/webhooks/airwallex", content=late, headers=sig)
    print(f"-- late, out-of-order 'failed' after 'paid' (new id) -> HTTP {r.status_code} {r.json()}   | {state()}")
    with factory() as s:
        kinds = [e.event_type for e in s.scalars(sa.select(AuditEvent).order_by(AuditEvent.seq))]
        print(f"   audit events: {kinds}   (execution_result once; one webhook_received per distinct event id)")
        print(f"   audit chain verifies: {AuditLog(s).verify_chain().ok}")


# ---------------------------------------------------------------------------------------------------- (c)
def part_c() -> None:
    banner("(c) SIGKILL the worker between 'key stored' and 'result recorded', restart, at-most-once")
    for kill_at, note in (("after_vendor_call_before_result_recorded", "the vendor DID debit; Tessera never heard back"),
                          ("before_vendor_call", "key stored, vendor never asked")):  # fmt: skip
        print(f"\n--- scenario: kill at '{kill_at}' ({note})")
        url, path = new_db(), Path(f"/tmp/claude-0/v13/mock_{uuid.uuid4().hex[:6]}.pkl")
        with make_session_factory(make_engine(url))() as s:
            add_business(s)
            s.commit()
        print("1. first worker, will be killed mid-execution:")
        spawn(url, path, kill_at)
        print(f"   mock EUR balance after the kill: {eur(PersistentMock(path))}  (started at 50000)")
        table(url)
        print("2. restart the worker (same database, same mock state, no kill):")
        spawn(url, path, "none")
        table(url)
        print(f"   FINAL mock EUR balance: {eur(PersistentMock(path))}   => debited exactly once (50000 - 1000 = 49000.00)")
        print("3. a third restart changes nothing:")
        spawn(url, path, "none")
        print(f"   mock EUR balance: {eur(PersistentMock(path))}")
        with make_session_factory(make_engine(url))() as s:
            print(f"   audit chain verifies: {AuditLog(s).verify_chain().ok}")


# ---------------------------------------------------------------------------------------------------- (d)
def part_d() -> None:
    banner("(d) verdict flips to BLOCK between approval and execution: NOT executed")
    from collections import Counter

    from tessera_worker.loop import _business_cycle
    from tessera_worker.submit import process_proposal
    from worker_helpers import FlakyMock

    url = new_db()
    factory = make_session_factory(make_engine(url))
    with factory() as s:
        add_business(s)
        s.commit()
    port, world = FlakyMock(), ChangingWorld(FixtureWorldProvider())
    deps = loop_deps(exec_deps(factory, port, world), lambda: scripted_llm(APPROVE_ITEM))
    _business_cycle(deps, "B0", Counter())  # plan only
    with factory() as s:
        row = s.scalars(sa.select(m.Proposal)).one()
        pid = row.id
        d = s.scalars(sa.select(m.PolicyDecision)).one()
        print(f"-- planned: proposal {pid} status={row.status}; policy decision phase={d.phase} verdict={d.verdict}")
    print(f"-- mock EUR before: {eur(port)}")
    world.change = lambda ctx: ctx.model_copy(update={"kill_switch_engaged": True})
    print("-- operator engages the kill switch; worker now tries to execute the APPROVED proposal ...")
    print(f"   process_proposal -> {process_proposal(deps.exec, pid)!r}")
    with factory() as s:
        print(f"   proposal status = {s.scalars(sa.select(m.Proposal.status)).one()}")
        print(f"   executions rows = {s.scalar(sa.select(sa.func.count()).select_from(m.Execution))}")
        for d in s.scalars(sa.select(m.PolicyDecision).order_by(m.PolicyDecision.id)):
            print(f"   decision #{d.id}: phase={d.phase:9s} verdict={d.verdict:8s} reasons={list(d.reasons)}")
    print(f"   port calls = {port.calls}   mock EUR after: {eur(port)}")


# ---------------------------------------------------------------------------------------------------- (e)
def part_e() -> None:
    banner("(e) illegal state transitions raise")
    cases = [("action", "SETTLED", "APPROVED"), ("action", "PROPOSED", "SUBMITTED"), ("action", "PENDING_APPROVAL", "SUBMITTED"),
             ("action", "BLOCKED", "APPROVED"), ("action", "FAILED", "SETTLED"), ("execution", "PENDING", "SETTLED"),
             ("execution", "SETTLED", "FAILED"), ("execution", "FAILED", "SUBMITTED")]  # fmt: skip
    for kind, old, new in cases:
        fn = transition_action if kind == "action" else transition_execution
        try:
            fn(old, new)
            print(f"   {kind:9s} {old} -> {new}: ALLOWED (!)")
        except IllegalTransitionError as exc:
            print(f"   {kind:9s} {old} -> {new}: IllegalTransitionError: {exc}")
    print("   and the legal path:", " -> ".join(["PROPOSED", "APPROVED", "SUBMITTED", "SETTLED"]),
          [transition_action(a, b).value for a, b in (("PROPOSED", "APPROVED"), ("APPROVED", "SUBMITTED"), ("SUBMITTED", "SETTLED"))])  # fmt: skip
    print("-- and at the database layer (a SETTLED proposal cannot be moved back):")
    from tessera_store import repo_exec
    from worker_helpers import seed_proposal

    url = new_db()
    factory = make_session_factory(make_engine(url))
    with factory() as s:
        seed_proposal(s, status="SETTLED")
        row = s.get(m.Proposal, "prp-1")
        try:
            repo_exec.move_proposal(s, row, "APPROVED")  # type: ignore[arg-type]
        except IllegalTransitionError as exc:
            print(f"   repo_exec.move_proposal(SETTLED -> APPROVED): IllegalTransitionError: {exc}")
        s.rollback()
        print(f"   status in DB afterwards: {s.get(m.Proposal, 'prp-1').status}")  # type: ignore[union-attr]
    print("-- and the DB CHECK constraint refuses an unknown status outright:")
    with factory() as s:
        try:
            s.execute(sa.text("UPDATE proposals SET status = 'DONE' WHERE id = 'prp-1'"))
        except sa.exc.IntegrityError as exc:
            print(f"   IntegrityError: {str(exc.orig).splitlines()[0]}")


# ---------------------------------------------------------------------------------------------------- (f)
def audit_trail(url: str) -> None:
    with make_session_factory(make_engine(url))() as s:
        print("   audit trail (seq, event, hash prefix <- prev prefix, key facts):")
        for e in s.scalars(sa.select(AuditEvent).order_by(AuditEvent.seq)):
            p = json.loads(e.payload)
            facts = {k: p[k] for k in ("phase", "verdict", "outcome", "source", "idempotency_key", "external_id",
                                       "action_type", "model_version", "status") if k in p}  # fmt: skip
            print(f"    #{e.seq:<2d} {e.event_type:<18s} {e.hash[:10]} <- {e.prev_hash[:10]}  {facts}")
        print(f"   chain verifies: {AuditLog(s).verify_chain()}")
        ex = s.scalars(sa.select(m.Execution)).one()
        pr = s.scalars(sa.select(m.Proposal)).one()
        print(f"   proposal {pr.id}: {pr.action_type} {pr.params} -> {pr.status}")
        print(f"   execution: status={ex.status} key={ex.idempotency_key} external_id={ex.external_id}")


def part_f() -> None:
    banner("(f) one full cycle for a small conversion (EUR 1000 -> INR) and its audit trail")
    live = all(os.environ.get(k) for k in ("AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY"))
    print(f"real sandbox credentials present: {live}.  The real sandbox is {'reachable?' if live else 'NOT being used.'}")
    if not live:
        print("No AIRWALLEX_* credentials in this session and api.sandbox.airwallex.com is refused by the egress proxy, so a"
              "\nrun against the REAL sandbox is impossible here. Below: the same cycle through SandboxAirwallex (the real"
              "\nHTTP adapter: auth, quote, request_id lookup, conversion) against the in-repo fake server (respx).")
    import respx
    from fake_airwallex import FakeAirwallex
    from pydantic import SecretStr
    from tessera_airwallex import SandboxAirwallex
    from tessera_airwallex._http import SandboxConfig
    from tessera_airwallex._http import SandboxHttp
    from tessera_worker.loop import run_cycle

    url = new_db()
    factory = make_session_factory(make_engine(url))
    with factory() as s:
        add_business(s)
        s.commit()
    clock = lambda: datetime.now(UTC)  # noqa: E731
    with respx.mock(assert_all_called=False) as router:
        fake = FakeAirwallex(router, clock, BALANCES)
        cfg = SandboxConfig(base_url="https://api.sandbox.airwallex.com", client_id=SecretStr(fake.client_id),
                            api_key=SecretStr(fake.api_key))  # fmt: skip
        port = SandboxAirwallex(cfg, http=SandboxHttp(cfg, clock=clock, sleep=lambda _s: None), clock=clock)
        deps = loop_deps(exec_deps(factory, port, ChangingWorld(FixtureWorldProvider()), directory=DictDirectory(),
                                   clock=clock), lambda: scripted_llm(APPROVE_ITEM))  # fmt: skip
        print(f"-- EUR before: {fake.balances[Currency.EUR]}  INR before: {fake.balances[Currency.INR]}")
        print(f"-- cycle report: {dict(run_cycle(deps))}")
        print(f"-- EUR after:  {fake.balances[Currency.EUR]}  INR after:  {fake.balances[Currency.INR]}")
        print("-- HTTP calls the adapter made:", [f"{mth} {p}" for mth, p in fake.calls])
    audit_trail(url)


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        worker(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        which = sys.argv[1] if len(sys.argv) > 1 else "all"
        for key, fn in (("a", part_a), ("b", part_b), ("c", part_c), ("d", part_d), ("e", part_e), ("f", part_f)):
            if which in ("all", key):
                fn()
