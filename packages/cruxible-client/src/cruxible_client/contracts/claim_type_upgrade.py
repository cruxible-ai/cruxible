"""Proposing the move of accepted ClaimTypes to ClaimType v7, as one reviewed change set."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.claim_types import RevisionEvidence
from cruxible_client.contracts.get_reads import PlaybillGetCoordinate


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ClaimTypeUpgradeRequest(_Model):
    """Which ClaimTypes move to v7, and what their statement-changing revisions keep.

    Every ClaimType before v7 accumulates evidence across revisions; the upgrade
    states the rule explicitly and defaults to ``replace``, so a revision that
    changes what it states carries exactly the evidence it cites.
    """

    tag: Literal["playbill-claim-type-upgrade-request-v1"] = (
        "playbill-claim-type-upgrade-request-v1"
    )
    #: Predicates to upgrade; empty upgrades every live ClaimType before v7.
    claim_types: tuple[str, ...] = ()
    revision_evidence: RevisionEvidence = "replace"
    #: Previews by default: the change set carries every dependent Claim.
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("claim_types")
    @classmethod
    def _claim_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(item.removeprefix("ClaimType:") for item in value)
        return tuple(sorted(set(names), key=lambda item: item.encode("utf-8")))


class ClaimTypeUpgrade(_Model):
    """One ClaimType moved to v7, and the one meaning that changes with it."""

    claim_type: str
    from_format: Literal[
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
        "playbill-claim-type-v5",
        "playbill-claim-type-v6",
    ]
    #: Every ClaimType before v7 accumulates.
    revision_evidence_before: Literal["accumulate"] = "accumulate"
    revision_evidence_after: RevisionEvidence
    #: Kept: every ClaimType before v7 means ``self``.
    evidence_requirement: Literal["self"] = "self"
    #: Contract versions a converted exact-digest rule (v1-v5) now also admits.
    widened_versions: tuple[str, ...] = ()


class ClaimTypeUpgradeRefusal(_Model):
    claim_type: str
    reason: str


class ClaimTypeUpgradeResult(_Model):
    tag: Literal["playbill-claim-type-upgrade-result-v1"] = "playbill-claim-type-upgrade-result-v1"
    #: ``would_propose``/``would_block`` answer a dry run, which writes nothing.
    status: Literal["unchanged", "proposed", "blocked", "would_propose", "would_block"]
    proposal_id: str | None = None
    upgraded: tuple[ClaimTypeUpgrade, ...] = ()
    #: ClaimTypes already at v7.
    unchanged: tuple[str, ...] = ()
    refused: tuple[ClaimTypeUpgradeRefusal, ...] = ()
    carried_claims: int = Field(default=0, ge=0)
    detail: str | None = None
    #: The accepted coordinate the change set was evaluated at; pass it as ``at``
    #: to commit exactly this preview.
    coordinate: PlaybillGetCoordinate | None = None


__all__ = [
    "ClaimTypeUpgradeRefusal",
    "ClaimTypeUpgradeRequest",
    "ClaimTypeUpgradeResult",
    "ClaimTypeUpgrade",
]
