"""SandboxHttp: auth, token cache, retries, error mapping, redaction. No network (respx)."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from tessera_airwallex import (
    AuthError,
    InsufficientFundsError,
    InvalidRequestError,
    NotFoundError,
    QuoteExpiredError,
    RateLimitedError,
    UnknownError,
)
from tessera_airwallex._errormap import map_error
from tessera_airwallex._http import SandboxConfig, SandboxHttp
from tessera_airwallex._redact import REDACTED, redact, scrub

BASE = "https://api.sandbox.airwallex.com"
LOGIN, BALANCES = f"{BASE}/api/v1/authentication/login", f"{BASE}/api/v1/balances/current"
CLIENT_ID, API_KEY, TOKEN = "cid-SECRET-111", "key-SECRET-222", "tok-FIXTURE-0000"
START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # the login fixture expires at 10:30


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def config(**kw: object) -> SandboxConfig:
    return SandboxConfig(base_url=BASE, client_id=SecretStr(CLIENT_ID), api_key=SecretStr(API_KEY), **kw)  # type: ignore[arg-type]


def ok(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class Rig:
    def __init__(self, router: respx.MockRouter, **cfg: object) -> None:
        self.router = router
        self.clock, self.sleeps = Clock(), []
        self.login = router.post(LOGIN).mock(return_value=ok(load_fixture_plain("login"), 201))
        self.balances = router.get(BALANCES)
        self.http = SandboxHttp(config(**cfg), clock=self.clock, sleep=self.sleeps.append, jitter=lambda: 1.0)

    def get(self) -> object:
        return self.http.request("GET", "/api/v1/balances/current")


def load_fixture_plain(name: str) -> object:
    import json

    from airwallex_fixtures import FIXTURE_DIR

    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


@pytest.fixture
def rig() -> Iterator[Rig]:
    with respx.mock(assert_all_called=False) as router:
        yield Rig(router)


@pytest.fixture
def fast_rig() -> Iterator[Rig]:
    with respx.mock(assert_all_called=False) as router:
        yield Rig(router, backoff_cap=1.5)


# ---------------------------------------------------------------------------- auth
class TestAuth:
    def test_login_sends_credentials_and_the_token_is_cached(self, rig: Rig) -> None:
        rig.balances.mock(return_value=ok([]))
        rig.get()
        rig.get()
        assert rig.login.call_count == 1
        login = rig.login.calls[0].request
        assert login.headers["x-client-id"] == CLIENT_ID and login.headers["x-api-key"] == API_KEY
        assert "x-login-as" not in login.headers and "x-api-version" not in login.headers
        assert rig.balances.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    def test_optional_headers_only_when_configured(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            rig = Rig(router, login_as="acct_1", api_version="2026-08-21")
            rig.balances.mock(return_value=ok([]))
            rig.get()
            for call in (rig.login.calls[0], rig.balances.calls[0]):
                assert call.request.headers["x-api-version"] == "2026-08-21"
            assert rig.login.calls[0].request.headers["x-login-as"] == "acct_1"

    def test_token_is_refreshed_before_it_expires(self, rig: Rig) -> None:
        rig.balances.mock(return_value=ok([]))
        rig.get()
        rig.clock.now = START + timedelta(minutes=27)  # outside the 2 minute margin: still cached
        rig.get()
        assert rig.login.call_count == 1
        rig.clock.now = START + timedelta(minutes=29)  # inside the margin: refresh
        rig.get()
        assert rig.login.call_count == 2

    def test_a_401_is_not_retried_and_clears_the_token(self, rig: Rig) -> None:
        rig.balances.mock(side_effect=[httpx.Response(401, json={"code": "credentials_expired"}), ok([])])
        with pytest.raises(AuthError) as err:
            rig.get()
        assert rig.balances.call_count == 1 and rig.sleeps == []
        assert err.value.status == 401 and err.value.code == "credentials_expired"
        rig.get()  # the next call logs in again
        assert rig.login.call_count == 2

    def test_a_failed_login_is_an_auth_error_and_nothing_else_is_called(self, rig: Rig) -> None:
        rig.login.mock(return_value=httpx.Response(401, json={"code": "credentials_invalid"}))
        with pytest.raises(AuthError):
            rig.get()
        assert rig.login.call_count == 1 and rig.balances.call_count == 0

    def test_login_response_without_a_token_is_unknown(self, rig: Rig) -> None:
        rig.login.mock(return_value=ok({"expires_at": "2026-11-02T10:30:00+0000"}, 201))
        with pytest.raises(UnknownError):
            rig.get()


# ---------------------------------------------------------------------------- retries
class TestRetries:
    def test_a_503_is_retried_with_exponential_backoff(self, rig: Rig) -> None:
        rig.balances.mock(side_effect=[httpx.Response(503), httpx.Response(503), ok([])])
        assert rig.get() == []
        assert rig.balances.call_count == 3
        assert rig.sleeps == [0.5, 1.0]

    def test_retries_are_bounded(self, rig: Rig) -> None:
        rig.balances.mock(return_value=httpx.Response(503))
        with pytest.raises(UnknownError) as err:
            rig.get()
        assert rig.balances.call_count == 4  # first try + 3 retries
        assert rig.sleeps == [0.5, 1.0, 2.0] and err.value.status == 503

    def test_backoff_is_capped(self, fast_rig: Rig) -> None:
        fast_rig.balances.mock(return_value=httpx.Response(500))
        with pytest.raises(UnknownError):
            fast_rig.get()
        assert fast_rig.sleeps == [0.5, 1.0, 1.5]

    def test_jitter_scales_the_delay_down_at_most_by_half(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            rig = Rig(router)
            rig.http._jitter = lambda: 0.0  # type: ignore[assignment]
            rig.balances.mock(side_effect=[httpx.Response(503), ok([])])
            rig.get()
            assert rig.sleeps == [0.25]

    def test_a_network_error_is_retried(self, rig: Rig) -> None:
        rig.balances.mock(side_effect=[httpx.ConnectError("boom"), ok([])])
        assert rig.get() == []
        assert rig.balances.call_count == 2

    def test_a_persistent_timeout_is_an_unknown_error(self, rig: Rig) -> None:
        rig.balances.mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(UnknownError):
            rig.get()
        assert rig.balances.call_count == 4

    @pytest.mark.parametrize("status", [400, 404, 409, 422, 429])
    def test_a_4xx_is_never_retried(self, rig: Rig, status: int) -> None:
        rig.balances.mock(return_value=httpx.Response(status, json={"code": "x"}))
        with pytest.raises(Exception):  # noqa: B017 - the class is checked in TestErrorMapping
            rig.get()
        assert rig.balances.call_count == 1 and rig.sleeps == []

    def test_a_429_carries_retry_after(self, rig: Rig) -> None:
        rig.balances.mock(
            return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={"code": "too_many_requests"})
        )
        with pytest.raises(RateLimitedError) as err:
            rig.get()
        assert err.value.retry_after == 7.0 and rig.balances.call_count == 1

    def test_non_json_success_body_is_unknown(self, rig: Rig) -> None:
        rig.balances.mock(return_value=httpx.Response(200, content=b"<html>"))
        with pytest.raises(UnknownError):
            rig.get()


# ---------------------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "expired_quote", QuoteExpiredError),
        (400, "insufficient_funds", InsufficientFundsError),
        (400, "credit_limit_exceeded", InsufficientFundsError),
        (400, "invalid_quote_id", NotFoundError),
        (404, "not_found", NotFoundError),
        (400, "amount_below_limit", InvalidRequestError),
        (400, "field_required", InvalidRequestError),
        (400, None, InvalidRequestError),
        (409, "whatever", InvalidRequestError),
        (401, "credentials_invalid", AuthError),
        (403, "forbidden", AuthError),
        (429, "too_many_requests", RateLimitedError),
        (500, "service_unavailable", UnknownError),
        (418, None, UnknownError),
    ],
)
def test_error_mapping(status: int, code: str | None, expected: type[Exception]) -> None:
    body = {"code": code, "message": "m"} if code else {}
    err = map_error(httpx.Response(status, json=body), what="GET /x")
    assert type(err) is expected
    assert err.status == status and err.code == code


def test_a_body_that_is_not_json_still_maps_by_status() -> None:
    err = map_error(httpx.Response(400, content=b"nope"), what="GET /x")
    assert isinstance(err, InvalidRequestError) and err.code is None


# ---------------------------------------------------------------------------- config
class TestConfig:
    def test_from_env_names_what_is_missing_but_never_values(self) -> None:
        with pytest.raises(ValueError) as err:
            SandboxConfig.from_env({"AIRWALLEX_BASE_URL": BASE, "AIRWALLEX_API_KEY": API_KEY})
        assert "AIRWALLEX_CLIENT_ID" in str(err.value) and API_KEY not in str(err.value)

    def test_from_env_reads_everything(self) -> None:
        cfg = SandboxConfig.from_env(
            {
                "AIRWALLEX_BASE_URL": BASE + "/",
                "AIRWALLEX_CLIENT_ID": CLIENT_ID,
                "AIRWALLEX_API_KEY": API_KEY,
                "AIRWALLEX_LOGIN_AS": "acct_9",
                "AIRWALLEX_API_VERSION": "",
            }
        )
        assert cfg.base_url == BASE and cfg.login_as == "acct_9" and cfg.api_version is None

    def test_plain_http_is_refused(self) -> None:
        with pytest.raises(ValueError, match="https"):
            SandboxConfig(base_url="http://api.sandbox.airwallex.com", client_id=SecretStr("a"), api_key=SecretStr("b"))

    def test_repr_hides_credentials(self) -> None:
        text = repr(config())
        assert CLIENT_ID not in text and API_KEY not in text


# ---------------------------------------------------------------------------- redaction
class TestRedaction:
    def test_sensitive_keys_are_blanked_at_any_depth(self) -> None:
        data = {"a": {"account_number": "123456789", "iban": "GB00", "ok": 1}, "list": [{"x-api-key": "k"}]}
        out = redact(data)
        assert out == {"a": {"account_number": REDACTED, "iban": REDACTED, "ok": 1}, "list": [{"x-api-key": REDACTED}]}

    def test_known_secrets_and_bearer_tokens_are_scrubbed_from_strings(self) -> None:
        assert "SECRET" not in scrub("key=SECRET and Bearer abc.def-123", ["SECRET"])

    def test_logs_and_errors_never_contain_credentials(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="tessera_airwallex.sandbox")
        rig.balances.mock(
            side_effect=[
                ok([{"currency": "USD", "token": TOKEN}]),
                httpx.Response(
                    400, json={"code": "invalid_argument", "message": f"bad key {API_KEY} id {CLIENT_ID} token {TOKEN}"}
                ),
            ]
        )
        rig.get()
        with pytest.raises(InvalidRequestError) as err:
            rig.get()
        assert "/api/v1/balances/current" in caplog.text  # logging really happened
        for secret in (CLIENT_ID, API_KEY, TOKEN):
            assert secret not in caplog.text
            assert secret not in str(err.value)

    def test_request_bodies_are_redacted_in_debug_logs(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="tessera_airwallex.sandbox")
        route = rig.router.post(f"{BASE}/api/v1/echo").mock(return_value=ok({}))
        rig.http.request("POST", "/api/v1/echo", body={"account_number": "99887766", "amount": "1.00"})
        assert route.called
        assert "99887766" not in caplog.text and "1.00" in caplog.text
