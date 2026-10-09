"""Neutral receipts of earlier refusals of the same statement on the same evidence.

When a Claim is proposed whose statement (subject, predicate, qualifier, object)
and cited evidence match a Claim previously retired, for any reason, or a Claim
in a proposal a reviewer rejected, Cruxible resurfaces what was recorded then:
which Claim or proposal, who decided, when, the reason code and rationale
exactly as recorded, and the producing Procedure when the Claim names one. It
never interprets the reason, never refuses, and never changes settlement
because of it; an authored policy may.

Evidence is compared by content, never by the Capture envelope's own digest,
which also binds the run, the observation time and the contract: a Capture
contributes its evidence commitment digest, or, for one produced through the
core-owned workspace.file interface, the file's bytes digest that core verified
against its source-read receipt. A re-run that re-fetches identical bytes cites
the same evidence; different bytes are a new question and match nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import ProposalDigest, Sha256Value, typed_digest
from cruxible_client.contracts.claims import ClaimArtifactAny, ClaimStatement, claim_path

CLAIM_REFUSAL_MATCH_KEY_DOMAIN = "cruxible-claim-refusal-match-v3"


def claim_refusal_match_key(statement: ClaimStatement, evidence_commitments: Iterable[str]) -> str:
    """The key a refusal is indexed and found under: the statement and its evidence content.

    ``evidence_commitments`` are the content digests of the Captures the Claim
    cites (``CaptureEnvelope.commitment.digest``, or a workspace.file Capture's
    verified file bytes digest), deduplicated and sorted here. Role, effective
    times and the ClaimType version are not part of the key: the same belief
    about the same thing on the same evidence matches across them.
    """

    return typed_digest(
        Sha256Value,
        CLAIM_REFUSAL_MATCH_KEY_DOMAIN,
        {
            "subject": statement.subject.model_dump(mode="json"),
            "predicate": statement.predicate,
            "qualifier": statement.qualifier,
            "object": statement.object.model_dump(mode="json"),
            "evidence": sorted(set(evidence_commitments), key=lambda item: item.encode("ascii")),
        },
    ).tagged


def claim_producer(claim: ClaimArtifactAny) -> ArtifactPin | None:
    """The Procedure a Claim names as its producer (a derivation's reducer), if any."""

    return next((pin for pin in claim.pins if pin.target.kind == "Procedure"), None)


class _StrictRefusalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _claim_id(value: str) -> str:
    claim_path(value)
    return value


def _path_matches(claim_id: str, path: str) -> None:
    if claim_path(claim_id) != path:
        raise ValueError("a refused Claim's path is its own Claim path")


class RefusedClaimKeyV1(_StrictRefusalModel):
    """A refused Claim recorded by its match key alone (the first, unreleased shape).

    The key binds the key domain it was computed under and cannot be recomputed
    from the record. A rebuild under another domain re-derives the key from the
    rejected candidate while its objects are retained, and drops it otherwise.
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
    def _path(self) -> "RefusedClaimKeyV1":
        _path_matches(self.claim_id, self.path)
        return self


class RefusedClaim(_StrictRefusalModel):
    """One Claim of a rejected proposal, recorded by the inputs of its match key.

    A rejection record carries these because a closed proposal's candidate tree
    is released: the statement and the Capture digests it cites are taken while
    the candidate is still readable. Captures stay retained, so the key is
    recomputed under whatever key domain reads the record, never stored.
    """

    tag: Literal["cruxible-refused-claim-v2"] = "cruxible-refused-claim-v2"
    claim_id: str
    path: str
    statement: ClaimStatement
    capture_digests: tuple[str, ...]
    producer: ArtifactPin | None = None

    _claim = field_validator("claim_id")(_claim_id)

    @field_validator("capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("refused Claim Capture digests must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value

    @model_validator(mode="after")
    def _path(self) -> "RefusedClaim":
        _path_matches(self.claim_id, self.path)
        return self


RefusedClaimAny: TypeAlias = Annotated[RefusedClaimKeyV1 | RefusedClaim, Field(discriminator="tag")]


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


class PriorRefusalCoverage(_StrictRefusalModel):
    """How far earlier retirements were indexed when receipts were read.

    Present only while the index is catching up: receipts then reflect
    retirements accepted through ``indexed_through_generation`` of
    ``accepted_generation``. Rejections are always current.
    """

    tag: Literal["cruxible-prior-refusal-coverage-v1"] = "cruxible-prior-refusal-coverage-v1"
    indexed_through_generation: int = Field(ge=0)
    accepted_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def _behind(self) -> "PriorRefusalCoverage":
        if self.indexed_through_generation >= self.accepted_generation:
            raise ValueError("coverage is reported only while the index is behind")
        return self


__all__ = [
    "PriorRefusalCoverage",
    "CLAIM_REFUSAL_MATCH_KEY_DOMAIN",
    "PriorRefusalKind",
    "PriorRefusalReceipt",
    "RefusedClaim",
    "RefusedClaimAny",
    "RefusedClaimKeyV1",
    "claim_producer",
    "claim_refusal_match_key",
]
