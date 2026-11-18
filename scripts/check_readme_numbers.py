"""Check that the numbers in README.md come from the reports, not from memory (Day 20).

    python scripts/check_readme_numbers.py [--readme README.md] [--reports docs/eval-report.md ...]

What it checks
  1. Every "significant" number in the README prose must appear, written the same way, in at least one report.
     Significant means: it has a decimal point, a percent sign, or an absolute value of 100 or more.
     (Integers below 100 are not checked: they occur everywhere, so a match would prove nothing.)
  2. Every interval written as ``0.50 [-0.02, 1.35]`` must appear verbatim in a report. This is the stronger
     check: it ties a point estimate to the interval it was reported with.
  3. A number that is not in a report (a test count, a tool version) must be listed in
     ``scripts/readme_numbers_allow.txt`` with a reason, so every exception is visible in review.

What is ignored: fenced code blocks, inline code, link targets, HTML comments, ISO dates, and
words glued to digits such as ``Python3``. Exit status 1 lists every number that failed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS = ("docs/eval-report.md", "docs/redteam-report.md", "docs/agent-eval-report.md")

NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?(?![\w])")
INTERVAL = re.compile(r"-?\d[\d,]*\.\d+ \[-?\d[\d,]*\.\d+, -?\d[\d,]*\.\d+\]")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def normalise(token: str) -> str:
    """'1,452' -> '1452'. Signs, decimals and percent signs are kept, so '0.5' and '0.50' stay different."""
    return token.replace(",", "")


def is_significant(token: str) -> bool:
    t = normalise(token)
    if "." in t or t.endswith("%"):
        return True
    try:
        return abs(int(t)) >= 100
    except ValueError:
        return False


def prose(markdown: str) -> str:
    """The README with everything that is not a claim in prose removed."""
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"\]\([^)\n]*\)", "]", text)  # keep the link text, drop the target
    text = re.sub(r"https?://\S+", " ", text)
    return ISO_DATE.sub(" ", text)


def load_allowlist(path: Path) -> dict[str, str]:
    allowed: dict[str, str] = {}
    if not path.exists():
        return allowed
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token, sep, reason = (part.strip() for part in line.partition("|"))
        if not sep or not reason:
            raise SystemExit(f"{path}:{lineno}: every allowlisted number needs a reason ('token | reason')")
        allowed[normalise(token)] = reason
    return allowed


def check(readme: str, corpus: str, allowed: dict[str, str]) -> tuple[list[str], dict[str, int]]:
    """Return (problems, counts). Pure, so the tests can call it without files."""
    text = prose(readme)
    corpus_tokens = {normalise(t) for t in NUMBER.findall(corpus)}
    problems: list[str] = []
    counts = {"checked": 0, "allowlisted": 0, "skipped_small": 0, "intervals": 0}

    for claim in sorted(set(INTERVAL.findall(text))):
        counts["intervals"] += 1
        if claim not in corpus and normalise(claim) not in allowed:
            problems.append(f"interval not found verbatim in any report: {claim}")

    for token in sorted(set(NUMBER.findall(text))):
        if not is_significant(token):
            counts["skipped_small"] += 1
            continue
        key = normalise(token)
        if key in corpus_tokens:
            counts["checked"] += 1
        elif key in allowed:
            counts["allowlisted"] += 1
        else:
            problems.append(f"number not found in any report and not allowlisted: {token}")
    return problems, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--reports", nargs="+", default=list(DEFAULT_REPORTS))
    ap.add_argument("--allow", default="scripts/readme_numbers_allow.txt")
    args = ap.parse_args(argv)

    readme_path = ROOT / args.readme
    report_paths = [ROOT / r for r in args.reports]
    missing = [str(p) for p in [readme_path, *report_paths] if not p.exists()]
    if missing:
        print("missing file(s): " + ", ".join(missing), file=sys.stderr)
        return 2
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in report_paths)
    problems, counts = check(readme_path.read_text(encoding="utf-8"), corpus, load_allowlist(ROOT / args.allow))

    print(
        f"README numbers: {counts['checked']} found in reports, {counts['allowlisted']} allowlisted, "
        f"{counts['intervals']} intervals verbatim-checked, {counts['skipped_small']} small integers not checked."
    )
    for p in problems:
        print("  FAIL " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
