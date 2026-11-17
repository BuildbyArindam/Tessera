"""The red-team stack: the real API, worker and webhook endpoint on a throwaway PostgreSQL and a call-logging vendor.

`open_stack()` is what `python -m tessera_evalkit.redteam --stack tessera_worker.redteam_stack:open_stack` loads. Each
`with open_stack() as stack:` is a fresh, migrated database, so scenarios cannot see each other's rows.

Needs a PostgreSQL reachable at TEST_DATABASE_ADMIN_URL (default: the `postgres-test` service, `make db-test-up`),
with a role that may create databases.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker
from tessera_agent import FakeLLM, tool_call_reply
from tessera_airwallex.webhooks import compute_signature
from tessera_api.deps import Settings
from tessera_api.main import create_app
from tessera_audit import AuditLog
from tessera_core.policy import load_ruleset
from tessera_evalkit.redteam.stack_api import Move, Planned
from tessera_evalkit.redteam.world import NOW, build_world
from tessera_store import make_engine, make_session_factory, repo
from tessera_store import models as m

from tessera_worker.loop import LoopDeps, _execute_approved, _reconcile
from tessera_worker.redteam_parts import RecordingPort, ScopedDirectory, StackWorld, register_payees
from tessera_worker.settings import WorkerSettings
from tessera_worker.submit import ExecDeps, process_proposal
from tessera_worker.webhooks import create_webhook_app

ROOT = Path(__file__).resolve().parents[3]
ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
API_KEY = "redteam-key-0123456789abcdef"
WEBHOOK_SECRET = "whsec-redteam-0123456789"
_template: list[str] = []


def _url_for(name: str) -> str:
    return sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


def _admin() -> sa.Engine:
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)


def _drop(names: Sequence[str]) -> None:
    admin = _admin()
    with admin.connect() as conn:
        for name in names:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _template_db() -> str:
    if _template:
        return _template[0]
    name = f"tessera_rt_tpl_{uuid.uuid4().hex[:8]}"
    admin = _admin()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = _url_for(name)
    command.upgrade(cfg, "head")
    _template.append(name)
    atexit.register(_drop, [name])
    return name


class RedTeamStack:
    def __init__(self, db_name: str, level: int = 3) -> None:
        self.url = _url_for(db_name)
        self.engine = make_engine(self.url)
        self.factory: sessionmaker[Session] = make_session_factory(self.engine)
        self.port = RecordingPort()
        self.directory = ScopedDirectory()
        self.worlds = {"B0": build_world(), "B1": build_world("B1")}
        self.provider = StackWorld(self.worlds, lambda: self.now)
        for w in self.worlds.values():
            register_payees(self.port, self.directory, w)
        self.now: datetime = NOW
        self._scripts: queue.Queue[tuple[dict[str, Any], ...]] = queue.Queue()
        self.last_outcome = "-"
        self.llm_pause = 0.0
        with self.factory() as s:
            for b in ("B0", "B1"):
                s.add(m.Business(id=b, name=f"Business {b}", base_currency="INR"))
            s.commit()
        self.set_level(level)
        self.app = create_app(
            Settings(database_url=self.url, api_key=API_KEY),
            world_provider=self.provider,
            llm_factory=self._llm,
            clock=lambda: self.now,
        )
        self.client = TestClient(self.app, headers={"X-API-Key": API_KEY})
        self.client.__enter__()
        self.hooks = TestClient(
            create_webhook_app(
                WorkerSettings(database_url=self.url, webhook_secret=SecretStr(WEBHOOK_SECRET)),
                session_factory=self.factory,
                clock=lambda: self.now,
            )
        )
        self.exec = ExecDeps(
            session_factory=self.factory, port=self.port, directory=self.directory, world_provider=self.provider,
            ruleset=load_ruleset(), clock=lambda: self.now, max_age=timedelta(hours=24),
        )  # fmt: skip

    @property
    def moves(self) -> list[Move]:
        return self.port.moves

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.engine.dispose()

    # ---- driving ----------------------------------------------------------------------------------------
    def _llm(self) -> FakeLLM:
        time.sleep(self.llm_pause)  # concurrent runs line up here, then hit the ledger together
        try:  # one script per plan run, taken in order (sync routes run on other threads, so no thread-local)
            script = self._scripts.get_nowait()
        except queue.Empty:
            script = ()
        body = [{**i, "rationale": "Scripted for the red team.", "confidence": 0.7, "evidence_ids": ["ev-1"]}
                for i in script]  # fmt: skip
        reply = json.dumps({"proposals": body})  # twice: the planner retries once on an invalid reply
        return FakeLLM([tool_call_reply("list_upcoming_obligations", {"days_ahead": 30}), reply, reply])

    def plan(self, *items: dict[str, Any], business: str = "B0") -> list[Planned]:
        self._scripts.put(items)
        response = self.client.post("/plan/run", json={"business_id": business})
        if response.status_code != 200:
            self.last_outcome = f"http_{response.status_code}"
            return []
        data = response.json()
        self.last_outcome = str(data["outcome"])
        ids = [p["id"] for p in data["created"]] + list(data["already_known"])
        with self.factory() as s:
            rows = {r.id: r for r in s.scalars(sa.select(m.Proposal).where(m.Proposal.id.in_(ids)))}
            latest = repo.latest_decisions(s, ids)
            return [
                Planned(i, rows[i].status, rows[i].action_type, tuple(latest[i].rule_ids) if i in latest else ())
                for i in ids if i in rows
            ]  # fmt: skip

    def approve(self, proposal_id: str) -> int:
        return int(self.client.post(f"/proposals/{proposal_id}/approve", json={"note": "red team"}).status_code)

    def reject(self, proposal_id: str) -> int:
        return int(self.client.post(f"/proposals/{proposal_id}/reject", json={"note": "red team"}).status_code)

    def execute(self, proposal_id: str) -> str:
        return process_proposal(self.exec, proposal_id)

    def plan_concurrently(self, *batches: tuple[dict[str, Any], ...], business: str = "B0") -> list[list[Planned]]:
        self.llm_pause = 0.4
        try:
            with ThreadPoolExecutor(max_workers=len(batches)) as pool:
                return list(pool.map(lambda b: self.plan(*b, business=business), batches))
        finally:
            self.llm_pause = 0.0

    def cycle(self) -> dict[str, int]:
        """The worker's execute and reconcile steps over everything APPROVED (planning is `plan`, through the API)."""
        report: Counter[str] = Counter()
        deps = LoopDeps(exec=self.exec, llm_factory=self._llm)
        _execute_approved(deps, report)
        _reconcile(deps, report)
        return dict(report)

    def execute_concurrently(self, proposal_ids: Sequence[str]) -> list[str]:
        with ThreadPoolExecutor(max_workers=max(1, len(proposal_ids))) as pool:
            return list(pool.map(self.execute, proposal_ids))

    def webhook(
        self,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        secret: str | None = None,
        at: datetime | None = None,
        altered: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if headers is None:
            stamp = str(int((at or self.now).timestamp() * 1000))
            sig = compute_signature(SecretStr(secret or WEBHOOK_SECRET), stamp, body)
            headers = {"x-timestamp": stamp, "x-signature": sig}
        response = self.hooks.post("/webhooks/airwallex", content=body if altered is None else altered, headers=headers)
        return response.status_code, response.json()

    def api(self, method: str, path: str, *, json: Any = None, key: bool = True) -> tuple[int, Any]:
        headers = {} if key else {"X-API-Key": "wrong-key-0123456789"}
        response = self.client.request(method, path, json=json, headers=headers if not key else None)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    # ---- reading ----------------------------------------------------------------------------------------
    def status(self, proposal_id: str) -> str:
        with self.factory() as s:
            row = s.get(m.Proposal, proposal_id)
            return "MISSING" if row is None else row.status

    def execution(self, proposal_id: str) -> dict[str, Any] | None:
        with self.factory() as s:
            row = s.scalars(sa.select(m.Execution).where(m.Execution.proposal_id == proposal_id)).first()
            if row is None:
                return None
            return {"status": row.status, "request_id": row.vendor_request_id, "external_id": row.external_id,
                    "error": (row.result or {}).get("error")}  # fmt: skip

    def executions_count(self) -> int:
        with self.factory() as s:
            return int(s.scalar(sa.select(sa.func.count()).select_from(m.Execution)) or 0)

    def vendor_request_id(self, proposal_id: str) -> str | None:
        found = self.execution(proposal_id)
        return None if found is None else found["request_id"]

    def audit_chain_ok(self) -> bool:
        with self.factory() as s:
            return AuditLog(s).verify_chain().ok

    # ---- changing the world -----------------------------------------------------------------------------
    def set_kill_switch(self, engaged: bool) -> None:
        self.provider.kill = engaged

    def set_level(self, level: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE autonomy_state SET level = :l, since = :t WHERE id = 1"), {"l": level, "t": NOW}
            )

    def provider_claims_level(self, level: int | None) -> None:
        self.provider.claim = level

    def change_world(self, change: Callable[[Any], Any] | None) -> None:
        self.provider.edit = change

    def revoke_beneficiary(self, business: str, beneficiary_id: str) -> None:
        world = self.worlds[business]
        kept = tuple(
            b if b.id != beneficiary_id else b.model_copy(update={"approved": False}) for b in world.beneficiaries
        )
        from dataclasses import replace

        self.worlds[business] = replace(world, beneficiaries=kept)

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def vendor_balance(self, currency: str) -> str:
        return str(next(b.available.amount for b in self.port.get_balances() if b.available.currency.value == currency))


@contextmanager
def open_stack(level: int = 3) -> Iterator[RedTeamStack]:
    template = _template_db()
    name = f"tessera_rt_{uuid.uuid4().hex[:10]}"
    admin = _admin()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    admin.dispose()
    stack = RedTeamStack(name, level)
    try:
        yield stack
    finally:
        stack.close()
        _drop([name])
