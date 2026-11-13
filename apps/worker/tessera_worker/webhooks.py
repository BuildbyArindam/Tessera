"""The Airwallex webhook endpoint: POST /webhooks/airwallex.

Order of checks, each before the next costs anything: the body is read once, raw, up to a size cap; the signature is
verified over exactly those bytes (401); the timestamp must be fresh (400, which rejects a replayed delivery); only
then is the body parsed (400 if unreadable). The event id is the deduplication key (the table's primary key), so a
repeat delivery is acknowledged and does nothing. A new event is stored, audited as `webhook_received`, and applied to
the execution it is about, all in one transaction: if anything fails the whole delivery rolls back and the vendor's
retry starts clean.

Responses never echo the body, the signature or the secret.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from tessera_airwallex.webhooks import (
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookEvent,
    WebhookFormatError,
    parse_event,
    timestamp_is_fresh,
    verify_signature,
)
from tessera_audit import AuditEventType, AuditLog
from tessera_audit import payloads as audit_payloads
from tessera_store import make_engine, make_session_factory, repo_exec

from tessera_worker.outcomes import apply_event
from tessera_worker.settings import WorkerSettings

log = logging.getLogger("tessera.worker.webhooks")


def _reply(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


async def _read_capped(request: Request) -> bytes | None:
    """The raw body, or None if it is larger than the cap (read no further than the cap)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def store_event(session: Session, event: WebhookEvent, clock: Callable[[], datetime]) -> str:
    """Record one verified, fresh, parsed event. Returns "duplicate" or the outcome it had. Caller commits."""
    is_new = repo_exec.insert_webhook_event_if_new(
        session,
        event_id=event.id,
        name=event.name,
        resource_id=event.resource_id,
        request_id=event.request_id,
        event_created_at=event.created_at,
    )
    if not is_new:
        return "duplicate"
    audit = AuditLog(session, clock)
    outcome, execution_id = apply_event(
        session, audit, name=event.name, resource_id=event.resource_id, request_id=event.request_id, at=clock()
    )
    row = repo_exec.get_webhook_event(session, event.id)
    assert row is not None
    row.outcome, row.execution_id = outcome, execution_id
    audit.append(
        AuditEventType.WEBHOOK_RECEIVED,
        audit_payloads.webhook_received_payload(
            event_id=event.id,
            name=event.name,
            resource_id=event.resource_id,
            request_id=event.request_id,
            event_created_at=event.created_at,
            outcome=outcome,
            execution_id=execution_id,
        ),
    )
    return outcome


def create_webhook_app(
    settings: WorkerSettings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    now = clock or (lambda: datetime.now(UTC))
    factory = session_factory or make_session_factory(make_engine(settings.database_url))
    app = FastAPI(title="Tessera webhooks", docs_url=None, redoc_url=None, openapi_url=None)

    def process(event: WebhookEvent) -> str:
        with factory() as session:
            outcome = store_event(session, event, now)
            session.commit()
            return outcome

    @app.exception_handler(OperationalError)
    async def _database_down(_: Request, __: OperationalError) -> JSONResponse:
        return _reply(503, "database unavailable")  # not a 2xx: the vendor will deliver again

    @app.post("/webhooks/airwallex")
    async def receive(request: Request) -> JSONResponse:
        body = await _read_capped(request)
        if body is None:
            return _reply(413, "body too large")
        timestamp = request.headers.get(TIMESTAMP_HEADER)
        if not verify_signature(settings.webhook_secret, timestamp, request.headers.get(SIGNATURE_HEADER), body):
            return _reply(401, "invalid signature")
        if not timestamp_is_fresh(timestamp, now(), settings.webhook_tolerance):
            return _reply(400, "stale or invalid timestamp")
        try:
            event = parse_event(body)
        except WebhookFormatError:
            return _reply(400, "unreadable event")
        outcome = await run_in_threadpool(process, event)
        return JSONResponse(status_code=200, content={"status": outcome})

    return app


def create_app() -> FastAPI:
    """Factory for uvicorn:  uvicorn --factory tessera_worker.webhooks:create_app"""
    return create_webhook_app(WorkerSettings.from_env())
