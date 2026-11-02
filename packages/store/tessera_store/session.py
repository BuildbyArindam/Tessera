"""Engine and session construction. Nothing here reads the environment: the caller passes the URL."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def normalise_url(url: str) -> str:
    """Accept `postgresql://` and `postgres://` and always use the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    return create_engine(normalise_url(url), pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: routes read attributes after commit to build their response.
    return sessionmaker(engine, expire_on_commit=False)
