"""Worker settings from the environment. Nothing is read at import time and no secret is ever logged."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import SecretStr
from tessera_airwallex.webhooks import DEFAULT_TOLERANCE

DEFAULT_RESUME_MAX_AGE = timedelta(
    hours=24
)  # inside the 7-day window in which the vendor treats a request_id as a duplicate


class SettingsError(RuntimeError):
    """The environment does not give a safe configuration. Messages never include a secret."""


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    webhook_secret: SecretStr = field(repr=False)
    webhook_tolerance: timedelta = DEFAULT_TOLERANCE
    resume_max_age: timedelta = DEFAULT_RESUME_MAX_AGE
    poll_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.database_url:
            raise SettingsError("DATABASE_URL must be set")
        if len(self.webhook_secret.get_secret_value()) < 8:
            raise SettingsError("AIRWALLEX_WEBHOOK_SECRET must be set (at least 8 characters)")
        if self.poll_seconds <= 0:
            raise SettingsError("TESSERA_WORKER_POLL_SECONDS must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        source = os.environ if env is None else env
        try:
            poll = float(source.get("TESSERA_WORKER_POLL_SECONDS", "30"))
        except ValueError:
            raise SettingsError("TESSERA_WORKER_POLL_SECONDS must be a number") from None
        return cls(
            database_url=source.get("DATABASE_URL", ""),
            webhook_secret=SecretStr(source.get("AIRWALLEX_WEBHOOK_SECRET", "")),
            poll_seconds=poll,
        )
