"""The FastAPI app. `create_app()` wires settings, the database, the policy ruleset and the optional plan-run provider.

Run it with:  uvicorn --factory tessera_api.main:create_app
Needs DATABASE_URL and TESSERA_API_KEY (at least 16 characters) in the environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import OperationalError
from tessera_agent import LLMClient, llm_from_env
from tessera_core.policy import Ruleset, load_ruleset
from tessera_store import make_engine, make_session_factory

from tessera_api.deps import API_KEY_HEADER, AppContext, Settings, key_is_valid
from tessera_api.planning import WorldProvider
from tessera_api.routes import audit, forecasts, invoices, plan, proposals

PUBLIC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


def create_app(
    settings: Settings | None = None,
    *,
    world_provider: WorldProvider | None = None,
    llm_factory: Callable[[], LLMClient] | None = None,
    ruleset: Ruleset | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    ctx = AppContext(
        settings=settings,
        session_factory=make_session_factory(engine),
        ruleset=ruleset or load_ruleset(),
        llm_factory=llm_factory or llm_from_env,
        clock=clock or (lambda: datetime.now(UTC)),
        world_provider=world_provider,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Tessera API", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def _authenticate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # First, before routing and before the body is parsed: an unauthenticated caller learns nothing about
        # validation. Only the API docs are public.
        if request.url.path not in PUBLIC_PATHS and not key_is_valid(
            request.headers.get(API_KEY_HEADER), settings.api_key
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid API key"},
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default body returns the rejected value (`input`) and `ctx`. The value can be untrusted text, even
        # a lone surrogate, which cannot be encoded and turns the error response itself into a 500. Send where and
        # why only. (A domain validator's own message may still quote a short repr of the value; that is its text.)
        errors = [
            {
                "type": e["type"],
                "loc": list(e["loc"]),
                "msg": str(e["msg"]).encode("utf-8", "backslashreplace").decode(),
            }
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(OperationalError)
    async def _database_down(_: Request, __: OperationalError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    for router in (invoices.router, forecasts.router, plan.router, proposals.router, audit.router):
        app.include_router(router)
    return app
