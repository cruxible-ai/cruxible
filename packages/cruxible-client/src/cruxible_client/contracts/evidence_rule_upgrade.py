"""Result of proposing the move to identity evidence rules (ClaimType v6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.get_reads import PlaybillGetCoordinate


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class EvidenceRuleUpgradeRequest(_Model):
    """Previews by default: the change set carries every dependent Claim."""

    tag: Literal["playbill-evidence-rule-upgrade-request-v1"] = (
        "playbill-evidence-rule-upgrade-request-v1"
    )
    dry_run: DryRun = None
    at: PreviewAt = None


class EvidenceRuleConversion(_Model):
    claim_type: str
    #: Accepted contract versions the converted rules admit that the exact rules did not.
    widened_versions: tuple[str, ...] = ()


class EvidenceRuleRefusal(_Model):
    claim_type: str
    reason: str


class EvidenceRuleUpgradeResult(_Model):
    tag: Literal["playbill-evidence-rule-upgrade-result-v1"] = (
        "playbill-evidence-rule-upgrade-result-v1"
    )
    #: ``would_propose``/``would_block`` answer a preview, which writes nothing.
    status: Literal["unchanged", "proposed", "blocked", "would_propose", "would_block"]
    proposal_id: str | None = None
    converted: tuple[EvidenceRuleConversion, ...] = ()
    refused: tuple[EvidenceRuleRefusal, ...] = ()
    carried_claims: int = 0
    detail: str | None = None
    #: The accepted coordinate the change set was evaluated at; pass it as ``at``
    #: to commit exactly this preview.
    coordinate: PlaybillGetCoordinate | None = None


__all__ = [
    "EvidenceRuleConversion",
    "EvidenceRuleRefusal",
    "EvidenceRuleUpgradeRequest",
    "EvidenceRuleUpgradeResult",
]
