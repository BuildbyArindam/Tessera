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
