"""Proposing the move of accepted ClaimTypes to ClaimType v7, as one reviewed change set."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.claim_types import RevisionEvidence


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ClaimTypeUpgradeRequestV1(_Model):
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
    #: Evaluate the change set and report it; propose nothing.
    dry_run: bool = False

    @field_validator("claim_types")
    @classmethod
    def _claim_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(item.removeprefix("ClaimType:") for item in value)
        return tuple(sorted(set(names), key=lambda item: item.encode("utf-8")))


class ClaimTypeUpgradeV1(_Model):
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


class ClaimTypeUpgradeRefusalV1(_Model):
    claim_type: str
    reason: str


class ClaimTypeUpgradeResultV1(_Model):
    tag: Literal["playbill-claim-type-upgrade-result-v1"] = "playbill-claim-type-upgrade-result-v1"
    #: ``would_propose``/``would_block`` answer a dry run, which writes nothing.
    status: Literal["unchanged", "proposed", "blocked", "would_propose", "would_block"]
    proposal_id: str | None = None
    upgraded: tuple[ClaimTypeUpgradeV1, ...] = ()
    #: ClaimTypes already at v7.
    unchanged: tuple[str, ...] = ()
    refused: tuple[ClaimTypeUpgradeRefusalV1, ...] = ()
    carried_claims: int = Field(default=0, ge=0)
    detail: str | None = None


__all__ = [
    "ClaimTypeUpgradeRefusalV1",
    "ClaimTypeUpgradeRequestV1",
    "ClaimTypeUpgradeResultV1",
    "ClaimTypeUpgradeV1",
]
