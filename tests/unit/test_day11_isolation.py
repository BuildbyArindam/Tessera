"""Day 11: the new packages respect the dependency rule. (import-linter takes over on Day 13.)

core imports nothing else; the agent cannot reach Airwallex; only the worker may. The API, the audit log and the store
must not import Airwallex, and the audit log and store must not import the agent or the API.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "core": ROOT / "packages/core/tessera_core",
    "store": ROOT / "packages/store/tessera_store",
    "audit": ROOT / "packages/audit/tessera_audit",
    "api": ROOT / "apps/api/tessera_api",
}


def imported_modules(directory: Path) -> set[str]:
    found: set[str] = set()
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module)
    return found


def touches(modules: set[str], *roots: str) -> set[str]:
    return {mod for mod in modules if mod.split(".")[0] in roots}


@pytest.mark.parametrize("name", ["store", "audit", "api"])
def test_nothing_new_imports_airwallex(name: str) -> None:
    assert touches(imported_modules(PACKAGES[name]), "tessera_airwallex") == set()


@pytest.mark.parametrize("name", ["store", "audit"])
def test_store_and_audit_do_not_import_the_agent_or_the_api(name: str) -> None:
    assert touches(imported_modules(PACKAGES[name]), "tessera_agent", "tessera_api", "fastapi") == set()


def test_store_does_not_import_audit() -> None:
    assert touches(imported_modules(PACKAGES["store"]), "tessera_audit") == set()


def test_core_stays_pure() -> None:
    forbidden = ("tessera_store", "tessera_audit", "tessera_api", "sqlalchemy", "fastapi", "psycopg", "httpx")
    assert touches(imported_modules(PACKAGES["core"]), *forbidden) == set()


def test_the_scan_can_fail(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("from tessera_airwallex import port\nimport tessera_airwallex.mock\n")
    assert touches(imported_modules(tmp_path), "tessera_airwallex") == {"tessera_airwallex", "tessera_airwallex.mock"}
