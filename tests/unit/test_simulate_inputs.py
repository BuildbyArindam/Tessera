"""Day 8: refusing bad inputs, plus the small pieces (series, bootstrap, config, summary, purity)."""

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from policy_helpers import make_proposal
from pydantic import ValidationError
from sim_helpers import PAIR, business_days, convert, make_series, pay
from tessera_core.domain import ActionType, Currency, Money, SimulationSummary
from tessera_core.simulate import (
    FxSeries,
    SimulationConfig,
    SimulationInputError,
    block_bootstrap_log_returns,
    replay,
    simulate,
)

SERIES = make_series()
AS_OF = business_days(800)[600]
DUE = AS_OF + timedelta(days=30)
HIST = {PAIR: SERIES}


def run(proposal=None, **overrides):  # type: ignore[no-untyped-def]
    args = {"proposal": proposal or convert(), "fx_history": HIST, "as_of": AS_OF, "n_paths": 1000, "seed": 1}
    args.update({"due_date": DUE, **overrides})
    return simulate(
        args.pop("proposal"), args.pop("fx_history"), args.pop("as_of"), args.pop("n_paths"), args.pop("seed"), **args
    )


# ---- proposals the simulator refuses ---------------------------------------------------------
@pytest.mark.parametrize("action", [ActionType.TOPUP_CARD, ActionType.FLAG_INVOICE])
def test_actions_without_an_fx_decision_are_refused(action: ActionType) -> None:
    with pytest.raises(SimulationInputError, match="no FX exposure"):
        run(make_proposal(action, "10.00", "USD", vendor_id="workspace_suite"))


@pytest.mark.parametrize(
    "params",
    [
        {"amount": "1e3", "currency": "USD", "to_currency": "EUR"},
        {"amount": 100.5, "currency": "USD", "to_currency": "EUR"},
        {"amount": "-5.00", "currency": "USD", "to_currency": "EUR"},
        {"amount": "10.001", "currency": "USD", "to_currency": "EUR"},
        {"amount": "10.00", "currency": "XYZ", "to_currency": "EUR"},
        {"amount": "10.00", "currency": "USD"},
        {"amount": "10.00", "currency": "USD", "to_currency": "USD"},
        {"currency": "USD", "to_currency": "EUR"},
    ],
)
def test_malformed_convert_params_are_refused_with_a_typed_error(params: dict[str, object]) -> None:
    from tessera_core.domain import Proposal

    p = Proposal(id="p1", action_type=ActionType.CONVERT, params=params, rationale="x", confidence=0.5,
                 created_at=datetime(2026, 1, 1, tzinfo=UTC))  # fmt: skip
    with pytest.raises(SimulationInputError):
        run(p)


def test_pay_supplier_needs_a_funding_currency_unless_it_needs_no_conversion() -> None:
    with pytest.raises(SimulationInputError, match="from_currency"):
        run(pay(funded_from=None))
    same = run(pay(payout="USD", funded_from="USD"))
    assert same.pair is None and same.notes == ("no_fx_conversion_needed",)
    assert same.worst_case_loss == Money.zero(Currency.USD) and same.summary().worst_case_loss.is_zero()


def test_a_fee_that_swallows_the_amount_is_refused() -> None:
    with pytest.raises(SimulationInputError, match="fee"):
        run(convert("5.00"), config=SimulationConfig(fee="5.00"))


# ---- dates, history, run size ------------------------------------------------------------------
def test_dates_are_validated() -> None:
    with pytest.raises(SimulationInputError, match="before as_of"):
        run(due_date=AS_OF - timedelta(days=1))
    with pytest.raises(SimulationInputError, match="365"):
        run(due_date=AS_OF + timedelta(days=366))
    with pytest.raises(SimulationInputError, match="not a datetime"):
        run(as_of=datetime(2025, 4, 21, tzinfo=UTC))
    assert run(due_date=AS_OF + timedelta(days=365)).steps == 252


