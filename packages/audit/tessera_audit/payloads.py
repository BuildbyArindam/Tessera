"""Builders for audit payloads, so every emitter writes the same shape.

They take plain values or domain objects and return a dict. Nothing here touches a database. Payloads hold what a
reviewer needs to replay a decision: the inputs, the prompt and ruleset hashes, and the verdict. They never hold
secrets (API keys, account numbers): none of those ever reach these functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date
from typing import Any

from tessera_core.autonomy import Assessment, AutonomyLevel, is_shadow_decision
from tessera_core.domain import PolicyDecision, Proposal
from tessera_core.forecast import ForecastResult
from tessera_core.policy import PolicyContext


def forecast_payload(result: ForecastResult, as_of: date) -> dict[str, Any]:
    return {
        "business_id": result.business_id,
        "currency": result.currency,
        "horizon_weeks": result.horizon_weeks,
        "as_of": as_of,
        "point": result.point,
        "lower": result.lower,
        "upper": result.upper,
        "target_coverage": result.target_coverage,
        "model_version": result.model_version,
    }


def proposal_payload(proposal: Proposal, business_id: str, plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """The proposal as the model wrote it, plus the facts that tie it to one planning run."""
    plan_summary: dict[str, Any] | None = None
    if plan is not None:
        raw = plan.get("raw_output")
        flags = plan.get("input_flags") or ()
        plan_summary = {
            "model": plan.get("model"),
            "prompt_name": plan.get("prompt_name"),
            "prompt_hash": plan.get("prompt_hash"),
            "context_hash": plan.get("context_hash"),
            "temperature": plan.get("temperature"),
            "outcome": plan.get("outcome"),
            "attempts": plan.get("attempts"),
            "evidence_ids": plan.get("evidence_ids"),
            "untrusted_sha256": plan.get("untrusted_sha256"),
            "input_flags": sorted({str(f.get("code")) for f in flags if isinstance(f, Mapping)}),
            "raw_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if isinstance(raw, str) else None,
        }
    return {
        "business_id": business_id,
        "proposal": {
            "id": proposal.id,
            "action_type": proposal.action_type,
            "params": proposal.params,
            "rationale": proposal.rationale,
            "confidence": proposal.confidence,
            "created_at": proposal.created_at,
        },
        "plan": plan_summary,
    }


def decision_payload(
    proposal_id: str, decision: PolicyDecision, context: PolicyContext | None, *, phase: str = "plan"
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "phase": phase,
        "verdict": decision.verdict,
        "reasons": list(decision.reasons),
        "rule_ids": list(decision.rule_ids),
        "ruleset_hash": decision.ruleset_hash,
        # Day 14: stopped only because the autonomy level does not allow the action. It never runs; a person sees it.
        "shadow": is_shadow_decision(decision),
        # What the engine saw. With the ruleset hash and the proposal this reproduces the verdict.
        "context": None if context is None else context.model_dump(mode="json"),
    }


def human_decision_payload(proposal_id: str, *, actor: str, note: str | None, from_status: str) -> dict[str, Any]:
    return {"proposal_id": proposal_id, "actor": actor, "note": note, "from_status": from_status}


def approval_requested_payload(proposal_id: str, decision: PolicyDecision) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "rule_ids": list(decision.rule_ids),
        "reasons": list(decision.reasons),
        "ruleset_hash": decision.ruleset_hash,
    }


def execution_submitted_payload(
    proposal_id: str, *, execution_id: int, idempotency_key: str, action_type: str, external_id: str | None
) -> dict[str, Any]:
    """The vendor accepted the request. The key is the one stored before the call, so a reviewer can match them."""
    return {
        "proposal_id": proposal_id,
        "execution_id": execution_id,
        "idempotency_key": idempotency_key,
        "action_type": action_type,
        "external_id": external_id,
    }


def execution_result_payload(
    proposal_id: str, *, execution_id: int, outcome: str, source: str, detail: Mapping[str, Any] | None
) -> dict[str, Any]:
    """How an execution ended. `source` is where the news came from: adapter, webhook or worker."""
    return {
        "proposal_id": proposal_id,
        "execution_id": execution_id,
        "outcome": outcome,
        "source": source,
        "detail": None if detail is None else dict(detail),
    }


def webhook_received_payload(
    *,
    event_id: str,
    name: str,
    resource_id: str | None,
    request_id: str | None,
    event_created_at: Any,
    outcome: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """The event envelope and what Tessera did with it. The body is never stored: it can carry bank details."""
    return {
        "event_id": event_id,
        "name": name,
        "resource_id": resource_id,
        "request_id": request_id,
        "event_created_at": event_created_at,
        "outcome": outcome,
        "execution_id": execution_id,
    }


# What an `autonomy_changed` event records. `level_changed` is false for a proposal or a withdrawal (no move).
AUTONOMY_KINDS = ("promotion_proposed", "promotion_confirmed", "promotion_rejected", "promotion_cancelled", "demoted")

LEVEL_MOVING_KINDS = ("promotion_confirmed", "demoted")  # the only kinds that actually move the dial


def autonomy_changed_payload(
    *,
    kind: str,
    from_level: AutonomyLevel,
    to_level: AutonomyLevel,
    actor: str,
    triggers: tuple[str, ...] = (),
    promotion_id: int | None = None,
    assessment: Assessment | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """A change to the autonomy dial, or a step toward one. The assessment carries the criteria and the config hash."""
    if kind not in AUTONOMY_KINDS:
        raise ValueError(f"unknown autonomy change kind {kind!r}")
    return {
        "kind": kind,
        "from_level": f"L{from_level.value}",
        "to_level": f"L{to_level.value}",
        "level_changed": kind in LEVEL_MOVING_KINDS,
        "actor": actor,  # "controller" for an automatic move, "operator" for a person
        "triggers": list(triggers),
        "promotion_id": promotion_id,
        "note": note,
        "assessment": None if assessment is None else assessment.model_dump(mode="json"),
    }
