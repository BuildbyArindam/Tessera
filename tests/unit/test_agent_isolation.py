"""Day 9: the agent package has no way to act. Enforced by reading its source, not by trusting it.

This is the test version of the dependency rule (agent may read core types, may never import airwallex).
Day 13 adds import-linter contracts; this stays as a second, independent check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[2] / "packages/agent/tessera_agent"

FORBIDDEN_MODULES = {
    "tessera_airwallex", "tessera_audit", "tessera_api", "tessera_worker",  # execution, audit writes, apps
    "sqlalchemy", "psycopg", "psycopg2", "alembic",  # databases
    "fastapi", "starlette", "uvicorn",  # servers
    "httpx", "requests", "urllib", "urllib3", "aiohttp", "http", "socket", "ssl",  # network
    "subprocess", "multiprocessing", "ctypes", "pickle", "marshal", "shelve",  # side doors
}  # fmt: skip
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open"}


def violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        found += [f"import {n}" for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"call {node.func.id}()")
    return found


def agent_files() -> list[Path]:
    return sorted(AGENT_DIR.glob("*.py"))


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.name)
def test_no_agent_module_imports_execution_database_network_or_dynamic_code(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8")) == []


def test_the_scan_can_fail() -> None:
    """Planting each kind of violation must be reported, or the test above proves nothing."""
    planted = "import tessera_airwallex\nfrom tessera_airwallex.mock import MockAirwallex\nimport httpx\neval('1')\n"
    assert violations(planted) == [
        "import tessera_airwallex",
        "import tessera_airwallex.mock",
        "import httpx",
        "call eval()",
    ]
    assert violations("import json\nfrom tessera_core.domain import Money\nimport anthropic\n") == []


def test_the_provider_sdk_is_imported_lazily_inside_a_function() -> None:
    tree = ast.parse((AGENT_DIR / "llm.py").read_text(encoding="utf-8"))
    top_level = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    assert all("anthropic" not in (getattr(n, "module", None) or "") for n in top_level)
    assert not any(a.name == "anthropic" for n in top_level if isinstance(n, ast.Import) for a in n.names)


def test_agent_files_stay_under_300_lines_and_ship_the_prompt() -> None:
    assert all(len(p.read_text().splitlines()) < 300 for p in agent_files())
    assert (AGENT_DIR / "prompts/planner_v1.md").is_file()
    pyproject = (AGENT_DIR.parent / "pyproject.toml").read_text()
    assert "prompts/*.md" in pyproject  # a non-editable install must include the prompt
