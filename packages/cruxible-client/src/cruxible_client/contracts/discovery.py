"""Frozen semantic discovery and bounded expansion contracts."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    Sha256Value,
    normalize_canonical,
)
from cruxible_client.contracts.diagnostics import GovernedOperationReference
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    AttestationCoverage,
    CoverageDescriptorV1,
    SemanticReadCoordinateV1,
    SourceDereferenceResultV1,
    SourceHandleV1,
)

DiscoveryMatchBasis = Literal[
    "exact_address",
    "exact_alias",
    "structural_signature",
    "tag",
    "lexical",
    "named_entrypoint",
    "dependency_walk",
    "content_equivalent",
]
"""The closed v1 match bases; ranking may reorder, and only a ratified contract
change extends.

``content_equivalent`` joined the closed set under
`dd-match-basis-content-equivalent` as a coordinated contract change, never a
free addition. It is minted only by the §11.6 coverage resolver, for identical
bytes observed at a *foreign* source occurrence; :func:`discover` never produces
it, because the accepted naming layer that path ranges over carries no byte
occurrences to compare. It resolves equivalence to ``False``: copied bytes at a
foreign occurrence are, by §11.6.1, precisely not identity.
"""

_FORBIDDEN_HINT_RE = re.compile(
    r"(?:https?://|file://|api[_ -]?key|bearer\s|password|private[_ -]?key|"
    r"benchmark[_ -]?task|customer[_ -]?(?:task|scratch)|ignore\s+(?:all\s+)?previous|"
    r"system\s+prompt|developer\s+message)",
    re.IGNORECASE,
)


class _StrictDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalized_match_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def normalize_discovery_term(value: str) -> str:
    """Normalize one match term deterministically; never a similarity score.

    This is the single normalization vocabulary shared by exact/lexical
    discovery, search, and the vocabulary index.
    """

    return _normalized_match_term(value)


def reject_locator_or_secret(value: str, *, label: str) -> str:
    """Refuse rendered or indexed text that carries a locator, secret, or lure.

    Discovery output is read by agents, so one exclusion vocabulary governs
    every index and capsule this layer emits.
    """

    if _FORBIDDEN_HINT_RE.search(value):
        raise ValueError(f"{label} contains forbidden locator, secret, task, or instruction text")
    return value


class DiscoveryBudgetV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-budget-v1"] = "playbill-discovery-budget-v1"
    max_hits: int = Field(default=20, ge=1)
    max_bytes: int = Field(default=16_384, ge=1)


class ExpansionBudgetV1(_StrictDiscoveryModel):
    tag: Literal["playbill-expansion-budget-v1"] = "playbill-expansion-budget-v1"
    max_bytes: int = Field(default=65_536, ge=1)
    max_relations: int = Field(default=100, ge=0)
    max_source_handles: int = Field(default=20, ge=0)


class DiscoveryMatchBasisV1(_StrictDiscoveryModel):
    basis: DiscoveryMatchBasis
    matched_text: str | None = None


class DiscoveryHitV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-hit-v1"] = "playbill-discovery-hit-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    kind: str
    label: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    match_basis: tuple[DiscoveryMatchBasisV1, ...]
    role: str | None = None
    verdict: str | None = None
    currency: Literal["current", "stale", "not_applicable"]
    source_handles: tuple[SourceHandleV1, ...] = ()
    dependency_addresses: tuple[SemanticAddress, ...] = ()
    dependent_addresses: tuple[SemanticAddress, ...] = ()


class DiscoveryRequestV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-request-v1"] = "playbill-discovery-request-v1"
    query: str | None = None
    entrypoint: str | None = None
    at: SemanticReadCoordinateV1
    evaluation_time: str
    profile: Literal["interfaces", "subjects", "all"] = "interfaces"
    budget: DiscoveryBudgetV1 = DiscoveryBudgetV1()

    @model_validator(mode="after")
    def _selection(self) -> "DiscoveryRequestV1":
        if (self.query is None) == (self.entrypoint is None):
            raise ValueError("discover requires exactly one query or entrypoint")
        return self


class DiscoveryPageV1(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-page-v1"] = "playbill-discovery-page-v1"
    coordinate_kind: Literal["accepted", "candidate", "local_only"]
    at: SemanticReadCoordinateV1 | None
    evaluation_time: str
    hits: tuple[DiscoveryHitV1, ...]
    selection_basis_digest: str
    receipt_digest: str
    coverage: CoverageDescriptorV1


class ExpandRequestV1(_StrictDiscoveryModel):
    tag: Literal["playbill-expand-request-v1"] = "playbill-expand-request-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    evaluation_time: str
    facets: tuple[str, ...]
    budget: ExpansionBudgetV1 = ExpansionBudgetV1()


class ContextCapsuleV1(_StrictDiscoveryModel):
    tag: Literal["playbill-context-capsule-v1"] = "playbill-context-capsule-v1"
    address: SemanticAddress
    at: SemanticReadCoordinateV1
    evaluation_time: str
    canonical_summary: object
    governance: object
    provenance: object
    attestation_coverage: AttestationCoverage
    claim_context: object | None = None
    procedure_context: object | None = None
    claim_type_card: object | None = None
    subject_profile: object | None = None
    source_material: tuple[SourceDereferenceResultV1, ...] = ()
    relations: tuple[object, ...] = ()
    next_reads: tuple[GovernedOperationReference, ...] = ()
    coverage: CoverageDescriptorV1
    receipt_digest: str

    @field_validator("canonical_summary", "governance", "provenance")
    @classmethod
    def _canonical_objects(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("claim_context", "procedure_context", "claim_type_card", "subject_profile")
    @classmethod
    def _optional_canonical(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("relations")
    @classmethod
    def _relations(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(normalize_canonical(item) for item in value)

    @field_validator("receipt_digest")
    @classmethod
    def _receipt_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ContextMaterialV1(_StrictDiscoveryModel):
    """Explicit instruction/data boundary for any later client injection."""

    tag: Literal["playbill-context-material-v1"] = "playbill-context-material-v1"
    classification: Literal["untrusted_data", "eligible_instruction"]
    subject: SemanticAddress
    at: SemanticReadCoordinateV1
    content_digest: str
    accepted_context_policy_digest: str | None = None

    @field_validator("content_digest", "accepted_context_policy_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _instruction_authority(self) -> "ContextMaterialV1":
        if (self.classification == "eligible_instruction") != (
            self.accepted_context_policy_digest is not None
        ):
            raise ValueError("instruction eligibility requires an exact accepted context policy")
        return self


__all__ = [
    "ContextCapsuleV1",
    "ContextMaterialV1",
    "DiscoveryBudgetV1",
    "DiscoveryHitV1",
    "DiscoveryMatchBasis",
    "DiscoveryMatchBasisV1",
    "DiscoveryPageV1",
    "DiscoveryRequestV1",
    "ExpandRequestV1",
    "ExpansionBudgetV1",
    "normalize_discovery_term",
    "reject_locator_or_secret",
]
