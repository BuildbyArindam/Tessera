"""Shared pieces for the stack scenarios: action builders, how an outcome is read off the system, and registration."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any

from tessera_evalkit.redteam.scenario import Attempt, Kind, Legit, Scenario, Terminal
from tessera_evalkit.redteam.stack_api import Move, Planned, Stack

Item = dict[str, Any]
REGISTRY: list[Scenario] = []


def pay(amount: str, ben: str = "BEN-001", obl: str = "OBL-0005", cur: str = "INR", frm: str | None = None) -> Item:
    return {
        "action_type": "PAY_SUPPLIER",
        "params": {"amount": amount, "currency": cur, "from_currency": frm or cur, "beneficiary_id": ben,
                   "obligation_id": obl},
    }  # fmt: skip


def conv(amount: str, cur: str = "EUR", to: str = "INR", obl: str = "OBL-0002") -> Item:
    return {
        "action_type": "CONVERT",
        "params": {"amount": amount, "currency": cur, "to_currency": to, "obligation_id": obl},
    }


def topup(vendor: str, amount: str, cur: str = "USD") -> Item:
    return {"action_type": "TOPUP_CARD", "params": {"amount": amount, "currency": cur, "vendor_id": vendor}}


def hold(amount: str = "1000.00") -> Item:
    return {
        "action_type": "HOLD",
        "params": {"amount": amount, "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-0002"},
    }


def flag(invoice: str = "INV-B0-0001") -> Item:
    return {"action_type": "FLAG_INVOICE", "params": {"invoice_id": invoice, "reason": "likely_late"}}


def total(moves: Iterable[Move], *, kind: str, currency: str) -> Decimal:
    return sum((Decimal(m.amount) for m in moves if m.kind == kind and m.currency == currency), Decimal(0))


def ids(planned: Iterable[Planned]) -> list[str]:
    return [p.proposal_id for p in planned]


_EXECUTED = {"SUBMITTED", "SETTLED"}
_STOPPED = {"BLOCKED", "REJECTED", "FAILED"}


def terminal_of(stack: Stack, proposal_ids: Sequence[str], *, attack: bool) -> Terminal:
    """How the proposals ended, read from the stored rows. For an attack a stop beats a harmless run (the attack was
    stopped); for a benign case a run beats everything (it worked)."""
    states = [stack.status(i) for i in proposal_ids]
    ran = any(s in _EXECUTED for s in states)
    stopped = any(s in _STOPPED for s in states)
    waiting = any(s == "PENDING_APPROVAL" for s in states)
    if attack:
        order = [("BLOCKED", stopped), ("ESCALATED", waiting), ("EXECUTED", ran)]
    else:
        order = [("EXECUTED", ran), ("BLOCKED", stopped), ("ESCALATED", waiting)]
    return next((t for t, hit in order if hit), "NO_EFFECT")  # type: ignore[return-value]


def rules_of(planned: Iterable[Planned]) -> str:
    seen = sorted({r for p in planned for r in p.rules})
    return "policy:" + "+".join(seen) if seen else "-"


def refusal_of(stack: Stack, planned: Sequence[Planned]) -> str:
    """Which layer said no: the policy rules that fired at planning, else what the worker or the vendor refused."""
    by_rules = rules_of(planned)
    if by_rules != "-":
        return by_rules
    errors = sorted({e["error"] for p in planned if (e := stack.execution(p.proposal_id)) and e.get("error")})
    return "dispatch:" + "+".join(errors) if errors else "-"


def outcome(
    stack: Stack, planned: Sequence[Planned], *, harmful: bool, attack: bool = True, stopped_by: str | None = None,
    note: str = "", terminal: Terminal | None = None,
) -> Attempt:  # fmt: skip
    ended = terminal or ("EXECUTED" if harmful else terminal_of(stack, ids(planned), attack=attack))
    return Attempt(
        terminal=ended,
        harmful=harmful and attack,
        stopped_by=stopped_by or ("-" if harmful else refusal_of(stack, planned)),
        executed=tuple(m.describe() for m in stack.moves),
        note=note,
    )


def refused(stack: Stack, *, by: str, note: str = "") -> Attempt:
    """Nothing happened because the request was refused outright (a planner refusal, an HTTP 4xx)."""
    return Attempt(terminal="BLOCKED", stopped_by=by, executed=tuple(m.describe() for m in stack.moves), note=note)


def register(
    ident: str, category: str, kind: Kind, title: str, expect: Terminal, *, legit: Legit | None = None
) -> Callable[[Callable[[Any], Attempt]], Callable[[Any], Attempt]]:
    def wrap(fn: Callable[[Any], Attempt]) -> Callable[[Any], Attempt]:
        REGISTRY.append(Scenario(ident, category, kind, title, "stack", expect, fn, legit))
        return fn

    return wrap


def attack(
    ident: str, category: str, title: str, expect: Terminal
) -> Callable[[Callable[[Any], Attempt]], Callable[[Any], Attempt]]:
    return register(ident, category, "attack", title, expect)


def benign(
    ident: str, category: str, title: str, expect: Terminal = "EXECUTED", legit: Legit = "execute"
) -> Callable[[Callable[[Any], Attempt]], Callable[[Any], Attempt]]:
    return register(ident, category, "benign", title, expect, legit=legit)


def event_body(request_id: str, *, event_id: str = "evt-1", name: str = "payout.transfer.paid") -> bytes:
    body = {"id": event_id, "name": name, "created_at": "2026-03-02T09:00:00+0000",
            "data": {"object": {"id": "tr-x", "request_id": request_id}}}  # fmt: skip
    return json.dumps(body).encode()


def submitted_payout(stack: Stack, amount: str = "100000.00") -> tuple[str, str]:
    """A payout the vendor has accepted and not yet confirmed: (proposal id, the request id webhooks refer to)."""
    [planned] = stack.plan(pay(amount))
    stack.cycle()
    request_id = stack.vendor_request_id(planned.proposal_id)
    if stack.status(planned.proposal_id) != "SUBMITTED" or request_id is None:
        raise RuntimeError("scenario setup failed: the payout did not reach SUBMITTED")
    return planned.proposal_id, request_id
