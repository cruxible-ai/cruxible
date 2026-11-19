"""Frozen semantic discovery and bounded expansion contracts."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CoverageDescriptor,
    SemanticReadCoordinate,
    SourceHandle,
)

DiscoveryMatchBasisKind = Literal[
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


class DiscoveryBudget(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-budget-v1"] = "playbill-discovery-budget-v1"
    max_hits: int = Field(default=20, ge=1)
    max_bytes: int = Field(default=16_384, ge=1)


class DiscoveryMatchBasis(_StrictDiscoveryModel):
    basis: DiscoveryMatchBasisKind
    matched_text: str | None = None


class DiscoveryHit(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-hit-v1"] = "playbill-discovery-hit-v1"
    address: SemanticAddress
    at: SemanticReadCoordinate
    kind: str
    label: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    match_basis: tuple[DiscoveryMatchBasis, ...]
    role: str | None = None
    verdict: str | None = None
    currency: Literal["current", "stale", "not_applicable"]
    source_handles: tuple[SourceHandle, ...] = ()
    dependency_addresses: tuple[SemanticAddress, ...] = ()
    dependent_addresses: tuple[SemanticAddress, ...] = ()


class DiscoveryRequest(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-request-v1"] = "playbill-discovery-request-v1"
    query: str | None = None
    entrypoint: str | None = None
    at: SemanticReadCoordinate
    evaluation_time: str
    profile: Literal["interfaces", "subjects", "all"] = "interfaces"
    budget: DiscoveryBudget = DiscoveryBudget()

    @model_validator(mode="after")
    def _selection(self) -> "DiscoveryRequest":
        if (self.query is None) == (self.entrypoint is None):
            raise ValueError("discover requires exactly one query or entrypoint")
        return self


class DiscoveryPage(_StrictDiscoveryModel):
    tag: Literal["playbill-discovery-page-v1"] = "playbill-discovery-page-v1"
    coordinate_kind: Literal["accepted", "candidate", "local_only"]
    at: SemanticReadCoordinate | None
    evaluation_time: str
    hits: tuple[DiscoveryHit, ...]
    selection_basis_digest: str
    receipt_digest: str
    coverage: CoverageDescriptor


__all__ = [
    "DiscoveryBudget",
    "DiscoveryHit",
    "DiscoveryMatchBasisKind",
    "DiscoveryMatchBasis",
    "DiscoveryPage",
    "DiscoveryRequest",
    "normalize_discovery_term",
    "reject_locator_or_secret",
]
