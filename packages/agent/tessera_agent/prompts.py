"""Versioned prompt files and their hashes.

A prompt is a file under `prompts/` named `<name>.md`. Its SHA-256 over the raw bytes is recorded with
every planning run, so any decision can be traced to the exact text the model was given. Editing a prompt
changes the hash; a new behaviour gets a new file (planner_v2.md), never a silent edit of v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from tessera_agent.canon import sha256_hex

DEFAULT_PROMPT = "planner_v1"
_NAME = re.compile(r"^[a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class PromptFile:
    name: str
    text: str
    sha256: str


def load_prompt(name: str = DEFAULT_PROMPT) -> PromptFile:
    if _NAME.fullmatch(name) is None:  # the name is never a path
        raise ValueError(f"invalid prompt name {name!r}")
    raw = (resources.files("tessera_agent") / "prompts" / f"{name}.md").read_bytes()
    return PromptFile(name=name, text=raw.decode("utf-8"), sha256=sha256_hex(raw))
