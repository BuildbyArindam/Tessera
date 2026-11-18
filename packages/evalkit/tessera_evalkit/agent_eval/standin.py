"""Scripted stand-ins for the LLM planner. They speak the real `LLMClient` protocol, so the REAL planner, validator,
tools and policy engine run unchanged around them. Neither is Claude, and no number produced with them says how Claude
would score: run `python -m tessera_evalkit.agent_eval --planner llm` with LLM_API_KEY set for that.

`NoopLLM`       proposes nothing. It is the floor: its agreement is the share of scenarios where doing nothing is right.
`ThresholdLLM`  the Day 17 scripted planner, generalised: ask `get_fx_range` for P(the sold currency loses 2% before the
                due date); at 20% or more, CONVERT 40% of the gap, otherwise HOLD it. Pay a funded payable within 3 days
                of its due date to an approved beneficiary. Flag a receivable overdue by 14 days or more.
                Constants (2%, 20%, 40%, 3 days, 14 days) are fixed here, taken from Day 17's design BEFORE any label was
                compared with them. They are not tuned and must not be tuned against the labels.

What `ThresholdLLM` deliberately does not do: it ignores how soon a payable is due when deciding to convert (only the
FX probability counts), ignores forecast width, and cannot see beneficiary age (the planner's context does not carry it).
Those are the behaviours the comparison is meant to expose.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from tessera_agent import LLMResponse, Message, ToolCall, ToolSpec

P_TRIGGER = 0.20
DROP_PCT_KEY = "2.0"
CONVERT_FRACTION = Decimal("0.40")
PAY_WITHIN_DAYS = 3
FLAG_AFTER_DAYS = 14
CENT = Decimal("0.01")


@dataclass
class NoopLLM:
    model: str = "scripted-noop-v1"

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec], temperature: float = 0.0
    ) -> LLMResponse:
        return LLMResponse(text='{"proposals": []}', model=self.model)


def _context(messages: Sequence[Message]) -> dict[str, Any]:
    text = messages[0].text or ""
    found = re.search(r"<context>\s*(.*?)\s*</context>", text, re.S)
    return json.loads(found.group(1)) if found else {}


def _results(messages: Sequence[Message]) -> dict[str, list[dict[str, Any]]]:
    """Tool name -> parsed wrapped results (each carries its evidence_id), from every user turn so far."""
    out: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        for result in message.tool_results:
            wrapped = json.loads(result.content)
            out.setdefault(wrapped["tool"], []).append(wrapped)
    return out


def _d(value: str) -> Decimal:
    return Decimal(value)


@dataclass
class ThresholdLLM:
    """The defaults are the evaluated planner. The two fields below exist for the Day 19 demo and are off by default:
    `convert_when_due_within` converts regardless of the FX probability once a payable is that close (the rubric's R4a),
    `convert_fraction` sizes the conversion."""

    model: str = "scripted-threshold-v1"
    convert_fraction: Decimal = CONVERT_FRACTION
    convert_when_due_within: int = 0

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec], temperature: float = 0.0
    ) -> LLMResponse:
        got = _results(messages)
        if "list_upcoming_obligations" not in got:
            return LLMResponse(
                tool_calls=(ToolCall("c1", "list_upcoming_obligations", {"days_ahead": 90}),), model=self.model
            )
        ctx = _context(messages)
        obligations = got["list_upcoming_obligations"][0]["result"]["obligations"]
        wanted = self._fx_requests(ctx, obligations)
        if wanted and "get_fx_range" not in got:
            calls = tuple(
                ToolCall(f"c{n}", "get_fx_range", {"pair": pair, "horizon_days": days})
                for n, (pair, days) in enumerate(wanted, start=2)
            )
            return LLMResponse(tool_calls=calls, model=self.model)
        return LLMResponse(text=json.dumps({"proposals": self._proposals(ctx, obligations, got)}), model=self.model)

    # ---- what to ask for ---------------------------------------------------------------------
    @staticmethod
    def _gap(ctx: dict[str, Any], ob: dict[str, Any]) -> tuple[str, Decimal] | None:
        """(payable currency, shortfall in that currency) when the held balance cannot cover the payable."""
        if ob["kind"] != "PAYABLE":
            return None
        ccy = ob["amount"]["currency"]
        short = _d(ob["amount"]["amount"]) - _d(ctx["balances"].get(ccy, "0"))
        return (ccy, short) if short > 0 else None

    @staticmethod
    def _source(ctx: dict[str, Any], buy: str) -> str | None:
        """The currency to sell: the held one with the biggest balance that has an FX pair against `buy`."""
        pairs = set(ctx["fx_pairs"])
        held = [(_d(v), c) for c, v in ctx["balances"].items() if c != buy and f"{c}/{buy}" in pairs and _d(v) > 0]
        return max(held)[1] if held else None

    def _fx_requests(self, ctx: dict[str, Any], obligations: list[dict[str, Any]]) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for ob in obligations:
            gap = self._gap(ctx, ob)
            sell = self._source(ctx, gap[0]) if gap else None
            if gap and sell:
                out.append((f"{sell}/{gap[0]}", max(1, min(90, ob["days_until_due"]))))
        return out

    # ---- what to propose ---------------------------------------------------------------------
    def _proposals(
        self, ctx: dict[str, Any], obligations: list[dict[str, Any]], got: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        fx = {w["result"]["range"]["pair"]: w for w in got.get("get_fx_range", [])}
        approved = {b["id"] for b in ctx["beneficiaries"] if b["approved"]}
        out: list[dict[str, Any]] = []
        for ob in obligations:
            days = ob["days_until_due"]
            if ob["kind"] == "RECEIVABLE":
                if days <= -FLAG_AFTER_DAYS and ob["invoice_id"]:
                    out.append(self._item("FLAG_INVOICE", {"invoice_id": ob["invoice_id"], "reason": "likely_late"},
                                          f"Invoice is {-days} days overdue, so it is very likely to pay late.", got, ()))  # fmt: skip
                continue
            gap = self._gap(ctx, ob)
            ccy = ob["amount"]["currency"]
            if gap is None:
                if days <= PAY_WITHIN_DAYS and ob["beneficiary_id"] in approved:
                    params = {"amount": ob["amount"]["amount"], "currency": ccy, "from_currency": ccy,
                              "beneficiary_id": ob["beneficiary_id"], "obligation_id": ob["id"]}  # fmt: skip
                    out.append(self._item("PAY_SUPPLIER", params, "Payable is due within 3 days and funded.", got, ()))
                continue
            sell = self._source(ctx, ccy)
            pair = f"{sell}/{ccy}"
            if sell is None or pair not in fx:
                continue
            rng = fx[pair]["result"]["range"]
            spot = Decimal(str(rng["spot"]))
            sell_all = (gap[1] / spot).quantize(CENT, rounding=ROUND_UP)
            p_fall = next(p["probability"] for p in rng["p_fall"] if f"{p['threshold_pct']:.1f}" == DROP_PCT_KEY)
            evidence = (fx[pair]["evidence_id"],)
            if p_fall >= P_TRIGGER or days <= self.convert_when_due_within:
                amount = (sell_all * self.convert_fraction).quantize(CENT, rounding=ROUND_UP)
                kind, why = "CONVERT", f"P(falls 2%) is {p_fall:.0%} and it is due in {days} days, so convert now."
            else:
                amount, kind, why = sell_all, "HOLD", f"P(falls 2%) is only {p_fall:.0%}, so leave the gap unconverted."
            params = {"amount": str(amount), "currency": sell, "to_currency": ccy, "obligation_id": ob["id"]}
            out.append(self._item(kind, params, why, got, evidence))
        return out[:5]

    @staticmethod
    def _item(
        kind: str, params: dict[str, Any], why: str, got: dict[str, list[dict[str, Any]]], own: tuple[str, ...]
    ) -> dict[str, Any]:
        ids = own or tuple(w["evidence_id"] for w in got["list_upcoming_obligations"])
        return {"action_type": kind, "params": params, "rationale": why, "confidence": 0.6, "evidence_ids": list(ids)}
