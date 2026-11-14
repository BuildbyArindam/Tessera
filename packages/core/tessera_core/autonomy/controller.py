"""AutonomyController: decide, from evidence, whether the dial should move. It never moves it.

`assess` is pure: state + evidence + time + kill switch in, an `Assessment` out. Three outcomes:

  DEMOTE             any trigger fired (a policy violation, a coverage collapse, the kill switch). Automatic: the
                     caller applies it at once, with no human step, because losing authority is always the safe move.
  PROPOSE_PROMOTION  every criterion passed. This is an ESCALATE, not a grant: nothing changes until a person confirms
                     (and the caller re-assesses at confirmation, because evidence may have changed since).
  NONE               stay where we are. Insufficient evidence is a reason to stay, never a pass.

Which evidence counts: violations over the whole rolling window (one inside it blocks promotion); everything else only
from when the current level began, since evidence at a lower level does not vouch for a higher one. Demotion triggers
look only at what happened AFTER the current level began, so the demotion a trigger caused cannot fire again and again.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime

from tessera_core.autonomy.config import AutonomyConfig
from tessera_core.autonomy.evidence import AutonomyState, Evidence, in_window
from tessera_core.autonomy.stats import bootstrap_lower_bound, mean
from tessera_core.domain.base import DomainModel
from tessera_core.policy.context import AutonomyLevel

TRIGGER_VIOLATION = "violation"
TRIGGER_KILL_SWITCH = "kill_switch"
TRIGGER_COVERAGE_COLLAPSE = "coverage_collapse"


class TransitionKind(StrEnum):
    NONE = "NONE"
    PROPOSE_PROMOTION = "PROPOSE_PROMOTION"
    DEMOTE = "DEMOTE"


class Criterion(DomainModel):
    name: str
    passed: bool
    observed: str
    required: str


class Assessment(DomainModel):
    kind: TransitionKind
    from_level: AutonomyLevel
    to_level: AutonomyLevel
    triggers: tuple[str, ...]
    criteria: tuple[Criterion, ...]
    eligible: bool
    config_hash: str
    assessed_at: AwareDatetime


def _insufficient(have: int, need: int) -> tuple[str, str]:
    return f"n={have}", f"n >= {need}"


def _share(count: int, total: int) -> Decimal:
    """A share of counts as an exact Decimal, so no float ever meets a threshold."""
    return Decimal(count) / Decimal(total)


class AutonomyController:
    def __init__(self, config: AutonomyConfig) -> None:
        self.config = config

    # ---- criteria (promotion) -------------------------------------------------------------------
    def _criteria(self, state: AutonomyState, evidence: Evidence, now: AwareDatetime, kill: bool) -> list[Criterion]:
        cfg, win = self.config.promotion, self.config.window.evidence_days
        out = [
            Criterion(name="kill_switch_off", passed=not kill, observed="engaged" if kill else "off", required="off")
        ]

        violations = [
            v for v in evidence.violations if v.at >= now - timedelta(days=win)
        ]  # future-dated ones count too
        out.append(
            Criterion(name="zero_violations", passed=not violations, observed=str(len(violations)), required="0")
        )

        shadow = [s for s in evidence.shadow if in_window(s.at, now, win, state.since)]
        if len(shadow) < cfg.min_shadow_decisions:
            obs, req = _insufficient(len(shadow), cfg.min_shadow_decisions)
            out.append(Criterion(name="shadow_agreement", passed=False, observed=obs, required=req))
        else:
            rate = _share(sum(1 for s in shadow if s.agreed), len(shadow))
            out.append(
                Criterion(
                    name="shadow_agreement",
                    passed=rate >= cfg.min_agreement,
                    observed=f"{rate:.4f} (n={len(shadow)})",
                    required=f">= {cfg.min_agreement} with n >= {cfg.min_shadow_decisions}",
                )
            )

        out.append(self._saving(state, evidence, now))
        out.append(self._coverage(state, evidence, now))
        return out

    def _saving(self, state: AutonomyState, evidence: Evidence, now: AwareDatetime) -> Criterion:
        cfg = self.config.promotion
        days = self.config.window.evidence_days
        samples = [s.saving_per_10k for s in evidence.savings if in_window(s.at, now, days, state.since)]
        if len(samples) < cfg.min_saving_observations:
            obs, req = _insufficient(len(samples), cfg.min_saving_observations)
            return Criterion(name="saving_lower_bound", passed=False, observed=obs, required=req)
        boot = self.config.bootstrap
        bound = bootstrap_lower_bound(samples, resamples=boot.resamples, confidence=boot.confidence, seed=boot.seed)
        return Criterion(
            name="saving_lower_bound",
            passed=bound > cfg.min_saving_lower_bound,
            observed=f"{bound:.4f} (mean {mean(samples):.4f}, n={len(samples)})",
            required=f"> {cfg.min_saving_lower_bound} at {boot.confidence} confidence",
        )

    def _coverage(self, state: AutonomyState, evidence: Evidence, now: AwareDatetime) -> Criterion:
        cfg = self.config.promotion
        obs = [c for c in evidence.coverage if in_window(c.at, now, self.config.window.evidence_days, state.since)]
        if len(obs) < cfg.min_coverage_observations:
            have, need = _insufficient(len(obs), cfg.min_coverage_observations)
            return Criterion(name="coverage_within_tolerance", passed=False, observed=have, required=need)
        observed = _share(sum(1 for c in obs if c.covered), len(obs))
        target = mean([c.target for c in obs])
        return Criterion(
            name="coverage_within_tolerance",
            passed=abs(observed - target) <= cfg.coverage_tolerance,
            observed=f"{observed:.4f} vs target {target:.4f} (n={len(obs)})",
            required=f"gap <= {cfg.coverage_tolerance}",
        )

    # ---- triggers (demotion) --------------------------------------------------------------------
    def _triggers(self, state: AutonomyState, evidence: Evidence, now: AwareDatetime, kill: bool) -> dict[str, int]:
        demotion, fired = self.config.demotion, {}
        if kill:
            fired[TRIGGER_KILL_SWITCH] = demotion.on_kill_switch.levels_down
        if any(v.at > state.since for v in evidence.violations):  # a violation dated after `now` counts
            fired[TRIGGER_VIOLATION] = demotion.on_violation.levels_down
        step = demotion.on_coverage_collapse
        recent = [c for c in evidence.coverage if in_window(c.at, now, self.config.window.drift_days, state.since)]
        if len(recent) >= step.min_observations:
            observed = _share(sum(1 for c in recent if c.covered), len(recent))
            target = mean([c.target for c in recent])
            if observed < target - step.margin:
                fired[TRIGGER_COVERAGE_COLLAPSE] = step.levels_down
        return fired

    # ---- the decision ---------------------------------------------------------------------------
    def assess(
        self, state: AutonomyState, evidence: Evidence, now: AwareDatetime, *, kill_switch_engaged: bool
    ) -> Assessment:
        triggers = self._triggers(state, evidence, now, kill_switch_engaged)
        criteria = self._criteria(state, evidence, now, kill_switch_engaged)
        level = state.level
        eligible = not triggers and level < self.config.max_level and all(c.passed for c in criteria)

        if triggers:
            floor = AutonomyLevel(max(0, level.value - max(triggers.values())))
            kind = TransitionKind.DEMOTE if floor < level else TransitionKind.NONE
            target = floor
        elif eligible:
            kind, target = TransitionKind.PROPOSE_PROMOTION, AutonomyLevel(level.value + 1)
        else:
            kind, target = TransitionKind.NONE, level
        return Assessment(
            kind=kind,
            from_level=level,
            to_level=target,
            triggers=tuple(sorted(triggers)),
            criteria=tuple(criteria),
            eligible=eligible,
            config_hash=self.config.hash,
            assessed_at=now,
        )
