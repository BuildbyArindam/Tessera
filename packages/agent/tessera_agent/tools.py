"""Read-only tools the model may call. They return JSON and never change anything.

Why they cannot mutate: every tool is a pure function of the frozen `PlannerSnapshot` and its own
(validated) arguments. There is no database handle, no Airwallex port and no writable state in reach,
and `tessera_agent` may not import `tessera_airwallex` (enforced by a test today, import-linter on Day 13).

What the model controls is deliberately small:
  * business: fixed by the snapshot; a tool cannot be pointed at another business;
  * numbers that matter to safety are not model inputs: `simulate_proposal` takes the due date from the
    obligation's record (never from the model), and seeds and path counts are fixed here;
  * arguments are strict (`extra="forbid"`, bounded) and a bad call returns an error result the model
    can read, never an exception that stops the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, ValidationError, model_validator
from tessera_core.domain import ActionType, Currency, Money, Proposal
from tessera_core.risk import assess_late_payment, estimate_fx_range
from tessera_core.simulate import SimulationConfig, simulate

from tessera_agent.canon import canonical_json, sha256_hex
from tessera_agent.context import PlannerSnapshot
from tessera_agent.llm import ToolSpec
from tessera_agent.schemas import Amount, RecordId, summarize_errors

SIM_PATHS = 5000
SIM_SEED = 0
FX_SEED = 0
MAX_ERROR_CHARS = 300
Pair = Annotated[str, Field(pattern=r"^[A-Z]{3}/[A-Z]{3}$")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GetForecastArgs(_Args):
    currency: Currency | None = None
    horizon_weeks: StrictInt | None = Field(default=None, ge=1, le=13)


class GetFxRangeArgs(_Args):
    pair: Pair
    horizon_days: StrictInt = Field(ge=1, le=90)


class GetLateRiskArgs(_Args):
    invoice_ids: list[RecordId] | None = Field(default=None, max_length=20)
    limit: StrictInt = Field(default=10, ge=1, le=20)


class SimulateArgs(_Args):
    action_type: Literal["CONVERT", "HOLD", "PAY_SUPPLIER"]
    amount: Amount
    currency: Currency
    to_currency: Currency | None = None  # CONVERT, HOLD
    from_currency: Currency | None = None  # PAY_SUPPLIER
    obligation_id: RecordId

    @model_validator(mode="after")
    def _needs_the_right_currency(self) -> SimulateArgs:
        if self.action_type == "PAY_SUPPLIER":
            if self.from_currency is None or self.to_currency is not None:
                raise ValueError("PAY_SUPPLIER takes from_currency and no to_currency")
        elif self.to_currency is None or self.from_currency is not None:
            raise ValueError(f"{self.action_type} takes to_currency and no from_currency")
        return self


class ListObligationsArgs(_Args):
    days_ahead: StrictInt = Field(default=30, ge=1, le=120)


def money_json(money: Money) -> dict[str, JsonValue]:
    return {"amount": str(money.amount), "currency": money.currency.value}


def relative_width(point: Decimal, lower: Decimal, upper: Decimal) -> str | None:
    """(upper - lower) / |point|, the quantity forecast_confidence_gate compares. None when point is 0."""
    if point == 0:
        return None
    return str(((upper - lower) / abs(point)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))


class ToolError(ValueError):
    """The call was well-formed but cannot be answered (unknown id, missing data)."""


class ReadOnlyTools:
    def __init__(
        self, snapshot: PlannerSnapshot, *, n_paths: int = SIM_PATHS, sim_config: SimulationConfig | None = None
    ):
        self._snap = snapshot
        self._n_paths = n_paths
        self._sim_config = sim_config or SimulationConfig()
        self._handlers: dict[str, tuple[type[_Args], str, Callable[[Any], dict[str, JsonValue]]]] = {
            "get_forecast": (
                GetForecastArgs,
                "Calibrated cash-flow forecast (point and 80% interval) per currency and horizon, with the interval "
                "width relative to the point estimate.",
                self._get_forecast,
            ),
            "get_fx_range": (
                GetFxRangeArgs,
                "Probabilistic range (5/25/50/75/95th percentile) of an FX pair's change over a horizon in days, and "
                "the probability the pair falls by 1%, 2% and 5%. Pair is SELL/BUY: BUY units per 1 SELL unit.",
                self._get_fx_range,
            ),
            "get_late_risk": (
                GetLateRiskArgs,
                "Probability each open customer invoice is paid more than 7 days late, with an expected payment date "
                "range. Without invoice_ids, returns the riskiest open invoices.",
                self._get_late_risk,
            ),
            "simulate_proposal": (
                SimulateArgs,
                "Replay a candidate CONVERT, HOLD or PAY_SUPPLIER over resampled historical FX paths against its "
                "baseline. The due date comes from obligation_id's record. Returns expected saving, 5th-percentile "
                "outcome and worst-case loss in the sell currency.",
                self._simulate,
            ),
            "list_upcoming_obligations": (
                ListObligationsArgs,
                "Receivables and payables due within days_ahead (overdue included), with ids, amounts and due dates.",
                self._list_obligations,
            ),
        }

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(name=name, description=desc, input_schema=args.model_json_schema())
            for name, (args, desc, _) in self._handlers.items()
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
        """Run one tool. Never raises for a bad call: returns {"error": {...}} the model can read."""
        entry = self._handlers.get(name)
        if entry is None:
            return {"error": {"type": "unknown_tool", "message": f"no tool named {name[:40]!r}"}}
        args_model, _, handler = entry
        try:
            args = args_model.model_validate(dict(arguments))
            payload = handler(args)
            canonical_json(payload)  # refuses NaN/Infinity, so a non-finite number becomes an error result
            return payload
        except ValidationError as exc:
            return {"error": {"type": "invalid_arguments", "message": "; ".join(summarize_errors(exc))}}
        except ValueError as exc:  # domain errors (short history, unknown invoice, ...) are all ValueErrors
            return {"error": {"type": type(exc).__name__, "message": str(exc)[:MAX_ERROR_CHARS]}}

    # ---- handlers ----------------------------------------------------------------------------
    def _get_forecast(self, args: GetForecastArgs) -> dict[str, JsonValue]:
        rows = [
            f
            for f in self._snap.forecasts
            if (args.currency is None or f.currency == args.currency.value)
            and (args.horizon_weeks is None or f.horizon_weeks == args.horizon_weeks)
        ]
        if not rows:
            raise ToolError("no forecast matches that currency and horizon")
        rows.sort(key=lambda f: (f.currency, f.horizon_weeks))
        return {
            "as_of": self._snap.as_of.isoformat(),
            "forecasts": [
                {
                    "currency": f.currency,
                    "horizon_weeks": f.horizon_weeks,
                    "point": str(f.point),
                    "lower": str(f.lower),
                    "upper": str(f.upper),
                    "target_coverage": f.target_coverage,
                    "relative_width": relative_width(f.point, f.lower, f.upper),
                    "model_version": f.model_version,
                }
                for f in rows
            ],
        }

    def _get_fx_range(self, args: GetFxRangeArgs) -> dict[str, JsonValue]:
        series = self._snap.fx_history.get(args.pair)
        if series is None:
            raise ToolError(f"no FX history for {args.pair}")
        visible = series.up_to(self._snap.as_of)  # nothing after as_of is ever read
        result = estimate_fx_range(visible.values, pair=args.pair, horizon_days=args.horizon_days, seed=FX_SEED)
        return {"as_of": self._snap.as_of.isoformat(), "range": result.model_dump(mode="json")}

    def _get_late_risk(self, args: GetLateRiskArgs) -> dict[str, JsonValue]:
        scorer = self._snap.late_scorer
        if scorer is None:
            raise ToolError("no late-payment scorer is available")
        frame = self._snap.invoices
        own = frame[frame["business_id"] == self._snap.business_id]
        assessed = assess_late_payment(scorer, own, self._snap.as_of, args.invoice_ids)
        assessed.sort(key=lambda a: (-a.p_late, a.invoice_id))
        shown = assessed if args.invoice_ids is not None else assessed[: args.limit]
        return {
            "as_of": self._snap.as_of.isoformat(),
            "total_open": len(assessed),
            "invoices": [a.model_dump(mode="json") for a in shown],
        }

    def _simulate(self, args: SimulateArgs) -> dict[str, JsonValue]:
        obligation = self._snap.obligation(args.obligation_id)
        if obligation is None:
            raise ToolError("unknown obligation_id")
        due = max(obligation.due_on, self._snap.as_of)  # the due date is the record's, never the model's
        params: dict[str, JsonValue] = {"amount": args.amount, "currency": args.currency.value}
        if args.to_currency is not None:
            params["to_currency"] = args.to_currency.value
        if args.from_currency is not None:
            params["from_currency"] = args.from_currency.value
        digest = sha256_hex(canonical_json(params) + args.action_type + args.obligation_id)[:16]
        probe = Proposal(
            id=f"sim-{digest}",
            action_type=ActionType(args.action_type),
            params=params,
            rationale="simulation probe",
            confidence=0.5,
            created_at=datetime.combine(self._snap.as_of, time.min, tzinfo=UTC),
        )
        report = simulate(
            probe,
            self._snap.fx_history,
            self._snap.as_of,
            self._n_paths,
            SIM_SEED,
            due_date=due,
            config=self._sim_config,
        )
        return {
            "as_of": self._snap.as_of.isoformat(),
            "due_date": due.isoformat(),
            "action_type": report.action_type.value,
            "comparator": report.comparator,
            "pair": report.pair,
            "amount": money_json(report.amount),
            "horizon_days": report.horizon_days,
            "n_paths": report.n_paths,
            "seed": report.seed,
            "expected_saving": money_json(report.expected_saving),
            "saving_p05": money_json(report.saving_p05),
            "worst_case_loss": money_json(report.worst_case_loss),
            "prob_beats_baseline": report.prob_beats_baseline,
            "spread_cost": money_json(report.spread_cost),
            "fee_cost": money_json(report.fee_cost),
            "notes": list(report.notes),
            "assumptions": {"spread_bps": str(self._sim_config.spread_bps), "fee": str(self._sim_config.fee)},
        }

    def _list_obligations(self, args: ListObligationsArgs) -> dict[str, JsonValue]:
        horizon: date = self._snap.as_of + timedelta(days=args.days_ahead)
        rows = sorted((o for o in self._snap.obligations if o.due_on <= horizon), key=lambda o: (o.due_on, o.id))
        return {
            "as_of": self._snap.as_of.isoformat(),
            "obligations": [
                {
                    "id": o.id,
                    "kind": o.kind,
                    "amount": money_json(o.amount),
                    "due_on": o.due_on.isoformat(),
                    "days_until_due": (o.due_on - self._snap.as_of).days,
                    "invoice_id": o.invoice_id,
                    "beneficiary_id": o.beneficiary_id,
                }
                for o in rows
            ],
        }
