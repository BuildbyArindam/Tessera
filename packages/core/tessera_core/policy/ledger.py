"""The ledger: what has ALREADY been approved, folded from the system of record into the cumulative-limit inputs.

`daily_cumulative_cap`, `velocity_limit` and `card_limit_cap` judge a proposal against totals ("spent today", "approved
in the last hour", "loaded this month"). Those totals are context, and context comes from a world provider. A provider
that reports empty totals (a stub, a bug, a stale cache, a fixture left in place) turns every one of those rules into
a rule that never binds, and splitting one large payment into capped pieces then walks straight through. The Day 18
red team did exactly that.

So the totals have one authority: the approved proposals themselves. `fold` computes them from plain entries (the
caller reads the entries from the database), and `merge_into` applies them to a context without ever lowering a total
the provider already knew about: the provider may add to the ledger, never subtract from it.

Pure: no I/O, no clock. The caller supplies `now`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.proposal_view import MONEY_MOVING_ACTIONS, ProposalFormatError, read_amount

VELOCITY_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class LedgerEntry:
    """One money-moving proposal that was approved (by policy or by a person) and has not failed or been rejected."""

    proposal: Proposal
    approved_at: datetime


@dataclass(frozen=True)
class Ledger:
    spend_today: dict[Currency, Money]
    recent_approval_times: tuple[datetime, ...]
    card_loaded_this_month: dict[str, Money]


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def fold(entries: Iterable[LedgerEntry], *, now: datetime) -> Ledger:
    """Totals as of `now`. An entry whose amount cannot be read is skipped: policy blocks an unreadable amount, so no
    such row can have been approved, and there is no amount to add."""
    today, month, hour = day_start(now), month_start(now), now - VELOCITY_WINDOW
    spend: dict[Currency, Money] = {}
    cards: dict[str, Money] = {}
    times: list[datetime] = []
    for entry in entries:
        proposal = entry.proposal
        if proposal.action_type not in MONEY_MOVING_ACTIONS:
            continue
        try:
            amount = read_amount(proposal)
        except ProposalFormatError:
            continue  # unreadable rows were never approvable; they carry no amount to add
        if entry.approved_at >= today:
            spend[amount.currency] = amount if amount.currency not in spend else spend[amount.currency] + amount
        if entry.approved_at > hour:
            times.append(entry.approved_at)
        if proposal.action_type is ActionType.TOPUP_CARD and entry.approved_at >= month:
            vendor = proposal.params.get("vendor_id")
            if isinstance(vendor, str):
                cards[vendor] = amount if vendor not in cards else cards[vendor] + amount
    return Ledger(spend_today=spend, recent_approval_times=tuple(sorted(times)), card_loaded_this_month=cards)


K = TypeVar("K")


def _larger(known: Mapping[K, Money] | None, ledger: Mapping[K, Money]) -> dict[K, Money]:
    merged: dict[K, Money] = dict(known or {})
    for key, amount in ledger.items():
        if key not in merged or merged[key].amount < amount.amount:
            merged[key] = amount
    return merged


def _inside(times: Iterable[datetime], now: datetime) -> int:
    return sum(1 for t in times if t > now - VELOCITY_WINDOW)


def merge_into(context: PolicyContext, ledger: Ledger, *, now: datetime) -> PolicyContext:
    """The context with the ledger applied: totals are the larger of what the provider said and what the ledger shows,
    the approval times are whichever list has more entries inside the hour, and `now` is the caller's."""
    provided = context.recent_approval_times or ()
    mine = _inside(ledger.recent_approval_times, now)
    times = ledger.recent_approval_times if mine >= _inside(provided, now) else provided
    return context.model_copy(
        update={
            "now": now,
            "spend_today": _larger(context.spend_today, ledger.spend_today),
            "card_loaded_this_month": _larger(context.card_loaded_this_month, ledger.card_loaded_this_month),
            "recent_approval_times": tuple(times),
        }
    )


__all__ = ["Ledger", "LedgerEntry", "day_start", "fold", "merge_into", "month_start"]
