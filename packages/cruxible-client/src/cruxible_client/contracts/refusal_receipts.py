"""Neutral receipts of earlier refusals of the same statement on the same evidence.

When a Claim is proposed whose statement (subject, predicate, qualifier, object)
and cited evidence (its backing Capture digests) match a Claim previously
retired, for any reason, or a Claim in a proposal a reviewer rejected,
Cruxible resurfaces what was recorded then: which Claim or proposal, who
decided, when, the reason code and rationale exactly as recorded, and the
producing Procedure when the Claim names one. It never interprets the reason,
never refuses, and never changes settlement because of it; an authored policy
may.

New evidence is a new question: a Claim citing any other set of Captures does
not match.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import ProposalDigest, Sha256Value, typed_digest
from cruxible_client.contracts.claims import ClaimArtifactAny, ClaimStatement, claim_path

CLAIM_REFUSAL_MATCH_KEY_DOMAIN = "cruxible-claim-refusal-match-v1"


def claim_refusal_match_key(statement: ClaimStatement, evidence_digests: Iterable[str]) -> str:
    """The key a refusal is indexed and found under: the statement and its exact evidence.

    Role, effective times and the ClaimType version are not part of it: the same
    belief about the same thing on the same evidence matches across them.
    """

    return typed_digest(
        Sha256Value,
        CLAIM_REFUSAL_MATCH_KEY_DOMAIN,
        {
            "subject": statement.subject.model_dump(mode="json"),
            "predicate": statement.predicate,
            "qualifier": statement.qualifier,
            "object": statement.object.model_dump(mode="json"),
            "evidence": sorted(set(evidence_digests), key=lambda item: item.encode("ascii")),
        },
    ).tagged


def claim_refusal_key(claim: ClaimArtifactAny) -> str:
    """The match key of one Claim artifact, over the Captures its backing cites."""

    return claim_refusal_match_key(claim.statement, claim.backing.capture_digests)


def claim_producer(claim: ClaimArtifactAny) -> ArtifactPin | None:
    """The Procedure a Claim names as its producer (a derivation's reducer), if any."""

    return next((pin for pin in claim.pins if pin.target.kind == "Procedure"), None)


class _StrictRefusalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _claim_id(value: str) -> str:
    claim_path(value)
    return value


class RefusedClaim(_StrictRefusalModel):
    """One Claim of a rejected proposal, keyed so a later proposal of it can be found.

    A rejection record carries these because a closed proposal's candidate tree
    is released: the key is taken while the candidate is still readable.
    """

    tag: Literal["cruxible-refused-claim-v1"] = "cruxible-refused-claim-v1"
    claim_id: str
    path: str
    match_key: str
    producer: ArtifactPin | None = None

    _claim = field_validator("claim_id")(_claim_id)

    @field_validator("match_key")
    @classmethod
    def _key(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _path(self) -> "RefusedClaim":
        if claim_path(self.claim_id) != self.path:
            raise ValueError("a refused Claim's path is its own Claim path")
        return self


PriorRefusalKind = Literal["claim_retired", "proposal_rejected"]


class PriorRefusalReceipt(_StrictRefusalModel):
    """An earlier refusal of the same statement on the same evidence, as recorded.

    ``claim_retired``: ``claim_id`` was retired by ``decided_by`` at ``decided_at``
    with ``reason_code`` (its retirement reason, when attributed) and the
    retiring change's ``rationale``; ``proposal_id`` is that change's proposal
    when known. ``proposal_rejected``: a reviewer, ``decided_by``, rejected
    ``proposal_id`` (which proposed ``claim_id``) at ``decided_at``, giving
    ``rationale``; a rejection records no reason code. ``producer`` is the
    Procedure the prior Claim named, when it named one. Nothing here is a
    verdict on the new Claim.
    """

    tag: Literal["cruxible-prior-refusal-receipt-v1"] = "cruxible-prior-refusal-receipt-v1"
    kind: PriorRefusalKind
    claim_id: str
    proposal_id: str | None = None
    decided_by: str
    decided_at: str
    reason_code: str | None = None
    rationale: str | None = None
    producer: ArtifactPin | None = None

    _claim = field_validator("claim_id")(_claim_id)

    @field_validator("proposal_id")
    @classmethod
    def _proposal(cls, value: str | None) -> str | None:
        if value is not None:
            ProposalDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "PriorRefusalReceipt":
        if self.kind == "proposal_rejected" and (
            self.proposal_id is None or self.reason_code is not None
        ):
            raise ValueError("a rejection names its proposal and records no reason code")
        return self


__all__ = [
    "CLAIM_REFUSAL_MATCH_KEY_DOMAIN",
    "PriorRefusalKind",
    "PriorRefusalReceipt",
    "RefusedClaim",
    "claim_producer",
    "claim_refusal_key",
    "claim_refusal_match_key",
]
