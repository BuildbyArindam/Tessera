# Numbers in README.md that are not in a report under docs/, each with the reason it is allowed.
# Format: token | reason. A number belongs here only if it is not a result: a count of tests, a version, a port.
# Results (accuracy, coverage, savings, attack counts) must come from docs/eval-report.md, docs/redteam-report.md
# or docs/agent-eval-report.md and never go on this list.
3.11 | Python version required by the packages (pyproject files)
1228 | unit and contract tests passed by `make test` on 2026-11-18 (pytest output, not a report)
219 | integration tests passed by `make test-integration` on 2026-11-18 (pytest output, not a report)
