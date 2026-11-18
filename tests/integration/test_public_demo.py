"""The public demo entry point: DemoStack reuses one existing database (no CREATE DATABASE) and the story holds."""

from __future__ import annotations

import importlib.util
import os
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from it_helpers import ROOT
from tessera_worker.demo_stack import DemoStack

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
KEY = "public-demo-key-0123456789"


def _load_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("serve_public_demo", ROOT / "scripts" / "serve_public_demo.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def scratch_url():  # type: ignore[no-untyped-def]
    name = f"tessera_pub_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError:
        if os.environ.get("TESSERA_REQUIRE_DB") == "1":
            pytest.fail("PostgreSQL is not reachable and TESSERA_REQUIRE_DB=1")
        pytest.skip("PostgreSQL is not reachable")
    yield sa.engine.make_url(ADMIN).set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_the_story_is_seeded_into_an_existing_database_and_settles_after_approval(scratch_url: str) -> None:
    script = _load_script()
    stack = DemoStack(database_url=scratch_url, api_key=KEY)
    script.seed_story(stack, KEY)
    client = TestClient(stack.api, headers={"X-API-Key": KEY})
    pending = [p for p in client.get("/proposals").json() if p["status"] == "PENDING_APPROVAL"]
    assert len(pending) == 1 and stack.balances() == {"INR": "60,000.00", "USD": "6,000.00"}

    assert client.post(f"/proposals/{pending[0]['id']}/approve").status_code == 200
    stack.cycle()  # conversion settles
    stack.cycle()  # payout submitted
    transfer = next(
        t for p in client.get("/proposals").json() if p["action_type"] == "PAY_SUPPLIER"
        if (t := stack.external_id(p["id"]))
    )  # fmt: skip
    assert stack.send_webhook("payout.transfer.paid", transfer, event_id="evt-test")[0] == 200
    assert stack.balances() == {"USD": "4,250.00"}

    again = DemoStack(database_url=scratch_url, api_key=KEY)  # a restart wipes and re-seeds, never duplicates
    assert again.balances() == {"INR": "60,000.00"}
    again.close()
    stack.close()
