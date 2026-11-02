"""Integration fixtures: a real PostgreSQL, migrated by Alembic, one fresh database per test.

Point the tests at a server with TEST_DATABASE_ADMIN_URL (default: the `postgres-test` service in docker-compose.yml,
`make db-test-up`). The role needs CREATEDB. A migrated template database is built once per session and each test gets
its own clone, so tests can really commit, run concurrently, and never see each other's rows.

If the server cannot be reached the tests are skipped, unless TESSERA_REQUIRE_DB=1 (CI and `make test-integration`
set it), in which case they fail: a silent skip must not pass for a green run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from it_helpers import API_KEY, HEADERS, ROOT, T0
from sqlalchemy.orm import Session, sessionmaker
from tessera_agent import LLMClient, LLMError
from tessera_api.deps import Settings
from tessera_api.main import create_app
from tessera_api.planning import WorldProvider
from tessera_store import make_engine, make_session_factory

ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")
REQUIRE_DB = os.environ.get("TESSERA_REQUIRE_DB") == "1"


def _url_for(name: str) -> str:
    return sa.engine.make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


def _admin() -> sa.Engine:
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)


def _migrate(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    admin = _admin()
    try:
        with admin.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except sa.exc.OperationalError as exc:
        if REQUIRE_DB:
            pytest.fail(f"TESSERA_REQUIRE_DB=1 but the test database is unreachable: {exc.orig}")
        pytest.skip(f"no PostgreSQL at TEST_DATABASE_ADMIN_URL ({exc.orig.__class__.__name__}); run `make db-test-up`")
    name = f"tessera_tpl_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        _migrate(_url_for(name))
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def new_db(template_db: str) -> Iterator[Callable[[], str]]:
    """Call it for a fresh, migrated database URL. Everything it made is dropped at the end of the test."""
    made: list[str] = []
    admin = _admin()

    def make() -> str:
        name = f"tessera_t_{uuid.uuid4().hex[:10]}"
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
        made.append(name)
        return _url_for(name)

    yield make
    with admin.connect() as conn:
        for name in made:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def db_url(new_db: Callable[[], str]) -> str:
    return new_db()


@pytest.fixture
def engine(db_url: str) -> Iterator[sa.Engine]:
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def _llm_not_configured() -> LLMClient:
    raise LLMError("no key")


@pytest.fixture
def make_client(db_url: str) -> Iterator[Callable[..., TestClient]]:
    """Build an app against this test's database. `llm` is a factory (a new scripted model per plan run)."""
    clients: list[TestClient] = []

    def make(
        *,
        world_provider: WorldProvider | None = None,
        llm: Callable[[], LLMClient] = _llm_not_configured,
        database_url: str | None = None,
        authenticated: bool = True,
    ) -> TestClient:
        ticks = iter(range(10_000))
        app = create_app(
            Settings(database_url=database_url or db_url, api_key=API_KEY),
            world_provider=world_provider,
            llm_factory=llm,
            clock=lambda: T0 + timedelta(seconds=next(ticks)),  # strictly increasing, so audit order is unambiguous
        )
        client = TestClient(app, headers=HEADERS if authenticated else None)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
