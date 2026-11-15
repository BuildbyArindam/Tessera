"""The rules in force, read-only. Changing a rule means editing its YAML and reloading: there is no write path here."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from tessera_api.deps import AppContext, get_ctx, require_api_key
from tessera_api.schemas import PolicyOut, RuleOut

router = APIRouter(prefix="/policies", tags=["policies"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=PolicyOut)
def get_policies(ctx: AppContext = Depends(get_ctx)) -> PolicyOut:
    ruleset = ctx.ruleset
    return PolicyOut(
        ruleset_hash=ruleset.hash,
        rules=[
            RuleOut(
                id=rule.rule_id,
                type=rule.type_name,
                version=rule.type_version,
                params=rule.config.model_dump(mode="json"),
            )
            for rule in ruleset.rules
        ],
    )
