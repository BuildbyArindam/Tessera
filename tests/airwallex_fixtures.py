"""Load the response fixtures in tests/fixtures/airwallex (doc-derived until recorded; see the README there)."""

from pathlib import Path
from typing import Any

from tessera_airwallex import _json

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "airwallex"


def load_fixture(name: str) -> Any:
    """Parsed fixture with numbers as Decimal, exactly as the client sees them."""
    return _json.loads((FIXTURE_DIR / f"{name}.json").read_text())
