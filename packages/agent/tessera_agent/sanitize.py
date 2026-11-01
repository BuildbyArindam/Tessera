"""Untrusted text in, a delimited and screened data block out.

Anything a third party wrote (an invoice note, a payment memo, a customer name, an email body) is
UNTRUSTED. It reaches the model only through this module, in this order:

  1. NORMALISE   NFKC, drop control / zero-width / bidi / tag characters, collapse whitespace
  2. SCREEN      the detector (detect.py) scans each field, then the fields joined together, so a phrase
                 split across two fields is still seen. Scanning happens on the UNCAPPED text: padding
                 a note so the payload sits past the length cap does not hide it.
  3. CAP         each field is cut to a per-source length; the block as a whole has a ceiling too
  4. DELIMIT     `render_block` emits ONE block, labelled as data. The fields are JSON, and `<`, `>`
                 and `&` are escaped inside it, so no text can contain a closing tag. The tag name also
                 carries a hash of the content, so even a guessed tag name could not be pre-written.

Flags become `InputFlag`s (code, source, record id; never the text). Any flag raises the whole planning
run to ESCALATE downstream (policy rule `untrusted_input_gate`). The sanitiser reduces what an attacker
can do with the text; it does not make the text safe, and nothing below depends on it being so.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from tessera_agent.canon import canonical_json, sha256_hex
from tessera_agent.detect import InputFlag, scan_phrases, scan_text
from tessera_agent.schemas import ID_PATTERN, looks_like_account_number
from tessera_agent.textnorm import hidden_char_codes, normalize_text, smuggled_text

__all__ = [
    "SOURCE_CAPS",
    "SanitizedField",
    "ScreeningResult",
    "UntrustedSource",
    "UntrustedText",
    "normalize_text",
    "render_block",
    "screen",
    "screen_model_text",
]

log = logging.getLogger("tessera.agent.sanitize")

UntrustedSource = Literal["invoice_note", "memo", "customer_name", "email_body"]
SOURCE_CAPS: dict[str, int] = {"customer_name": 80, "memo": 300, "invoice_note": 500, "email_body": 2000}
MAX_FIELDS = 50
MAX_RAW_CHARS = 20_000  # per field, before any processing: bounds the work a hostile input can cause
MAX_BLOCK_CHARS = 8_000  # all capped fields together
MAX_JOINED_CHARS = 30_000
BLOCK_TAG = "untrusted-data"
_ID = re.compile(ID_PATTERN)


@dataclass(frozen=True)
class UntrustedText:
    """One third-party string and the record it belongs to. `ref_id` is OUR record id, never free text."""

    source: UntrustedSource
    ref_id: str
    text: str

    def __post_init__(self) -> None:
        if self.source not in SOURCE_CAPS:
            raise ValueError(f"unknown untrusted source {self.source!r}")
        if _ID.fullmatch(self.ref_id) is None or looks_like_account_number(self.ref_id):
            raise ValueError("ref_id must be a record id (letters, digits, '_' or '-'), not free text")
        if not isinstance(self.text, str):
            raise TypeError("untrusted text must be a string")


@dataclass(frozen=True)
class SanitizedField:
    source: str
    ref_id: str
    text: str  # normalised and capped; this is what the model sees
    truncated: bool


@dataclass(frozen=True)
class ScreeningResult:
    fields: tuple[SanitizedField, ...]
    flags: tuple[InputFlag, ...]
    content_sha256: str  # over the fields the model sees, for the audit record

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def codes(self) -> tuple[str, ...]:
        """Distinct flag codes, sorted: what policy is told."""
        return tuple(sorted({f.code for f in self.flags}))


def screen(items: Sequence[UntrustedText]) -> ScreeningResult:
    """Normalise, scan and cap every field. Never raises on hostile text; only on a malformed `UntrustedText`."""
    found: set[tuple[str, str, str]] = set()  # (source, ref_id, code)
    fields: list[SanitizedField] = []
    scanned: list[str] = []
    used = 0
    for index, item in enumerate(items):
        if index >= MAX_FIELDS:
            found.add(("joined", "-", "oversize"))
            break
        raw = item.text
        if len(raw) > MAX_RAW_CHARS:
            raw = raw[:MAX_RAW_CHARS]
            found.add((item.source, item.ref_id, "oversize"))
        clean = normalize_text(raw)
        hidden = smuggled_text(raw)
        to_scan = f"{clean} {normalize_text(hidden)}" if hidden else clean
        scanned.append(to_scan)
        for code in hidden_char_codes(raw) | scan_text(to_scan):
            found.add((item.source, item.ref_id, code))

        text = clean[: SOURCE_CAPS[item.source]].rstrip()
        if used + len(text) > MAX_BLOCK_CHARS:
            found.add(("joined", "-", "oversize"))  # still scanned above; just not shown to the model
            continue
        used += len(text)
        fields.append(SanitizedField(item.source, item.ref_id, text, truncated=len(clean) > len(text)))

    if len(scanned) > 1:  # a phrase can be split across fields, even in the middle of a word
        spaced = " ".join(scanned)[:MAX_JOINED_CHARS]
        seen = {code for _, _, code in found}
        # The join without a separator is searched for phrases only: gluing "...12345" to "67890..." would
        # otherwise invent an account number out of two harmless fields.
        for code in (scan_text(spaced) | scan_phrases("".join(scanned)[:MAX_JOINED_CHARS])) - seen:
            found.add(("joined", "-", code))

    flags = tuple(InputFlag(code=c, source=s, ref_id=r) for s, r, c in sorted(found))
    result = ScreeningResult(
        fields=tuple(fields),
        flags=flags,
        content_sha256=sha256_hex(canonical_json([[f.source, f.ref_id, f.text] for f in fields])),
    )
    if result.flagged:  # codes and counts only: never the text, which could itself be a log injection
        log.warning("untrusted input flagged: codes=%s fields=%d", ",".join(result.codes), len(items))
    return result


def screen_model_text(text: str, proposal_id: str) -> tuple[str, tuple[InputFlag, ...]]:
    """Normalise and scan text the MODEL wrote (a rationale). A fooled model can carry an attacker's
    words, or an urgent instruction aimed at the human approver, out through it."""
    clean = normalize_text(text)
    codes = hidden_char_codes(text) | scan_text(clean)
    return clean, tuple(InputFlag(code=c, source="model_output", ref_id=proposal_id) for c in sorted(codes))


def render_block(result: ScreeningResult) -> str:
    """The delimited data block, or "" when there is nothing untrusted to show."""
    if not result.fields:
        return ""
    rows = [
        {"ref": f.ref_id, "source": f.source, "text": f.text, **({"truncated": True} if f.truncated else {})}
        for f in result.fields
    ]
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")  # no closing tag can exist
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    tag = f"{BLOCK_TAG}-{result.content_sha256[:16]}"
    return f"<{tag}>\n{body}\n</{tag}>"
