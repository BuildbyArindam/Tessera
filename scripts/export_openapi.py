"""Write the FastAPI OpenAPI schema to a file (default apps/web/openapi.json) for the web client generator (Day 15).

    python scripts/export_openapi.py [--out apps/web/openapi.json] [--check]

`--check` exits 1 if the file on disk differs from the live schema, so CI can fail on a stale generated client.
It needs no database: the app is built with a dummy URL and the engine never connects.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path[:0] = [f"packages/{p}" for p in ("core", "store", "audit", "agent", "airwallex")] + ["apps/api"]

from tessera_api.deps import Settings  # noqa: E402
from tessera_api.main import create_app  # noqa: E402


def render() -> str:
    app = create_app(Settings(database_url="postgresql+psycopg://x:x@localhost/x", api_key="export-only-not-a-secret"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("apps/web/openapi.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not args.out.exists() or args.out.read_text() != text:
            print(f"{args.out} is stale: run `make web-client`", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
