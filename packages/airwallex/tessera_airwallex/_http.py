"""HTTP layer for the Airwallex sandbox client.

- Token: fetched with POST /api/v1/authentication/login, cached, refreshed before it expires.
  A 401 is never retried; it invalidates the cached token so the next call logs in again.
- Retries: network errors and 5xx only, bounded, exponential backoff with jitter. Never 4xx.
  Re-sending a conversion is safe because the vendor rejects a repeated request_id.
- Logging: method, path, status, attempt and duration at INFO. Bodies only at DEBUG, redacted.
  Headers are never logged. Error messages never contain bodies or credentials.
"""

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from tessera_airwallex import _json
from tessera_airwallex._errormap import map_error
from tessera_airwallex._redact import redact, scrub
from tessera_airwallex._timeparse import parse_timestamp
from tessera_airwallex.errors import AirwallexError, AuthError, UnknownError

log = logging.getLogger("tessera_airwallex.sandbox")

LOGIN_PATH = "/api/v1/authentication/login"
_MAX_BODY_LOG = 2000


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(15.0, connect=5.0)


@dataclass(frozen=True)
class SandboxConfig:
    base_url: str
    client_id: SecretStr
    api_key: SecretStr
    login_as: str | None = None  # x-login-as, only if the key covers several accounts
    api_version: str | None = None  # x-api-version; UNVERIFIED header, sent only when set
    timeout: httpx.Timeout = field(default_factory=_default_timeout)
    max_retries: int = 3  # retries after the first attempt
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    token_refresh_margin: timedelta = timedelta(minutes=2)

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("AIRWALLEX_BASE_URL must start with https://")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SandboxConfig":
        import os

        source = env if env is not None else os.environ
        missing = [n for n in ("AIRWALLEX_BASE_URL", "AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY") if not source.get(n)]
        if missing:
            raise ValueError("missing environment variables: " + ", ".join(missing))
        return cls(
            base_url=source["AIRWALLEX_BASE_URL"].rstrip("/"),
            client_id=SecretStr(source["AIRWALLEX_CLIENT_ID"]),
            api_key=SecretStr(source["AIRWALLEX_API_KEY"]),
            login_as=source.get("AIRWALLEX_LOGIN_AS") or None,
            api_version=source.get("AIRWALLEX_API_VERSION") or None,
        )


class SandboxHttp:
    def __init__(
        self,
        config: SandboxConfig,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._cfg = config
        self._client = client or httpx.Client(timeout=config.timeout)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._jitter = jitter or random.random
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expiry: datetime | None = None

    # ------------------------------------------------------------------ public
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        """Authenticated JSON call. Returns the parsed body (floats as Decimal) or raises a port error.

        `retry=False` makes exactly one attempt: for a create call that has no request_id the vendor cannot
        deduplicate, so repeating it after a lost response could create a second record. The caller then
        reconciles (looks the record up) instead.
        """
        headers = {"Authorization": f"Bearer {self._bearer()}", **self._common_headers()}
        content = _json.dumps(body).encode() if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
            self._debug("request", path, content.decode())
        response = self._send(method, path, headers=headers, params=params, content=content, retry=retry)
        if response.status_code == 401:
            self._invalidate_token()
        return self._parse(response, method, path)

    # ------------------------------------------------------------------ auth
    def _common_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._cfg.api_version:
            headers["x-api-version"] = self._cfg.api_version
        return headers

    def _bearer(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is None or self._expiry is None or now >= self._expiry - self._cfg.token_refresh_margin:
                self._login()
            assert self._token is not None
            return self._token

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token, self._expiry = None, None

    def _login(self) -> None:
        headers = {
            "x-client-id": self._cfg.client_id.get_secret_value(),
            "x-api-key": self._cfg.api_key.get_secret_value(),
            **self._common_headers(),
        }
        if self._cfg.login_as:
            headers["x-login-as"] = self._cfg.login_as
        response = self._send("POST", LOGIN_PATH, headers=headers)
        data = self._parse(response, "POST", LOGIN_PATH)
        try:
            token, expiry = str(data["token"]), parse_timestamp(data["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UnknownError("login response missing token or expires_at") from exc
        if not token:
            raise UnknownError("login response held an empty token")
        self._token, self._expiry = token, expiry

    # ------------------------------------------------------------------ transport
    def _secrets(self) -> list[str]:
        found = [self._cfg.client_id.get_secret_value(), self._cfg.api_key.get_secret_value()]
        if self._token:
            found.append(self._token)
        return found

    def _delay(self, attempt: int) -> float:
        base = min(self._cfg.backoff_cap, self._cfg.backoff_base * (2.0 ** (attempt - 1)))
        return base * (0.5 + self._jitter() / 2)

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int] | None = None,
        content: bytes | None = None,
        retry: bool = True,
    ) -> httpx.Response:
        url = self._cfg.base_url + path
        attempts = self._cfg.max_retries + 1 if retry else 1
        reason, status = "", None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.request(method, url, headers=headers, params=params, content=content)
            except httpx.TransportError as exc:
                reason, status = type(exc).__name__, None
                self._log(method, path, "error", attempt, started)
            else:
                self._log(method, path, response.status_code, attempt, started)
                if response.status_code < 500:
                    return response
                reason, status = f"HTTP {response.status_code}", response.status_code
            if attempt < attempts:
                self._sleep(self._delay(attempt))
        raise UnknownError(f"{method} {path} failed after {attempts} attempts ({reason})", status=status)

    def _parse(self, response: httpx.Response, method: str, path: str) -> Any:
        self._debug("response", path, response.text)
        if not 200 <= response.status_code < 300:
            raise map_error(response, what=f"{method} {path}", secrets=self._secrets())
        if not response.content:
            return None
        try:
            return _json.loads(response.text)
        except ValueError as exc:
            raise UnknownError(f"{method} {path}: response was not valid JSON", status=response.status_code) from exc

    # ------------------------------------------------------------------ logging
    def _log(self, method: str, path: str, status: object, attempt: int, started: float) -> None:
        log.info(
            "airwallex %s %s -> %s (attempt %d, %.0f ms)",
            method,
            path,
            status,
            attempt,
            (time.monotonic() - started) * 1000,
        )

    def _debug(self, label: str, path: str, raw: str) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        secrets = self._secrets()
        try:
            shown = _json.dumps(redact(_json.loads(raw), secrets))
        except (ValueError, TypeError):
            shown = scrub(raw, secrets)
        log.debug("airwallex %s %s %s", label, path, shown[:_MAX_BODY_LOG])


__all__ = ["AirwallexError", "AuthError", "SandboxConfig", "SandboxHttp"]
