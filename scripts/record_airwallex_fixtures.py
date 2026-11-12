"""Record REAL sandbox responses as redacted fixtures, so CI can replay them without network.

    AIRWALLEX_BASE_URL=https://api.sandbox.airwallex.com \\
    AIRWALLEX_CLIENT_ID=... AIRWALLEX_API_KEY=... python scripts/record_airwallex_fixtures.py

Writes tests/fixtures/airwallex/recorded/*.json. It makes read-only calls plus one FX quote (a
quote reserves nothing). With AIRWALLEX_LIVE_EXECUTE=1 it also executes a 1 USD -> EUR
conversion in the sandbox and records the create and list responses.

Ids and anything in tessera_airwallex._redact's sensitive-key list are replaced by placeholders.
Read each file before committing it. Compare with the doc-derived fixtures one level up, fix any
field the real API names differently, and update docs/airwallex-notes.md.

NOT YET RUN against the real sandbox: written on Day 12 without credentials.
"""

import os
import sys
import uuid
from pathlib import Path
from typing import Any

from tessera_airwallex import SandboxConfig, _json
from tessera_airwallex._http import SandboxHttp
from tessera_airwallex._redact import redact
from tessera_airwallex.sandbox import (
    BALANCES_PATH,
    CONVERSION_CREATE_PATH,
    CONVERSIONS_PATH,
    QUOTE_CREATE_PATH,
    QUOTE_PATH,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "airwallex" / "recorded"


def scrub_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (f"redacted-{k}" if k.endswith("_id") and isinstance(v, str) else scrub_ids(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_ids(v) for v in value]
    return value


def save(name: str, data: Any) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(_json.dumps(scrub_ids(redact(data))) + "\n")
    print(f"recorded {name}")


def main() -> int:
    try:
        config = SandboxConfig.from_env()
    except ValueError as exc:
        print(f"cannot record: {exc}", file=sys.stderr)
        return 2
    http = SandboxHttp(config)
    save("balances_current", http.request("GET", BALANCES_PATH, params={"account_type": "cash"}))
    quote = http.request(
        "POST",
        QUOTE_CREATE_PATH,
        body={"buy_currency": "EUR", "sell_currency": "USD", "sell_amount": _json.loads("1"), "validity": "MIN_1"},
    )
    save("quote_create", quote)
    save("quote_retrieve", http.request("GET", QUOTE_PATH.format(quote_id=quote["quote_id"])))
    if os.environ.get("AIRWALLEX_LIVE_EXECUTE") == "1":
        request_id = f"record-{uuid.uuid4().hex[:16]}"
        body = {
            "buy_currency": "EUR",
            "sell_currency": "USD",
            "sell_amount": "1",
            "quote_id": quote["quote_id"],
            "request_id": request_id,
        }
        save("conversion_create", http.request("POST", CONVERSION_CREATE_PATH, body=body))
        save("conversions_list", http.request("GET", CONVERSIONS_PATH, params={"request_id": request_id}))
        # A repeated request_id: record how the vendor rejects it (the notes list this as UNVERIFIED).
        try:
            http.request("POST", CONVERSION_CREATE_PATH, body=body)
        except Exception as exc:  # noqa: BLE001 - recording the failure shape is the point
            print(f"duplicate request_id was rejected: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
