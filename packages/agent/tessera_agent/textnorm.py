"""Text normalisation for untrusted strings: one canonical, visible, single-line form.

Why each step exists:
  * NFKC folds compatibility look-alikes (fullwidth letters, ligatures, circled digits) to their plain
    forms, so "ｉｇｎｏｒｅ" and "ignore" reach the detector and the model as the same text.
  * Control (Cc), format (Cf: zero-width and bidi marks, soft hyphen, BOM, Unicode tag characters),
    surrogate (Cs) and private-use (Co) code points are removed. They are invisible to a human reviewer
    but not to a model, which is exactly why attackers like them.
  * Every kind of whitespace, including line and paragraph separators, collapses to one space. A text
    cannot start a "new line" that looks like a system message, and a reviewer sees what the model sees.

Unassigned code points (Cn) are deliberately NOT stripped: which code points are assigned depends on the
Unicode version of the running Python, and normalisation must give the same bytes on every machine.

`hidden_char_codes` reports what was hidden BEFORE it is stripped, because after normalisation the
evidence is gone. It is conservative about legitimate text: zero-width joiners are normal in Indic
scripts and emoji, so they raise a flag only when they sit inside a run of Latin letters
("ig<ZWSP>nore"), which is how the trick is actually used.
"""

from __future__ import annotations

import re
import unicodedata

_STRIPPED = frozenset({"Cc", "Cf", "Cs", "Co"})
_SPACES = frozenset({"Zs", "Zl", "Zp"})

_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_INVISIBLE_IN_LATIN_WORD = re.compile(f"[A-Za-z][{_INVISIBLE}]+[A-Za-z]")
_BIDI_OVERRIDE = re.compile("[\u202a-\u202e\u2066-\u2069]")
_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
_MATH_ALPHANUMERIC = re.compile("[\U0001d400-\U0001d7ff]")


def _once(raw: str) -> str:
    folded = unicodedata.normalize("NFKC", raw)
    kept: list[str] = []
    for char in folded:
        category = unicodedata.category(char)
        if category in _SPACES or char in "\t\n\r\v\f\x1c\x1d\x1e\x1f\x85":
            kept.append(" ")
        elif category not in _STRIPPED:
            kept.append(char)
    return " ".join("".join(kept).split())


def normalize_text(raw: str) -> str:
    """NFKC, drop invisible code points, collapse all whitespace to single spaces, strip the ends.

    Repeated until stable (it settles within two passes): removing an invisible character can let the
    characters on either side compose ("e" + ZWSP + U+0301 becomes "e" + U+0301, which NFKC then folds
    to "é"), and one pass would leave that second step undone. Idempotence is what lets the same text
    always hash to the same bytes.
    """
    text = raw
    for _ in range(4):
        step = _once(text)
        if step == text:
            break
        text = step
    return text


def hidden_char_codes(raw: str) -> set[str]:
    """Flag codes for what the text hides, judged on the RAW string (before `normalize_text`)."""
    codes: set[str] = set()
    if _TAG_CHARS.search(raw) or _INVISIBLE_IN_LATIN_WORD.search(raw):
        codes.add("hidden_chars")
    if _BIDI_OVERRIDE.search(raw):
        codes.add("bidi_control")
    if _MATH_ALPHANUMERIC.search(raw):
        codes.add("compat_chars")
    return codes


def smuggled_text(raw: str) -> str:
    """ASCII hidden in Unicode tag characters (U+E0020..U+E007E), decoded so it can be scanned.

    Those code points render as nothing but map one-to-one onto ASCII; some models read them.
    """
    return "".join(chr(ord(c) - 0xE0000) for c in raw if 0xE0020 <= ord(c) <= 0xE007E)
