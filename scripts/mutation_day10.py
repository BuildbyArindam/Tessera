# ruff: noqa: E501
"""Day 10 mutation check: plant realistic bugs in COPIES of the three packages Day 10 touched and require the
tests to catch each one.

Run: .venv/bin/python scripts/mutation_day10.py
The repo is never touched: tessera_agent, tessera_core and tessera_evalkit are copied to a temp directory that
shadows the installed ones (pytest's own `pythonpath` is overridden, because the repo config would otherwise put
the REAL core and evalkit first). A mutant that survives is a gap in the tests, and the script exits non-zero.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "tessera_agent": ROOT / "packages/agent/tessera_agent",
    "tessera_core": ROOT / "packages/core/tessera_core",
    "tessera_evalkit": ROOT / "packages/evalkit/tessera_evalkit",
}
from mutants_day10 import MUTANTS  # noqa: E402  (sits beside this script)


def run(workdir: Path, tests: str) -> int:
    """pytest exit code: 0 = all passed (mutant survived), 1 = a test failed (killed), other = broken mutant."""
    env = {**os.environ, "PYTHONPATH": str(workdir)}
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *tests.split(),
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "-o",
        f"pythonpath={workdir} tests",
    ]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True).returncode


def main() -> int:
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name, src in PACKAGES.items():
            shutil.copytree(src, work / name, ignore=shutil.ignore_patterns("__pycache__"))
        probe = "import sys; sys.path.insert(0, sys.argv[1]); import tessera_agent, tessera_core, tessera_evalkit as e; print(tessera_agent.__file__, tessera_core.__file__, e.__file__)"
        shadow = subprocess.run(
            [sys.executable, "-c", probe, str(work)], capture_output=True, text=True, cwd=ROOT
        ).stdout.split()
        assert len(shadow) == 3 and all(p.startswith(str(work)) for p in shadow), (
            f"the copies do not shadow the installed packages: {shadow}"
        )
        for tests in sorted({m[4] for m in MUTANTS}):
            assert run(work, tests) == 0, f"baseline (unmutated copy) fails {tests}"
        print(f"baseline passes on the copies ({work})\n")
        for name, rel, old, new, tests in MUTANTS:
            path = work / rel
            original = (
                ROOT
                / "packages"
                / {"tessera_agent": "agent", "tessera_core": "core", "tessera_evalkit": "evalkit"}[rel.split("/")[0]]
                / rel
            ).read_text()
            assert original.count(old) == 1, (
                f"mutation text for {name!r} must match exactly once in {rel} (found {original.count(old)})"
            )
            path.write_text(original.replace(old, new))
            code = run(work, tests)
            path.write_text(original)
            label = {0: "SURVIVED", 1: "killed  "}.get(code, f"BROKEN({code})")  # BROKEN: not a real test failure
            print(f"{label}  {name}")
            if code != 1:
                survivors.append(name)
    print(
        f"\n{len(MUTANTS) - len(survivors)} of {len(MUTANTS)} mutants killed"
        + (f"; SURVIVORS OR BROKEN: {survivors}" if survivors else "")
    )
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
