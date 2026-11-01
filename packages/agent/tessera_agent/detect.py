"""The injection detector. A SIGNAL for a human, never the defence.

It looks for text that tries to give orders (instruction phrases, fake system markup, tool-schema
mimicry, requests to pay somewhere new or to lift a limit), text that tries to hide (homoglyphs, mixed
scripts, encodings), and payment destinations (IBAN, account, UPI, IFSC shapes). A hit raises the whole
planning run to ESCALATE (policy rule `untrusted_input_gate`); it never blocks and never approves.

What it is NOT, stated so nobody leans on it:
  * Pattern matching. Paraphrase, other languages and fresh tricks get past it. The multilingual lists
    are short, and detector-evading cases are in the red-team suite on purpose.
  * The only thing standing between a fooled model and money. The deterministic policy rules judge
    every proposal whether or not this fired, and the red-team run shows them doing it with the
    detector switched off.

Output is codes only (`FLAG_CODES`). The text itself is never returned or logged: a log line built from
attacker text is its own injection vector.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import re
import unicodedata
from collections.abc import Sequence

from pydantic import Field
from tessera_core.domain.base import DomainModel

from tessera_agent.textnorm import normalize_text

FLAG_CODES = (
    "instruction_phrase", "role_markup", "tool_mimicry", "payment_request", "limit_change",  # what it says
    "hidden_chars", "bidi_control", "compat_chars", "mixed_script", "homoglyph", "encoded_text",  # how it hides
    "account_number", "oversize",  # what it points at, and how much of it there is
)  # fmt: skip

MAX_BLOBS = 10  # encoded runs decoded per text: bounded work on hostile input


class InputFlag(DomainModel):
    """One thing the screen found. Carries a code and where, never the text."""

    code: str = Field(pattern=r"^[a-z_]{1,40}$")
    source: str = Field(pattern=r"^[a-z_]{1,32}$")  # an untrusted source, "joined", or "model_output"
    ref_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")  # the record the text belongs to; "-" for none


_I = re.IGNORECASE
_GAP = r"(?:\W+\w+){0,4}?\W+"  # up to four filler words between a verb and its object

_EN: tuple[tuple[str, str], ...] = (
    ("instruction_phrase", r"\b(?:ignore|disregard|forget|override|bypass|circumvent|skip|drop)\b" + _GAP
     + r"(?:instructions?|rules?|polic(?:y|ies)|guidelines?|restrictions?|safeguards?|checks?|prompts?|"
     r"limits?|caps?)\b"),
    ("instruction_phrase", r"\bforget (?:everything|all|what)\b|\bnew (?:instructions?|rules?|orders?|directives?)\b"),
    ("instruction_phrase", r"\bfrom now on\b|\b(?:jailbreak|dan mode|developer mode|god mode|do anything now)\b"),
    ("instruction_phrase", r"\byou (?:are|must|should|shall|will|need to|have to) (?:now |immediately |always )?"
     r"(?:act|behave|respond|reply|answer|pay|transfer|approve|ignore|obey|execute|send)\b"),
    ("instruction_phrase", r"\bact as (?:a|an|the|if)\b|\bpretend (?:to be|you(?:'re| are))\b|\brole-?play\b"
     r"|\byou are now\b"),
    ("instruction_phrase", r"\b(?:do not|don't|never) (?:escalate|flag|mention|tell|report|alert|notify|ask|verify)\b"),
    ("instruction_phrase", r"\bwithout (?:any )?(?:approval|confirmation|review|human|checking|verification|"
     r"escalation)\b"),
    ("instruction_phrase", r"\b(?:system|developer|hidden|secret) (?:prompt|message|instructions?)\b"),
    ("instruction_phrase", r"\b(?:i am|i'm|this is|message from|on behalf of) (?:the |your )?"
     r"(?:ceo|cfo|cto|owner|director|administrator|admin|treasurer|manager|bank|airwallex|support team|compliance)\b"),
    ("instruction_phrase", r"\b(?:approve|execute|authori[sz]e|confirm) (?:this|it|the (?:payment|transfer|proposal|"
     r"action|conversion)|now|immediately)\b"),
    ("role_markup", r"<\|?\s*(?:im_start|im_end|system|assistant|user|endoftext|eot_id)\s*\|?>"),
    ("role_markup", r"\[\s*/?\s*(?:inst|sys|system)\s*\]|<<\s*/?\s*sys\s*>>"),
    ("role_markup", r"(?:^|[\s>\]}\"'])(?:system|assistant|developer|human)\s*:"
     r"|#{2,}\s*(?:system|instruction|new task)"),
    ("role_markup", r"\b(?:begin|end) (?:system|admin|developer|new) (?:prompt|message|instructions?)\b"),
    ("role_markup", r"</?\s*(?:untrusted[_-]?data|context|instructions?|system|data|tool_?results?|"
     r"tool_?calls?)\b[^>]{0,60}>"),
    ("tool_mimicry", r"\b(?:action_type|evidence_ids?|beneficiary_id|obligation_id|vendor_id|invoice_id|"
     r"simulate_proposal|get_forecast|get_fx_range|get_late_risk|list_upcoming_obligations)\b|\"proposals\"\s*:"),
    ("tool_mimicry", r"\b(?:pay_supplier|topup_card|flag_invoice)\b"),
    ("payment_request", r"\b(?:pay|send|transfer|wire|remit|route|redirect|reroute|settle|deposit|credit|move)\b" + _GAP
     + r"(?:account|a/c|iban|swift|bic|ifsc|upi|routing|beneficiary|payee|wallet)\b"),
    ("payment_request", r"\b(?:new|updated|changed|different|alternate|alternative|revised) "
     r"(?:bank (?:details|account)|"
     r"account (?:details|number)|beneficiary|payee|iban|banking details|payment details|wire instructions)\b"),
    ("payment_request", r"\bbank details (?:have |has )?(?:changed|been (?:updated|changed))\b"),
    ("payment_request", r"\b(?:add|create|register|set ?up) (?:a |an |the |our )?(?:new )?"
     r"(?:beneficiary|payee|supplier|vendor)\b"),
    ("limit_change", r"\b(?:raise|increase|lift|remove|double|triple|set|change|update|disable|turn off|switch off|"
     r"deactivate|unlock|extend)\b" + _GAP
     + r"(?:card limits?|limits?|caps?|thresholds?|allow ?lists?|whitelists?|kill ?switch|guardrails?|"
     r"polic(?:y|ies))\b"),
)  # fmt: skip

# Short, deliberately incomplete lists for the languages Tessera's niche is most likely to meet. Run on
# the plain text only (the look-alike folding below would mangle Cyrillic). No \b: it misbehaves on
# Devanagari combining marks, so words are separated by \s+ instead.
_I18N: tuple[tuple[str, str], ...] = (
    ("instruction_phrase", r"ignora(?:r|d)?\s+(?:todas\s+)?(?:las\s+)?instrucciones\s+(?:anteriores|previas)"),
    ("instruction_phrase", r"olvida(?:r)?\s+(?:todas\s+)?(?:tus|las)\s+instrucciones"
     r"|omite\s+(?:las\s+)?(?:reglas|instrucciones)"),
    ("payment_request", r"\b(?:paga|transfiere|env[ií]a)\b.{0,40}\bcuenta\b"),
    ("instruction_phrase", r"ignore[zr]?\s+(?:toutes\s+)?les\s+instructions\s+"
     r"(?:pr[eé]c[eé]dentes|ci-dessus|ant[eé]rieures)"),
    ("instruction_phrase", r"oubliez?\s+(?:toutes\s+)?(?:vos|les)\s+instructions"),
    ("payment_request", r"\b(?:payez|virez|transf[eé]rez|versez)\b.{0,40}\bcompte\b"),
    ("instruction_phrase", r"ignorier(?:e|en)?\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen|fr[uü]heren)\s+"
     r"(?:anweisungen|instruktionen|regeln)"),
    ("instruction_phrase", r"vergiss\s+(?:alle\s+)?(?:deine\s+|die\s+)?(?:anweisungen|regeln)"),
    ("payment_request", r"\b(?:[uü]berweise|ueberweise|zahle)\b.{0,40}\bkonto\b"),
    ("instruction_phrase", r"игнорируй(?:те)?\s+(?:все\s+)?(?:предыдущие|прежние|прошлые)\s+"
     r"(?:инструкции|указания|правила)"),
    ("instruction_phrase", r"забудь(?:те)?\s+(?:все\s+)?(?:свои\s+|ваши\s+)?(?:инструкции|правила)"),
    ("payment_request", r"переве(?:ди|дите)\b.{0,40}\bсч[её]т\b"),
    ("instruction_phrase", r"(?:忽略|无视|忽视|不要理会)(?:之前|以前|以上|上述|先前|所有|全部|前面)*的?(?:所有|全部)?"
     r"(?:指令|指示|说明|规则|提示)"),
    ("payment_request", r"(?:转账|汇款|打款|支付)到?.{0,12}(?:账户|账号|帐户|帐号)"),
    ("instruction_phrase", r"पिछले\s+(?:सभी\s+)?निर्देश(?:ों)?\s+को\s+(?:अनदेखा|नज़?रअंदाज़?|भूल)"),
    ("instruction_phrase", r"सभी\s+निर्देश(?:ों)?\s+(?:को\s+)?(?:अनदेखा|भूल)"),
    ("payment_request", r"नए\s+(?:बैंक\s+)?खाते\s+में"),
    ("instruction_phrase", r"\bpichhle\s+(?:sabhi\s+)?nirdesh(?:on)?\s+(?:ko\s+)?(?:ignore|andekha|bhool)"),
    ("payment_request", r"\bnaye?\s+(?:bank\s+)?(?:khate|account)\s+(?:me|mein|men)\s+"
     r"(?:paise|paisa|rupaye|payment|transfer)"),
    ("instruction_phrase", r"تجاهل\s+(?:جميع\s+|كل\s+)?التعليمات\s+السابقة"),
)  # fmt: skip


def _compile(table: Sequence[tuple[str, str]]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((code, re.compile(pattern, _I)) for code, pattern in table)


_EN_RE = _compile(_EN)
_ALL_RE = _EN_RE + _compile(_I18N)


def _phrase_codes(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> set[str]:
    return {code for code, pattern in patterns if pattern.search(text)}


# ---- look-alikes -------------------------------------------------------------------------------
_CONFUSABLES = str.maketrans({
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ԛ": "q", "ԝ": "w",
    "ӏ": "l",  # Cyrillic
    "ο": "o", "α": "a", "ν": "v", "ι": "i", "ρ": "p", "υ": "u", "κ": "k",
    "χ": "x", "ϲ": "c",  # Greek
    "ı": "i", "ɑ": "a", "ɡ": "g", "ɩ": "i",  # Latin look-alikes outside NFKC
})  # fmt: skip
_LEET = str.maketrans("013457@$", "oieastas")
_TOKEN = re.compile(r"[a-z0-9@$]+")


def _skeleton(lowered: str) -> str:
    """Fold look-alike letters and in-word digit substitutions (1gn0re) to plain Latin."""
    folded = lowered.translate(_CONFUSABLES)

    def fix(match: re.Match[str]) -> str:
        token = match.group()
        has_letter = any(c.isalpha() for c in token)
        return token.translate(_LEET) if has_letter and any(not c.isalpha() for c in token) else token

    return _TOKEN.sub(fix, folded)


def _mixed_script(text: str) -> set[str]:
    for word in re.findall(r"\w{2,}", text):
        scripts = {
            name.split(" ", 1)[0]
            for name in (unicodedata.name(c, "") for c in word if c.isalpha())
            if name.startswith(("LATIN", "CYRILLIC", "GREEK"))
        }
        if "LATIN" in scripts and len(scripts) > 1:
            return {"mixed_script"}
    return set()


# ---- payment destinations ----------------------------------------------------------------------
_IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,3})?(?![A-Za-z0-9])")
_ACCOUNT_SHAPES = tuple(
    re.compile(p, _I)
    for p in (
        r"(?<!\d)\d{9,}(?!\d)",  # a bare run of digits (bank account, card)
        r"(?<!\d)(?:\d{3,4}[ -]){2,}\d{2,4}(?!\d)",  # digits in groups ("4111 1111 1111 1111")
        r"\b[A-Z]{4}0[A-Z0-9]{6}\b",  # IFSC
        r"(?<![\w.@-])[\w.-]{2,64}@[a-z]{2,20}\b(?![.\w-])",  # UPI id: name@bank, no dot after the @
    )
)


def _account_codes(text: str) -> set[str]:
    # An IBAN-shaped match must also carry at least 8 digits, or ordinary words ("IN24 will be paid") qualify.
    if any(sum(c.isdigit() for c in m.group()) >= 8 for m in _IBAN.finditer(text)):
        return {"account_number"}
    return {"account_number"} if any(p.search(text) for p in _ACCOUNT_SHAPES) else set()


# ---- encoded text ------------------------------------------------------------------------------
_B64 = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_=-])")
_HEX = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){12,}(?![0-9A-Fa-f])")
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_ESCAPES = re.compile(r"\\u[0-9A-Fa-f]{4}|&#x?[0-9A-Fa-f]+;")


def _readable(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 8 or sum(c.isprintable() for c in text) / len(text) < 0.9:
        return None
    return text


def _b64_text(blob: str) -> str | None:
    body = blob.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        return _readable(base64.b64decode(body + "=" * (-len(body) % 4), validate=True))
    except (binascii.Error, ValueError):
        return None


def _hex_text(blob: str) -> str | None:
    try:
        return _readable(bytes.fromhex(blob))
    except ValueError:
        return None


def _unescaped(text: str) -> str:
    """Percent, \\uXXXX and HTML-entity escapes resolved (only when there are several, so one stray
    "%20" or "&amp;" in a URL is left alone). Done by hand: the agent package may not import urllib."""
    out = text
    if len(_PERCENT.findall(out)) >= 3:
        octets = re.sub(rb"%([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), out.encode("utf-8"))
        out = octets.decode("utf-8", errors="replace")
    if len(_ESCAPES.findall(out)) >= 3:
        out = html.unescape(re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), out))
    return out


def _encoded_codes(text: str, lowered: str) -> set[str]:
    codes: set[str] = set()
    blobs = [t for t in (_b64_text(m.group()) for m in list(_B64.finditer(text))[:MAX_BLOBS]) if t]
    blobs += [t for t in (_hex_text(m.group()) for m in list(_HEX.finditer(text))[:MAX_BLOBS]) if t]
    if blobs:
        codes.add("encoded_text")
    unescaped = _unescaped(text)
    if unescaped != text:
        blobs.append(unescaped)
    for blob in blobs:
        inner = normalize_text(blob)
        hits = _phrase_codes(inner.lower(), _ALL_RE) | _account_codes(inner)
        if hits:
            codes |= hits | {"encoded_text"}
    rot13 = codecs.decode(lowered, "rot13")
    if rot13 != lowered and not _phrase_codes(lowered, _EN_RE):
        hits = _phrase_codes(rot13, _EN_RE)
        if hits:
            codes |= hits | {"encoded_text"}
    return codes


# ---- entry points ------------------------------------------------------------------------------
def scan_text(text: str) -> set[str]:
    """Flag codes for text that is ALREADY normalised (see `textnorm.normalize_text`)."""
    lowered = text.lower()
    codes = _phrase_codes(lowered, _ALL_RE)
    skeleton = _skeleton(lowered)
    if skeleton != lowered:
        folded = _phrase_codes(skeleton, _EN_RE)
        if folded - codes:  # a phrase that only appears once look-alikes are folded away
            codes |= folded | {"homoglyph"}
    return codes | _mixed_script(text) | _account_codes(text) | _encoded_codes(text, lowered)


def scan_phrases(text: str) -> set[str]:
    """Phrase codes only. Used where joining unrelated fields could otherwise invent an account number."""
    return _phrase_codes(text.lower(), _ALL_RE)
