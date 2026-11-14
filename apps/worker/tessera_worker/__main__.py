"""Run the worker loop:  python -m tessera_worker

Needs DATABASE_URL and AIRWALLEX_WEBHOOK_SECRET (the webhook app shares the settings). TESSERA_AIRWALLEX_ADAPTER picks
`mock` (the default) or `sandbox`. Serve the webhook endpoint separately:
    uvicorn --factory tessera_worker.webhooks:create_app --port 8001
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import UTC, datetime

from tessera_agent import llm_from_env
from tessera_airwallex import AirwallexPort, MockAirwallex, SandboxAirwallex
from tessera_core.policy import load_ruleset
from tessera_store import make_engine, make_session_factory

from tessera_worker.dispatch import CardRef
from tessera_worker.loop import LoopDeps, run_forever
from tessera_worker.settings import DEFAULT_RESUME_MAX_AGE, WorkerSettings
from tessera_worker.submit import ExecDeps

log = logging.getLogger("tessera.worker")


class EmptyDirectory:
    """TODO(Day 14): back this with the onboarding records. Until then no beneficiary or card maps to a vendor id, so
    PAY_SUPPLIER and existing-card TOPUP_CARD proposals FAIL safely instead of guessing."""

    def beneficiary(self, business_id: str, beneficiary_id: str) -> str | None:
        return None

    def card(self, business_id: str, vendor_id: str) -> CardRef | None:
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = WorkerSettings.from_env()
    port: AirwallexPort = (
        SandboxAirwallex() if os.environ.get("TESSERA_AIRWALLEX_ADAPTER") == "sandbox" else MockAirwallex()
    )
    log.warning("no world provider is configured: the worker will not plan or execute until one is (Day 14)")
    log.warning("no autonomy inputs provider is configured: the dial will not move on its own (it stays at its level)")
    deps = LoopDeps(
        exec=ExecDeps(
            session_factory=make_session_factory(make_engine(settings.database_url)),
            port=port,
            directory=EmptyDirectory(),
            world_provider=None,
            ruleset=load_ruleset(),
            clock=lambda: datetime.now(UTC),
            max_age=DEFAULT_RESUME_MAX_AGE,
        ),
        llm_factory=llm_from_env,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_forever(deps, settings.poll_seconds, stop)


if __name__ == "__main__":
    main()