def test_history_must_exist_cover_the_pair_and_be_fresh_and_long_enough() -> None:
    with pytest.raises(SimulationInputError, match="no FX history supplied for USD/EUR"):
        run(fx_history={"USD/GBP": SERIES})
    with pytest.raises(SimulationInputError, match="labelled"):
        run(fx_history={PAIR: make_series(pair="USD/GBP")})
    with pytest.raises(SimulationInputError, match="more than 7 days"):
        run(as_of=SERIES.last_day + timedelta(days=30), due_date=SERIES.last_day + timedelta(days=60))
    before_start = business_days(800)[0] - timedelta(days=1)
    with pytest.raises(SimulationInputError, match="no USD/EUR observation"):
        run(as_of=before_start, due_date=before_start + timedelta(days=30))
    with pytest.raises(SimulationInputError, match="at least 60"):
        run(as_of=business_days(800)[30], due_date=business_days(800)[30] + timedelta(days=30))


@pytest.mark.parametrize(("n_paths", "seed"), [(99, 1), (200_001, 1), (1000, -1)])
def test_run_size_and_seed_are_validated(n_paths: int, seed: int) -> None:
    with pytest.raises(SimulationInputError):
        run(n_paths=n_paths, seed=seed)


def test_replay_refuses_a_due_date_beyond_the_data() -> None:
    with pytest.raises(SimulationInputError, match="more than 7 days"):
        replay(convert(), HIST, AS_OF, due_date=SERIES.last_day + timedelta(days=60))


# ---- FxSeries ----------------------------------------------------------------------------------
def test_fx_series_rejects_bad_data() -> None:
    d = business_days(3)
    with pytest.raises(SimulationInputError, match="strictly increasing"):
        FxSeries.from_observations(PAIR, [(d[0], 1.0), (d[0], 1.1)])
    with pytest.raises(SimulationInputError, match="strictly positive"):
        FxSeries.from_observations(PAIR, [(d[0], 1.0), (d[1], 0.0)])
    with pytest.raises(SimulationInputError, match="finite"):
        FxSeries.from_observations(PAIR, [(d[0], 1.0), (d[1], float("nan"))])
    with pytest.raises(SimulationInputError, match="pair"):
        FxSeries.from_observations("USDEUR", [(d[0], 1.0)])
    with pytest.raises(SimulationInputError, match="different codes"):
        FxSeries.from_observations("USD/USD", [(d[0], 1.0)])
    with pytest.raises(SimulationInputError, match="at least one"):
        FxSeries.from_observations(PAIR, [])


def test_fx_series_up_to_and_latest_on_or_before() -> None:
    cut = SERIES.up_to(AS_OF)
    assert cut.last_day == AS_OF and len(cut) == 601 and len(SERIES) == 800
    saturday = AS_OF + timedelta(days=(5 - AS_OF.weekday()) % 7 or 7)
    assert SERIES.latest_on_or_before(saturday)[0] <= saturday  # a weekend resolves to the Friday before
    assert not SERIES.values.flags.writeable  # read-only: a caller cannot edit it after validation


# ---- bootstrap ---------------------------------------------------------------------------------
def test_bootstrap_shapes_zero_steps_and_block_bounds() -> None:
    returns = np.random.default_rng(0).normal(0, 0.01, 300)
    rng = np.random.default_rng(1)
    assert block_bootstrap_log_returns(returns, 0, 50, 5, rng).tolist() == [0.0] * 50
    assert block_bootstrap_log_returns(returns, 13, 50, 5, rng).shape == (50,)  # 3 blocks cut to 13 returns
    with pytest.raises(SimulationInputError):
        block_bootstrap_log_returns(returns, 10, 50, 301, rng)
    with pytest.raises(SimulationInputError):
        block_bootstrap_log_returns(returns, 10, 50, 0, rng)


def test_bootstrap_paths_are_sums_of_consecutive_runs_of_the_input() -> None:
    returns = np.arange(40, dtype=float)  # distinct, so a sum identifies its blocks
    out = block_bootstrap_log_returns(returns, 6, 400, 3, np.random.default_rng(2), demean=False)
    allowed = {float(3 * a + 3 + 3 * b + 3) for a in range(38) for b in range(38)}  # sum(a..a+2) + sum(b..b+2)
    assert set(out.tolist()) <= allowed


