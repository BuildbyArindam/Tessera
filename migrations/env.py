"""Alembic environment. The URL comes from code (`config.attributes["url"]`) or the DATABASE_URL variable."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from tessera_audit import model as _audit_model  # noqa: F401  (registers audit_events on the shared metadata)
from tessera_store import models as _store_models  # noqa: F401  (registers the other tables)
from tessera_store.base import Base
from tessera_store.session import normalise_url

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL (or pass a url in config.attributes) before running migrations")
    return normalise_url(str(url))


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
