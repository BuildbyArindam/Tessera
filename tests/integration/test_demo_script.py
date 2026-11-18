"""Day 19: `make demo` keeps working. The script asserts every step itself and exits nonzero if the story breaks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from it_helpers import ROOT

ADMIN = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://tessera:tessera@localhost:5433/postgres")


def test_the_demo_runs_to_the_end_and_tells_the_whole_story(tmp_path: Path) -> None:
    out = tmp_path / "transcript.txt"
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo.py"), "--out", str(out)],
        env={**os.environ, "TEST_DATABASE_ADMIN_URL": ADMIN},
        capture_output=True, text=True, timeout=240, check=False,
    )  # fmt: skip
    if run.returncode == 2 and "cannot reach PostgreSQL" in run.stderr:
        if os.environ.get("TESSERA_REQUIRE_DB") == "1":
            pytest.fail("PostgreSQL is not reachable and TESSERA_REQUIRE_DB=1")
        pytest.skip("PostgreSQL is not reachable")
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    text = out.read_text()
    for needle in (
        "Policy verdict: ESCALATE by forecast_confidence_gate",
        "Status: SUBMITTED",
        "applied",
        "duplicate",
        "Chain OK: True",
        "DONE.",
    ):
        assert needle in text, needle
    assert "!!" not in text
