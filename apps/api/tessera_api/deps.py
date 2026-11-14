"""Settings, the per-app context, and the dependencies every route shares: the API key check and a DB session.

There is no user system. One API key protects every route except the OpenAPI docs. The key is compared in constant
time, is never logged or echoed, and the app refuses to start without a long-enough one (fail closed).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session, sessionmaker
from tessera_agent import LLMClient
from tessera_core.autonomy import AutonomyController
from tessera_core.policy import Ruleset

from tessera_api.autonomy_service import AutonomyInputsProvider
from tessera_api.planning import WorldProvider

MIN_API_KEY_LENGTH = 16
API_KEY_HEADER = "X-API-Key"
ACTOR = "operator"  # the single API key stands for one operator; there are no individual users yet


class SettingsError(RuntimeError):
    """The environment does not give a safe configuration. Messages never include the key."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.api_key) < MIN_API_KEY_LENGTH:
            raise SettingsError(f"TESSERA_API_KEY must be set and at least {MIN_API_KEY_LENGTH} characters long")
        if not self.database_url:
            raise SettingsError("DATABASE_URL must be set")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        return cls(database_url=source.get("DATABASE_URL", ""), api_key=source.get("TESSERA_API_KEY", ""))


@dataclass
class AppContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    ruleset: Ruleset
    llm_factory: Callable[[], LLMClient]
    clock: Callable[[], datetime]
    autonomy_controller: AutonomyController
    world_provider: WorldProvider | None = None
    # Builds the controller's evidence. None until one is configured: then nothing is promoted or confirmed.
    autonomy_inputs: AutonomyInputsProvider | None = None


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def key_is_valid(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison. An absent or empty key is never valid."""
    return bool(supplied) and hmac.compare_digest(str(supplied).encode("utf-8"), expected.encode("utf-8"))


def require_api_key(request: Request, supplied: str | None = Security(_key_header)) -> None:
    """Route-level check. The middleware in main.py already enforces this first; this dependency is what makes the
    OpenAPI docs show the key, and it keeps each route safe if the middleware is ever removed."""
    if not key_is_valid(supplied, get_ctx(request).settings.api_key):
        raise HTTPException(
            status_code=401, detail="missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"}
        )


def get_session(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    """One session per request. Routes commit explicitly; leaving the block rolls back anything uncommitted."""
    with ctx.session_factory() as session:
        yield session
