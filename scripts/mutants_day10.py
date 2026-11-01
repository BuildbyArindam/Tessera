# ruff: noqa: E501
"""The Day 10 mutant list: (name, file, original text, mutated text, tests that must fail)."""

SAN = "tests/unit/test_agent_sanitize.py tests/unit/test_agent_detect.py"  # space-separated: pytest gets each
INJ = "tests/unit/test_agent_injection.py tests/unit/test_agent_injection_defence.py"
GATE = "tests/unit/test_policy_untrusted_gate.py"
RT = "tests/unit/test_redteam.py"
A = "tessera_agent/"
C = "tessera_core/policy/"
E = "tessera_evalkit/redteam/"

# (name, file under the work dir, original text, mutated text, test files that must fail)
MUTANTS: list[tuple[str, str, str, str, str]] = [
    # ---- normalisation ----
    ("NFKC dropped", A + "textnorm.py", 'unicodedata.normalize("NFKC", raw)', "raw", SAN),
    ("invisible characters kept", A + "textnorm.py", "        elif category not in _STRIPPED:", "        else:", SAN),
    (
        "whitespace not collapsed",
        A + "textnorm.py",
        'return " ".join("".join(kept).split())',
        'return "".join(kept)',
        SAN,
    ),
    ("normalisation runs a single pass", A + "textnorm.py", "for _ in range(4):", "for _ in range(1):", SAN),
    (
        "tag characters not reported as hidden",
        A + "textnorm.py",
        "if _TAG_CHARS.search(raw) or _INVISIBLE_IN_LATIN_WORD.search(raw):",
        "if _INVISIBLE_IN_LATIN_WORD.search(raw):",
        SAN,
    ),
    ("tag-character text not decoded", A + "textnorm.py", "chr(ord(c) - 0xE0000)", "chr(ord(c))", SAN),
    # ---- sanitiser ----
    (
        "length cap not applied",
        A + "sanitize.py",
        "text = clean[: SOURCE_CAPS[item.source]].rstrip()",
        "text = clean",
        SAN,
    ),
    (
        "scan runs on the CAPPED text",
        A + "sanitize.py",
        "for code in hidden_char_codes(raw) | scan_text(to_scan):",
        "for code in hidden_char_codes(raw) | scan_text(to_scan[: SOURCE_CAPS[item.source]]):",
        SAN,
    ),
    ("joined-fields scan removed", A + "sanitize.py", "    if len(scanned) > 1:", "    if False:", SAN),
    (
        "no-separator join removed",
        A + "sanitize.py",
        'for code in (scan_text(spaced) | scan_phrases("".join(scanned)[:MAX_JOINED_CHARS])) - seen:',
        "for code in (scan_text(spaced)) - seen:",
        SAN,
    ),
    (
        "block text not escaped",
        A + "sanitize.py",
        r'body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")',
        "body = body",
        SAN,
    ),
    (
        "block tag not tied to the content",
        A + "sanitize.py",
        'tag = f"{BLOCK_TAG}-{result.content_sha256[:16]}"',
        'tag = BLOCK_TAG + "-0000000000000000"',
        SAN,
    ),
    (
        "flag log carries attacker text",
        A + "sanitize.py",
        'log.warning("untrusted input flagged: codes=%s fields=%d", ",".join(result.codes), len(items))',
        'log.warning("untrusted input flagged: codes=%s text=%s", ",".join(result.codes), items[0].text)',
        SAN,
    ),
    (
        "ref_id not validated",
        A + "sanitize.py",
        "if _ID.fullmatch(self.ref_id) is None or looks_like_account_number(self.ref_id):",
        "if False:",
        SAN,
    ),
    (
        "model rationale not normalised",
        A + "sanitize.py",
        "    clean = normalize_text(text)\n    codes = hidden_char_codes(text)",
        "    clean = text\n    codes = hidden_char_codes(text)",
        SAN,
    ),
    ("raw input size not bounded", A + "sanitize.py", "if len(raw) > MAX_RAW_CHARS:", "if False:", SAN),
    # ---- detector ----
    ("look-alike folding disabled", A + "detect.py", "skeleton = _skeleton(lowered)", "skeleton = lowered", SAN),
    ("base64 never decoded", A + "detect.py", "_B64.finditer(text)", "[]", SAN),
    ("ROT13 never tried", A + "detect.py", 'rot13 = codecs.decode(lowered, "rot13")', "rot13 = lowered", SAN),
    ("non-English phrases ignored", A + "detect.py", "_ALL_RE = _EN_RE + _compile(_I18N)", "_ALL_RE = _EN_RE", SAN),
    (
        "mixed-script check removed",
        A + "detect.py",
        "return codes | _mixed_script(text) | _account_codes(text) | _encoded_codes(text, lowered)",
        "return codes | _account_codes(text) | _encoded_codes(text, lowered)",
        SAN,
    ),
    (
        "bare digit runs not an account number",
        A + "detect.py",
        r'r"(?<!\d)\d{9,}(?!\d)"',
        r'r"(?<!\d)\d{90,}(?!\d)"',
        SAN,
    ),
    ("tag-character text not scanned", A + "sanitize.py", "hidden = smuggled_text(raw)", 'hidden = ""', SAN),
    # ---- the policy gate ----
    (
        "a flag no longer escalates",
        C + "builtin_rules/untrusted_input_gate.py",
        "        if screening.flag_codes:",
        "        if False:",
        GATE,
    ),
    (
        "missing screening passes a money action",
        C + "builtin_rules/untrusted_input_gate.py",
        "            if proposal.action_type in MONEY_MOVING_ACTIONS:",
        "            if False:",
        GATE,
    ),
    (
        "screening codes may carry text",
        C + "context.py",
        'Field(pattern=r"^[a-z_]{1,40}$")',
        'Field(pattern=r"^.{1,400}$")',
        GATE,
    ),
    (
        "the gate is not registered",
        C + "builtin_rules/__init__.py",
        "    untrusted_input_gate,\n    velocity_limit,",
        "    velocity_limit,",
        "tests/unit/test_policy_ruleset.py",
    ),
    # ---- the early payment gate ----
    (
        "early payments pass",
        C + "builtin_rules/early_payment_gate.py",
        "        if early > limit:",
        "        if False:",
        "tests/unit/test_policy_early_payment.py",
    ),
    (
        "limit off by one",
        C + "builtin_rules/early_payment_gate.py",
        "        if early > limit:",
        "        if early >= limit:",
        "tests/unit/test_policy_early_payment.py",
    ),
    (
        "unknown obligation passes",
        C + "builtin_rules/early_payment_gate.py",
        "if due is None:\n            return self.result(RuleOutcome.ESCALATE",
        "if due is None:\n            return self.result(RuleOutcome.PASS",
        "tests/unit/test_policy_early_payment.py",
    ),
    (
        "not registered",
        C + "builtin_rules/__init__.py",
        "    early_payment_gate,\n    forecast",
        "    forecast",
        "tests/unit/test_policy_ruleset.py",
    ),
    # ---- the planner ----
    (
        "failed reply replayed on retry",
        A + "planner.py",
        "withhold_replay=bool(screening.fields),",
        "withhold_replay=False,",
        INJ,
    ),
    (
        "model rationale not screened",
        A + "planner.py",
        "rationale, found = screen_model_text(item.rationale, proposal_id)",
        "rationale, found = item.rationale, ()",
        INJ,
    ),
    (
        "proposal ids ignore the notes",
        A + "planner.py",
        'basis = sha256_hex(f"{context_hash}|{screening.content_sha256}") if screening.fields else context_hash',
        "basis = context_hash",
        INJ,
    ),
    (
        "input flags not recorded",
        A + "planner.py",
        "input_flags=(*screening.flags, *output_flags),",
        "input_flags=(),",
        INJ,
    ),
    (
        "default prompt back to v1",
        A + "prompts.py",
        'DEFAULT_PROMPT = "planner_v2"',
        'DEFAULT_PROMPT = "planner_v1"',
        INJ,
    ),
    (
        "raw note text sent instead of the delimited block",
        A + "planner.py",
        "text=_task_message(context_json, render_block(screening))",
        'text=_task_message(context_json, "\\n".join(n.text for n in snapshot.untrusted))',
        INJ,
    ),
    # ---- the runner ----
    (
        "executor called for anything not blocked",
        E + "runner.py",
        "if verdict.verdict is Verdict.APPROVE:",
        "if verdict.verdict is not Verdict.BLOCK:",
        RT,
    ),
    (
        "blind mode still tells policy",
        E + "runner.py",
        'to_policy = () if mode == "blind" else plan.flag_codes',
        "to_policy = plan.flag_codes",
        RT,
    ),
    (
        "allowlist includes unapproved payees",
        E + "world.py",
        "frozenset(b.id for b in self.beneficiaries if b.approved)",
        "frozenset(b.id for b in self.beneficiaries)",
        RT,
    ),
    (
        "screening result never reaches policy",
        E + "world.py",
        "input_screening=InputScreening(flag_codes=flag_codes),",
        "input_screening=InputScreening(),",
        RT,
    ),
    (
        "the gate is blamed for blocks",
        E + "runner.py",
        "        if outcome is RuleOutcome.BLOCK:\n            blocked.append(rule.rule_id)",
        "        if outcome is not RuleOutcome.PASS:\n            blocked.append(rule.rule_id)",
        RT,
    ),
]
