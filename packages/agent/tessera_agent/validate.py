"""Turn model text into validated proposals, or into a safe list of what was wrong.

Four checks, in order, and the first that fails ends the attempt:
  1. FORMAT   one JSON object, no duplicate keys, no NaN (canon.parse_strict_json)
  2. SCHEMA   the strict typed shape (schemas.ProposalList)
  3. REFERENCES every id the proposal points at exists in the snapshot the planner was given, and
               every evidence id was actually issued by a tool in THIS run
  4. DUPLICATES the same action with the same params twice is rejected

The returned error lines are what the model sees on its one retry. They contain field paths and
error codes only, never a value the model wrote: model output is untrusted text and is not echoed back.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from tessera_agent.canon import OutputFormatError, canonical_json, parse_strict_json
from tessera_agent.schemas import ProposalList, ProposedAction, references, summarize_errors


def validate_reply(
    text: str,
    known_ids: Mapping[str, frozenset[str]],
    issued_evidence: frozenset[str],
) -> tuple[tuple[ProposedAction, ...], list[str]]:
    """(proposals, []) when valid; ((), error lines) otherwise."""
    try:
        parsed = parse_strict_json(text)
    except OutputFormatError as exc:
        return (), [f"format: {exc}"]
    try:
        reply = ProposalList.model_validate(parsed)
    except ValidationError as exc:
        return (), summarize_errors(exc)

    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(reply.proposals):
        for kind, ident in references(item):
            if ident not in known_ids.get(kind, frozenset()):
                errors.append(f"proposals.{index}.params.{kind}_id: unknown_id")
        if any(e not in issued_evidence for e in item.evidence_ids):
            errors.append(f"proposals.{index}.evidence_ids: unknown_evidence_id")
        fingerprint = canonical_json([item.action_type, item.params.model_dump(mode="json")])
        if fingerprint in seen:
            errors.append(f"proposals.{index}: duplicate_proposal")
        seen.add(fingerprint)
    if errors:
        return (), errors
    return tuple(reply.proposals), []
