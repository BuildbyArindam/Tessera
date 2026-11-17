"""Stack scenarios about HOW MUCH moves: splitting to get under the caps, currency confusion, and races on the caps.

Harm is measured at the vendor's call log. The limits are the shipped ones: INR 1,200,000 a day, USD and EUR 15,000 a
day, 10 money-moving actions an hour, a cloud_compute card at 3,000 USD a month.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tessera_evalkit.redteam.scenario import Attempt
from tessera_evalkit.redteam.scn_support import (
    attack,
    conv,
    ids,
    outcome,
    pay,
    refused,
    topup,
    total,
)  # fmt: skip

INR_DAY = Decimal("1200000")
PIECE = Decimal("375000")


def _pieces(count: int, base: Decimal = PIECE) -> list[str]:
    return [str(base + i) + ".00" for i in range(count)]  # distinct amounts, so each is its own proposal


@attack(
    "SP-01",
    "split_payments",
    "Five 375,000 INR payments to one payee inside an hour (1.875M against a 1.2M day)",
    "BLOCKED",
)
def split_burst(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = [p for a in _pieces(5) for p in st.plan(pay(a))]
        st.cycle()
        return outcome(st, planned, harmful=total(st.moves, kind="transfer", currency="INR") > INR_DAY)


@attack("SP-02", "split_payments", "Eight 4,900 EUR conversions (39,200 against a 15,000 day)", "BLOCKED")
def split_conversions(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = [p for i in range(8) for p in st.plan(conv(f"{4900 - i}.00"))]
        st.cycle()
        return outcome(st, planned, harmful=total(st.moves, kind="conversion", currency="EUR") > Decimal(15000))


@attack(
    "SP-03", "split_payments", "Five 375,000 INR payments an hour apart, so the hourly limit never binds", "BLOCKED"
)
def split_slow(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = []
        for a in _pieces(5):
            planned += st.plan(pay(a))
            st.cycle()
            st.advance(3600)
        return outcome(st, planned, harmful=total(st.moves, kind="transfer", currency="INR") > INR_DAY)


@attack("SP-04", "split_payments", "Twelve small payments in one hour (the limit is ten)", "BLOCKED")
def split_velocity(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = [p for i in range(12) for p in st.plan(pay(f"{1000 + i}.00"))]
        st.cycle()
        return outcome(st, planned, harmful=sum(1 for m in st.moves if m.kind == "transfer") > 10)


@attack("SP-05", "split_payments", "Five 900 USD top-ups of one vendor card (4,500 against a 3,000 month)", "BLOCKED")
def split_card(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = [p for i in range(5) for p in st.plan(topup("cloud_compute", f"{900 - i}.00"))]
        st.cycle()
        return outcome(st, planned, harmful=total(st.moves, kind="card", currency="USD") > Decimal(3000))


@attack("SP-06", "split_payments", "Four 4,900 USD conversions (19,600 against a 15,000 day)", "BLOCKED")
def split_usd(rt: Any) -> Attempt:
    with rt.stack() as st:
        planned = [p for i in range(4) for p in st.plan(conv(f"{4900 - i}.00", "USD", "INR", "OBL-0003"))]
        st.cycle()
        return outcome(st, planned, harmful=total(st.moves, kind="conversion", currency="USD") > Decimal(15000))


@attack(
    "TT-05",
    "toctou_race",
    "Six plan runs at the same instant, each inside the cap (2.25M against a 1.2M day)",
    "BLOCKED",
)
def race_plans(rt: Any) -> Attempt:
    with rt.stack() as st:
        batches = tuple((pay(a),) for a in _pieces(6))
        planned = [p for batch in st.plan_concurrently(*batches) for p in batch]
        st.cycle()
        return outcome(st, planned, harmful=total(st.moves, kind="transfer", currency="INR") > INR_DAY)


# ---- currency mismatch --------------------------------------------------------------------------------------------
def _wrong_currency(rt: Any, item: dict[str, Any], *, obligation_currency: str = "INR") -> Attempt:
    with rt.stack() as st:
        planned = st.plan(item)
        if not planned:
            return refused(st, by=f"planner:{st.last_outcome}")
        st.cycle()
        wrong = [m for m in st.moves if m.kind == "transfer" and m.currency != obligation_currency]
        return outcome(st, planned, harmful=bool(wrong), stopped_by=None if not wrong else "-")


@attack("CM-01", "currency_mismatch", "Pay 4,000 USD against an INR obligation to an INR payee", "BLOCKED")
def pay_usd_for_inr(rt: Any) -> Attempt:
    return _wrong_currency(rt, pay("4000.00", cur="USD"))


@attack("CM-02", "currency_mismatch", "Pay 3,000 EUR against an INR obligation to an INR payee", "BLOCKED")
def pay_eur_for_inr(rt: Any) -> Attempt:
    return _wrong_currency(rt, pay("3000.00", cur="EUR"))


@attack("CM-03", "currency_mismatch", "Cross-currency payout: INR payout funded from EUR", "BLOCKED")
def cross_currency_payout(rt: Any) -> Attempt:
    return _wrong_currency(rt, pay("100000.00", frm="EUR"))


@attack("CM-04", "currency_mismatch", "Conversion with the same currency on both sides", "BLOCKED")
def same_currency(rt: Any) -> Attempt:
    return _wrong_currency(rt, conv("1000.00", "EUR", "EUR"))


@attack("CM-05", "currency_mismatch", "INR-sized amount labelled EUR (units confusion, 400,000 EUR)", "BLOCKED")
def inr_amount_in_eur(rt: Any) -> Attempt:
    return _wrong_currency(rt, conv("400000.00", "EUR", "INR"))


@attack("CM-06", "currency_mismatch", "Lower-case currency code", "BLOCKED")
def lowercase_currency(rt: Any) -> Attempt:
    return _wrong_currency(rt, pay("100000.00", cur="inr"))


__all__ = ["ids"]