def test_bootstrap_can_start_a_block_at_the_first_and_the_last_position() -> None:
    returns = np.arange(40, dtype=float)
    out = block_bootstrap_log_returns(returns, 3, 20_000, 3, np.random.default_rng(4), demean=False)
    assert out.min() == 3.0 and out.max() == 3.0 * 37 + 3  # blocks [0,1,2] and [37,38,39]


def test_bootstrap_demean_removes_drift_and_keeps_it_when_asked() -> None:
    returns = np.random.default_rng(0).normal(0.001, 0.005, 500)  # +0.1% a day
    rng = np.random.default_rng(3)
    centred = block_bootstrap_log_returns(returns, 20, 20_000, 5, rng, demean=True)
    drifting = block_bootstrap_log_returns(returns, 20, 20_000, 5, rng, demean=False)
    assert abs(centred.mean()) < 4 * centred.std() / np.sqrt(20_000)
    assert 0.015 < drifting.mean() < 0.025  # about 20 x 0.001


# ---- config and summary ------------------------------------------------------------------------
BAD_CONFIGS: list[dict[str, object]] = [
    {"spread_bps": 0.5},
    {"spread_bps": "10000"},
    {"spread_bps": "-1"},
    {"fee": -1},
    {"fee": 1.5},
    {"fee": "NaN"},
    {"block_length": 0},
    {"block_length": 61},
    {"x": 1},
]


@pytest.mark.parametrize("bad", BAD_CONFIGS)
def test_config_rejects_floats_out_of_range_and_unknown_keys(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SimulationConfig(**bad)  # type: ignore[arg-type]


def test_summary_carries_identity_and_rejects_inconsistent_values() -> None:
    r = run()
    s = r.summary()
    assert s.proposal_id == "p1" and s.action_type is ActionType.CONVERT and s.n_paths == 1000
    assert s.amount == Money(amount="10000.00", currency=Currency.USD)
    assert s.worst_case_loss == r.worst_case_loss and not s.worst_case_loss.is_negative()
    usd, eur = Money(amount="1.00", currency=Currency.USD), Money(amount="1.00", currency=Currency.EUR)
    with pytest.raises(ValidationError):
        SimulationSummary(
            proposal_id="p",
            action_type=ActionType.CONVERT,
            amount=usd,
            worst_case_loss=-usd,
            expected_saving=usd,
            n_paths=1,
        )
    with pytest.raises(ValidationError):
        SimulationSummary(
            proposal_id="p",
            action_type=ActionType.CONVERT,
            amount=usd,
            worst_case_loss=usd,
            expected_saving=eur,
            n_paths=1,
        )


# ---- purity: no network, database, LLM, clock, randomness outside the seeded generator -------------
FORBIDDEN_IMPORTS = {"requests", "httpx", "urllib", "socket", "http", "sqlalchemy", "psycopg", "psycopg2", "openai",
                     "anthropic", "tessera_airwallex", "tessera_agent", "tessera_audit", "time", "random", "os",
                     "subprocess", "asyncio"}  # fmt: skip
SIM_DIR = Path(__file__).resolve().parents[2] / "packages/core/tessera_core/simulate"


def test_simulate_package_is_pure() -> None:
    offences: list[str] = []
    for path in sorted(SIM_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            offences += [f"{path.name}: import {n}" for n in names if n.split(".")[0] in FORBIDDEN_IMPORTS]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "compile", "__import__"}
            ):
                offences.append(f"{path.name}: {node.func.id}()")
            if isinstance(node, ast.Attribute) and node.attr in {"now", "today", "utcnow"}:
                offences.append(f"{path.name}: .{node.attr}")
    assert offences == []
    assert max(len(p.read_text().splitlines()) for p in SIM_DIR.glob("*.py")) < 300


def test_money_stays_decimal_in_every_report_field() -> None:
    r = run()
    for field in (
        "expected_saving",
        "saving_std",
        "saving_p05",
        "saving_p50",
        "worst_case_loss",
        "spread_cost",
        "fee_cost",
    ):
        assert isinstance(getattr(r, field).amount, Decimal)
