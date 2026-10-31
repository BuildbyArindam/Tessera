# ruff: noqa: E501  (report-style script: long print lines are the output)
"""Day 9 verification: the LLM planner. Run: .venv/bin/python scripts/verify_day9.py [--live]

Mirrors the Day 9 verification prompt and exits non-zero on any failure:
  a. invalid JSON from the model => one retry, then an empty list, no crash
  b. a proposal with an extra field => rejected
  c. a proposal with a free-text account number => rejected by the schema
  d. the agent package imports nothing from tessera_airwallex (AST scan of every module)
  e. tools cannot mutate state: the Airwallex mock and the snapshot are identical after a full planner run
Plus: temperature 0 on every call, prompt hash and model name recorded, no socket opened by any of it.
`--live` then makes ONE real call (needs LLM_API_KEY, optional LLM_MODEL) and prints raw output, parsed
proposals and the prompt hash. Without --live that step is reported as NOT RUN, never as passed.
"""

import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests/unit")]

from agent_helpers import (  # noqa: E402
    NOW,
    convert_item,
    first_open_invoice,
    make_mock,
    make_snapshot,
    mock_state,
    pay_item,
    reply,
)
from tessera_agent import FakeLLM, LLMError, Planner, llm_from_env, load_prompt, tool_call_reply  # noqa: E402
from test_agent_isolation import agent_files, violations  # noqa: E402
from test_agent_tools import digest  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def refuse(*_a: object, **_k: object) -> None:
    raise AssertionError("socket opened")


print("building a synthetic snapshot (fits the late-payment scorer, ~20 s)...")
mock = make_mock()
snap = make_snapshot(mock)
prompt = load_prompt()
OBLIGATIONS = tool_call_reply("list_upcoming_obligations", {"days_ahead": 30})

# Everything from here to the live step runs with sockets disabled.
_real_connect, _real_gai = socket.socket.connect, socket.getaddrinfo
socket.socket.connect, socket.getaddrinfo = refuse, refuse  # type: ignore[method-assign, assignment]


def plan(script: list, s=snap):  # noqa: ANN001, ANN201
    llm = FakeLLM(script)
    return Planner(llm).run(s, now=NOW), llm


print("\n== a. invalid JSON: retry once, then empty list ==")
r, llm = plan(["I think we should convert some USD", "still prose, no JSON"])
check("returns an empty list, no exception", r.proposals == () and r.record.outcome == "invalid_output")
check("exactly one retry (2 model calls), tools off on the retry", len(llm.calls) == 2 and llm.calls[1].tools == ())
print(f"      errors the model was told: {list(r.record.validation_errors)}")
r, _ = plan([OBLIGATIONS, "oops", reply(convert_item())])
check("a corrected retry is accepted", len(r.proposals) == 1 and r.record.attempts == 2)

print("\n== b. extra field ==")
bad = reply(convert_item() | {"execute_immediately": True})
r, _ = plan([OBLIGATIONS, bad, bad])
check(
    "rejected",
    r.proposals == () and any("extra_forbidden" in e for e in r.record.validation_errors),
    str(r.record.validation_errors),
)

print("\n== c. free-text account number ==")
for label, value in (
    ("IBAN", "GB82WEST12345698765432"),
    ("account no.", "12345678901234"),
    ("card no.", "4111-1111-1111-1111"),
    ("name", "Acme Supplies Ltd"),
):
    bad = reply(pay_item(value))
    r, retry_llm = plan([OBLIGATIONS, bad, bad])
    leaked = any(value in (m.text or "") for m in retry_llm.calls[-1].messages if m.role == "user")
    check(f"{label} as beneficiary_id: rejected and not echoed back", r.proposals == () and not leaked)
unknown = reply(pay_item("BEN-777"))
r, _ = plan([OBLIGATIONS, unknown, unknown])
check(
    "a well-formed id the planner was never given: rejected",
    r.proposals == () and any("unknown_id" in e for e in r.record.validation_errors),
)

print("\n== d. no airwallex import anywhere in the agent package ==")
files = agent_files()
check(
    f"AST scan of {len(files)} modules finds no forbidden import", all(violations(f.read_text()) == [] for f in files)
)
grep = re.compile(r"^\s*(from|import)\s+tessera_airwallex", re.M)  # the guide's grep: import lines, not prose
check(
    "grep for 'import tessera_airwallex' / 'from tessera_airwallex' finds nothing",
    not any(grep.search(f.read_text()) for f in files),
)

print("\n== e. tools cannot mutate state ==")
before_mock, before_snap = mock_state(mock), digest(snap)
script = [
    tool_call_reply("get_forecast", {}, "a"),
    tool_call_reply("get_fx_range", {"pair": "USD/INR", "horizon_days": 30}, "b"),
    tool_call_reply("get_late_risk", {"invoice_ids": [first_open_invoice()]}, "c"),
    tool_call_reply(
        "simulate_proposal",
        {
            "action_type": "CONVERT",
            "amount": "3000.00",
            "currency": "USD",
            "to_currency": "INR",
            "obligation_id": "OBL-0001",
        },
        "d",
    ),
    tool_call_reply("list_upcoming_obligations", {}, "e"),
    reply(convert_item(evidence=("ev-1", "ev-4"))),
]
r, _ = plan(script)
check("all five tools ran and one proposal came out", len(r.record.tool_calls) == 5 and len(r.proposals) == 1)
check("Airwallex mock state identical before and after", mock_state(mock) == before_mock)
check("snapshot (forecasts, FX, invoices, obligations) identical before and after", digest(snap) == before_snap)

print("\n== record: model, prompt hash, temperature ==")
rec = r.record
check("temperature 0.0 on every model call", rec.temperature == 0.0)
check(
    "prompt hash recorded and equals the file's SHA-256",
    rec.prompt_hash == prompt.sha256,
    f"{prompt.name} {prompt.sha256[:16]}...",
)
check("model name recorded", rec.model == "fake-llm")
check(
    "proposal ids are deterministic for the same state",
    [p.id for p in plan(script)[0].proposals] == [p.id for p in r.proposals],
)

socket.socket.connect, socket.getaddrinfo = _real_connect, _real_gai  # type: ignore[method-assign]

print("\n== live model call ==")
if "--live" not in sys.argv:
    print("NOT RUN  pass --live (with LLM_API_KEY set) to make one real call. This is NOT counted as a pass.")
else:
    try:
        client = llm_from_env()
    except LLMError as exc:
        check("live client configured", False, str(exc))
    else:
        result = Planner(client).run(snap, now=NOW)
        rec = result.record
        print(f"model={rec.model}  prompt_hash={rec.prompt_hash}  outcome={rec.outcome}  llm_calls={rec.llm_calls}")
        print(f"tool calls: {[(t.name, t.evidence_id) for t in rec.tool_calls]}")
        print(f"validation errors: {list(rec.validation_errors)}")
        print(f"--- raw model output ---\n{rec.raw_output}\n--- parsed proposals ({len(result.proposals)}) ---")
        for p in result.proposals:
            print(
                f"  {p.id} {p.action_type.value} {dict(p.params)} conf={p.confidence} evidence={result.evidence[p.id]}\n    {p.rationale}"
            )
        check("live call completed without a provider error", rec.outcome != "llm_error")

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
