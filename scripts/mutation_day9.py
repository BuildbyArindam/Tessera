# ruff: noqa: E501
"""Day 9 mutation check: plant realistic bugs in a COPY of tessera_agent and require the tests to catch each.

Run: .venv/bin/python scripts/mutation_day9.py
The repo is never touched: the package is copied to a temp dir that shadows the installed one via PYTHONPATH.
A mutant that survives (all selected tests still pass) is a gap in the tests and the script exits non-zero.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packages/agent/tessera_agent"
S, P, T, L = (
    "tests/unit/test_agent_schemas.py",
    "tests/unit/test_agent_planner.py",
    "tests/unit/test_agent_tools.py",
    "tests/unit/test_agent_llm.py",
)

# (name, file, original text, mutated text, tests that must fail)
MUTANTS: list[tuple[str, str, str, str, str]] = [
    (
        "schema accepts unknown fields",
        "schemas.py",
        'model_config = ConfigDict(extra="forbid", frozen=True)',
        'model_config = ConfigDict(extra="ignore", frozen=True)',
        S,
    ),
    (
        "account-number shape check disabled",
        "schemas.py",
        "    squeezed = value.replace",
        "    return False\n    squeezed = value.replace",
        S,
    ),
    ("digit-run limit loosened to 20", "schemas.py", "[0-9]{9,}", "[0-9]{20,}", S),
    ("amount may have 6 decimals", "schemas.py", r"(\.[0-9]{1,2})?$", r"(\.[0-9]{1,6})?$", S),
    ("proposal cap raised to 50", "schemas.py", "MAX_PROPOSALS = 5", "MAX_PROPOSALS = 50", S),
    (
        "evidence no longer required",
        "schemas.py",
        "Field(min_length=1, max_length=MAX_EVIDENCE)",
        "Field(min_length=0, max_length=MAX_EVIDENCE)",
        S,
    ),
    ("same-currency conversion allowed", "schemas.py", "if self.currency == self.to_currency:", "if False:", S),
    (
        "duplicate JSON keys accepted",
        "canon.py",
        '            raise OutputFormatError("a JSON object repeats a key")',
        "            pass",
        S,
    ),
    ("NaN accepted in model output", "canon.py", "parse_constant=_reject_constant", "parse_constant=float", S),
    ("unknown ids not checked", "validate.py", "if ident not in known_ids.get(kind, frozenset()):", "if False:", P),
    (
        "unknown evidence not checked",
        "validate.py",
        "if any(e not in issued_evidence for e in item.evidence_ids):",
        "if False:",
        P,
    ),
    ("duplicate proposals allowed", "validate.py", "if fingerprint in seen:", "if False:", P),
    ("temperature 0.7", "planner.py", "TEMPERATURE = 0.0", "TEMPERATURE = 0.7", P),
    (
        "retry keeps tools on",
        "planner.py",
        "text = self._call(run, tools_on=False).text",
        "text = self._call(run, tools_on=True).text",
        P,
    ),
    (
        "retry note echoes the model's text",
        "planner.py",
        'listed = "\\n".join(f"- {line}" for line in errors)',
        'listed = "\\n".join(f"- {line}" for line in errors) + str(errors) + "MARKER-XYZ"',
        P,
    ),
    (
        "failed tool results count as evidence",
        "planner.py",
        'evidence_id = None if failed else f"ev-{len(run.evidence) + 1}"',
        'evidence_id = f"ev-{len(run.evidence) + 1}"',
        P,
    ),
    ("tool-call budget removed", "planner.py", "if len(run.traces) >= MAX_TOOL_CALLS:", "if False:", P),
    (
        "provider error escapes as an exception",
        "planner.py",
        "        except LLMError:",
        "        except ZeroDivisionError:",
        P,
    ),
    (
        "proposal ids not deterministic",
        "planner.py",
        'digest = sha256_hex(f"{context_hash}|{index}|{canonical_json(body)}")[:20]',
        'digest = sha256_hex(f"{id(body)}|{index}")[:20]',
        P,
    ),
    (
        "due date ignores the obligation",
        "tools.py",
        "due = max(obligation.due_on, self._snap.as_of)",
        "due = self._snap.as_of",
        T,
    ),
    ("FX range reads future data", "tools.py", "visible = series.up_to(self._snap.as_of)", "visible = series", T),
    (
        "late risk sees other businesses",
        "tools.py",
        'own = frame[frame["business_id"] == self._snap.business_id]',
        "own = frame",
        T,
    ),
    (
        "tool arguments not strict",
        "tools.py",
        'model_config = ConfigDict(extra="forbid", frozen=True)',
        'model_config = ConfigDict(extra="ignore", frozen=True)',
        T,
    ),
    (
        "tool errors echo model input",
        "tools.py",
        '"message": "; ".join(summarize_errors(exc))',
        '"message": str(exc)',
        T,
    ),
    (
        "SDK error message leaks",
        "llm.py",
        'raise LLMError(f"provider call failed: {type(exc).__name__}") from None',
        'raise LLMError(f"provider call failed: {exc}") from exc',
        L,
    ),
    ("temperature dropped from request", "llm.py", '            "temperature": temperature,\n', "", L),
]


def run(workdir: Path, tests: str) -> int:
    """pytest exit code: 0 = all passed (mutant survived), 1 = a test failed (killed), other = broken mutant."""
    env = {**os.environ, "PYTHONPATH": str(workdir)}
    out = subprocess.run(
        [sys.executable, "-m", "pytest", tests, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return out.returncode


def main() -> int:
    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        check = subprocess.run(
            [sys.executable, "-c", "import tessera_agent; print(tessera_agent.__file__)"],
            env={**os.environ, "PYTHONPATH": str(work)},
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        shutil.copytree(SRC, work / "tessera_agent")
        shadow = subprocess.run(
            [sys.executable, "-c", "import tessera_agent; print(tessera_agent.__file__)"],
            env={**os.environ, "PYTHONPATH": str(work)},
            capture_output=True,
            text=True,
            cwd=ROOT,
        ).stdout.strip()
        assert shadow.startswith(str(work)), (
            f"the copy does not shadow the installed package: {shadow} (before copy: {check.stdout.strip()})"
        )
        for tests in sorted({m[4] for m in MUTANTS}):
            assert run(work, tests) == 0, f"baseline (unmutated copy) fails {tests}"
        print(f"baseline passes on the copy ({shadow})\n")
        for name, fname, old, new, tests in MUTANTS:
            path = work / "tessera_agent" / fname
            original = (SRC / fname).read_text()
            assert original.count(old) == 1, (
                f"mutation text for {name!r} must match exactly once in {fname} (found {original.count(old)})"
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
