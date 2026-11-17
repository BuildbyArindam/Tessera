"""Stack scenarios about approvals and webhooks: replayed approvals, forged and replayed webhook deliveries.

Every attack here aims at an execution (a call that reaching the vendor) or a state change nobody authorised. Harm is
read from the vendor call log and the stored rows, never from a status code alone.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from tessera_core.domain import Currency
from tessera_core.policy import ForecastConfidence

from tessera_evalkit.redteam.scenario import Attempt
from tessera_evalkit.redteam.scn_support import (
    attack,
    event_body,
    outcome,
    pay,
    submitted_payout,
)  # fmt: skip

EARLY = {"obl": "OBL-0001"}  # ten days early to an established payee: escalated by early_payment_gate, nothing else


def _wide_forecast(ctx: Any) -> Any:
    widths = {**(ctx.forecast_confidence or {}), Currency.INR: ForecastConfidence(relative_width=Decimal(5))}
    return ctx.model_copy(update={"forecast_confidence": widths})


def _moved(st: Any) -> bool:
    return bool(st.moves)


# ---- replayed approvals ---------------------------------------------------------------------------------------------
@attack("RP-01", "replayed_approval", "Approve the same escalated payment twice", "ESCALATED")
def approve_twice(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", **EARLY))
        first, second = st.approve(p.proposal_id), st.approve(p.proposal_id)
        st.cycle()
        st.cycle()
        harmful = len(st.moves) > 1 or second == 200
        return Attempt(terminal="ESCALATED", harmful=harmful, stopped_by=f"lifecycle:{first}/{second}",
                       executed=tuple(m.describe() for m in st.moves))  # fmt: skip


@attack("RP-02", "replayed_approval", "Approve a BLOCKED proposal", "BLOCKED")
def approve_blocked(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", ben="BEN-002"))
        code = st.approve(p.proposal_id)
        st.cycle()
        return outcome(st, [p], harmful=_moved(st) or code == 200, stopped_by=f"lifecycle:{code}")


@attack("RP-03", "replayed_approval", "Approve a proposal a person already rejected", "BLOCKED")
def approve_after_reject(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", **EARLY))
        st.reject(p.proposal_id)
        code = st.approve(p.proposal_id)
        st.cycle()
        return outcome(st, [p], harmful=_moved(st) or code == 200, stopped_by=f"lifecycle:{code}")


@attack("RP-04", "replayed_approval", "Plan a rejected proposal again to revive it", "BLOCKED")
def replan_rejected(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", **EARLY))
        st.reject(p.proposal_id)
        again = st.plan(pay("100000.00", **EARLY))
        st.cycle()
        return outcome(st, again or [p], harmful=_moved(st) or st.status(p.proposal_id) != "REJECTED",
                       stopped_by="lifecycle:rejected_stays_rejected")  # fmt: skip


@attack(
    "RP-05", "replayed_approval", "Approval for one rule reused after a second rule starts to escalate", "ESCALATED"
)
def approval_covers_one_rule(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", **EARLY))
        st.approve(p.proposal_id)
        st.change_world(_wide_forecast)
        st.cycle()
        return outcome(st, [p], harmful=_moved(st), stopped_by="gate:approval_does_not_cover_new_rule")


@attack("RP-06", "replayed_approval", "Approve again after the payment was sent, hoping to send it twice", "BLOCKED")
def approve_after_send(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00"))
        st.cycle()
        code = st.approve(p.proposal_id)
        st.cycle()
        return outcome(st, [p], harmful=len(st.moves) > 1, terminal="BLOCKED", stopped_by=f"lifecycle:{code}")


@attack("RP-07", "replayed_approval", "Run the worker's execution step three times for one approval", "NO_EFFECT")
def execute_three_times(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00"))
        labels = [st.execute(p.proposal_id) for _ in range(3)]
        return outcome(
            st, [p], harmful=len(st.moves) > 1, terminal="NO_EFFECT", stopped_by="idempotency:" + ",".join(labels)
        )


@attack(
    "RP-08",
    "replayed_approval",
    "Human-approved payment, then the kill switch is pulled before the worker runs",
    "BLOCKED",
)
def approved_then_killed(rt: Any) -> Attempt:
    with rt.stack() as st:
        [p] = st.plan(pay("100000.00", **EARLY))
        st.approve(p.proposal_id)
        st.set_kill_switch(True)
        st.cycle()
        return outcome(st, [p], harmful=_moved(st), stopped_by="policy:kill_switch (at execution)")


# ---- forged and replayed webhooks -----------------------------------------------------------------------------------
def _forge(rt: Any, label: str, deliver: Any) -> Attempt:
    with rt.stack() as st:
        pid, request_id = submitted_payout(st)
        code, _ = deliver(st, event_body(request_id))
        changed = st.status(pid) != "SUBMITTED" or st.executions_count() != 1
        if changed:
            return Attempt(terminal="EXECUTED", harmful=True, note=f"{label}: state changed (HTTP {code})")
        return Attempt(terminal="BLOCKED", stopped_by=f"edge:{code}", note=label)


@attack("FW-01", "forged_webhook", "No signature headers at all", "BLOCKED")
def fw_unsigned(rt: Any) -> Attempt:
    return _forge(rt, "unsigned", lambda st, body: st.webhook(body, headers={}))


@attack("FW-02", "forged_webhook", "Signed with a guessed secret", "BLOCKED")
def fw_wrong_secret(rt: Any) -> Attempt:
    return _forge(rt, "wrong secret", lambda st, body: st.webhook(body, secret="whsec-guess-000000000"))


@attack("FW-03", "forged_webhook", "Genuine signature, body altered afterwards (paid -> failed)", "BLOCKED")
def fw_tampered(rt: Any) -> Attempt:
    def deliver(st: Any, body: bytes) -> Any:
        return st.webhook(body, altered=body.replace(b"paid", b"failed"))  # signed as sent, altered in transit

    return _forge(rt, "tampered body", deliver)


@attack("FW-04", "forged_webhook", "Captured genuine delivery replayed an hour later", "BLOCKED")
def fw_stale(rt: Any) -> Attempt:
    return _forge(rt, "stale", lambda st, body: st.webhook(body, at=st.now - timedelta(hours=1)))


@attack("FW-05", "forged_webhook", "Delivery dated one hour in the future", "BLOCKED")
def fw_future(rt: Any) -> Attempt:
    return _forge(rt, "future", lambda st, body: st.webhook(body, at=st.now + timedelta(hours=1)))


@attack("FW-06", "forged_webhook", "A 1.5 MB body", "BLOCKED")
def fw_huge(rt: Any) -> Attempt:
    return _forge(rt, "oversize", lambda st, body: st.webhook(body + b" " * 1_500_000))


@attack("FW-07", "forged_webhook", "Signature present, timestamp header missing", "BLOCKED")
def fw_no_timestamp(rt: Any) -> Attempt:
    return _forge(rt, "no timestamp", lambda st, body: st.webhook(body, headers={"x-signature": "0" * 64}))


@attack("FW-08", "forged_webhook", "Full-width digits in the timestamp header", "BLOCKED")
def fw_unicode_stamp(rt: Any) -> Attempt:
    stamp = "".join(chr(ord(c) + 0xFEE0) for c in "1772442000000")
    return _forge(
        rt,
        "unicode timestamp",
        lambda st, body: st.webhook(body, headers={"x-timestamp": stamp.encode(), "x-signature": "0" * 64}),
    )  # type: ignore[dict-item]


@attack("FW-09", "forged_webhook", "Signature computed over the body only, without the timestamp", "BLOCKED")
def fw_body_only(rt: Any) -> Attempt:
    import hashlib
    import hmac

    def deliver(st: Any, body: bytes) -> Any:
        sig = hmac.new(b"whsec-redteam-0123456789", body, hashlib.sha256).hexdigest()
        return st.webhook(body, headers={"x-timestamp": str(int(st.now.timestamp() * 1000)), "x-signature": sig})

    return _forge(rt, "body-only signature", deliver)
