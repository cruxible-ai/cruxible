"""Result of proposing the move to identity evidence rules (ClaimType v6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class EvidenceRuleConversionV1(_Model):
    claim_type: str
    #: Accepted contract versions the converted rules admit that the exact rules did not.
    widened_versions: tuple[str, ...] = ()


class EvidenceRuleRefusalV1(_Model):
    claim_type: str
    reason: str


class EvidenceRuleUpgradeResultV1(_Model):
    tag: Literal["playbill-evidence-rule-upgrade-result-v1"] = (
        "playbill-evidence-rule-upgrade-result-v1"
    )
    status: Literal["unchanged", "proposed", "blocked"]
    proposal_id: str | None = None
    converted: tuple[EvidenceRuleConversionV1, ...] = ()
    refused: tuple[EvidenceRuleRefusalV1, ...] = ()
    carried_claims: int = 0
    detail: str | None = None


__all__ = ["EvidenceRuleConversionV1", "EvidenceRuleRefusalV1", "EvidenceRuleUpgradeResultV1"]
