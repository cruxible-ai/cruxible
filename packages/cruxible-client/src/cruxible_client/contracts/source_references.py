"""Locator-free semantic read coordinates and source/evidence commitments."""

from __future__ import annotations

import base64
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.candidates import SemanticCandidate, candidate_digest
from cruxible_client.contracts.canonical import (
    CandidateDigest,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress

AttestationCoverage = Literal[
    "exact_subject",
    "containing_artifact",
    "containing_change_set",
]
SourceAccessClass = Literal["public", "instance", "restricted"]

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_SECRET_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "connection_string",
    "credential",
    "credentials",
    "host",
    "hostname",
    "password",
    "private_key",
    "secret",
    "token",
}


class _StrictSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProvisionalSemanticReadCoordinateV1(_StrictSourceModel):
    """Locator-free candidate read bound to one exact accepted base."""

    tag: Literal["playbill-provisional-read-coordinate-v1"] = (
        "playbill-provisional-read-coordinate-v1"
    )
    accepted_base: AcceptedCoordinate
    candidate: SemanticCandidate
    candidate_digest: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _binding(self) -> "ProvisionalSemanticReadCoordinateV1":
        if self.candidate.parent_semantic_root != self.accepted_base.semantic_root:
            raise ValueError("provisional read candidate differs from its accepted base")
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("provisional read candidate digest does not reproduce")
        return self


class CandidateGenerationReadCoordinateV1(_StrictSourceModel):
    """Locator-free verified prebuild coordinate; still not accepted state."""

    tag: Literal["playbill-candidate-generation-read-coordinate-v1"] = (
        "playbill-candidate-generation-read-coordinate-v1"
    )
    accepted_base: AcceptedCoordinate
    candidate: SemanticCandidate
    candidate_digest: str
    generation_git_oid: str
    semantic_root: str
    generation_root: str

    @field_validator("candidate_digest")
    @classmethod
    def _candidate_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value

    @field_validator("generation_git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("candidate-generation Git OID is malformed")
        return value

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("generation_root")
    @classmethod
    def _generation_root(cls, value: str) -> str:
        GenerationRoot.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _binding(self) -> "CandidateGenerationReadCoordinateV1":
        if self.candidate.parent_semantic_root != self.accepted_base.semantic_root:
            raise ValueError("candidate generation differs from its accepted base")
        if candidate_digest(self.candidate).tagged != self.candidate_digest:
            raise ValueError("candidate-generation C_s digest does not reproduce")
        if self.generation_git_oid == self.accepted_base.git_oid:
            raise ValueError("candidate generation must differ from its accepted base")
        return self


SemanticReadCoordinateV1 = Annotated[
    AcceptedCoordinate | ProvisionalSemanticReadCoordinateV1 | CandidateGenerationReadCoordinateV1,
    Field(discriminator="tag"),
]


def validate_local_read_coordinate(
    coordinate: AcceptedCoordinate
    | ProvisionalSemanticReadCoordinateV1
    | CandidateGenerationReadCoordinateV1,
    *,
    expected_accepted: AcceptedCoordinate,
) -> None:
    """Refuse foreign/unverified roots instead of treating remote state as local."""

    base = coordinate if isinstance(coordinate, AcceptedCoordinate) else coordinate.accepted_base
    if base != expected_accepted:
        raise ValueError("remote or unverified accepted-state coordinate is forbidden in v1")


class LedgerSourceReferenceV1(_StrictSourceModel):
    tag: Literal["playbill-ledger-source-reference-v1"] = "playbill-ledger-source-reference-v1"
    kind: Literal["ledger"] = "ledger"
    address: SemanticAddress
    coordinate: AcceptedCoordinate


class CasSourceReferenceV1(_StrictSourceModel):
    tag: Literal["playbill-cas-source-reference-v1"] = "playbill-cas-source-reference-v1"
    kind: Literal["cas"] = "cas"
    content_digest: str

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


# A selector field that quotes the source's own bytes to locate a window in
# them. It is prose the author copied out of the file, and a URL inside it is
# bytes like any other: nothing reads it as an address, so the locator rule
# does not apply to it. The secret rules still do -- credential material has
# no business in an anchor whatever it was copied from.
_QUOTED_SOURCE_KEYS = frozenset({"anchor"})


def _reject_secret_or_locator(
    value: object,
    *,
    location: str = "$",
    quoted_source: bool = False,
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.casefold().replace("-", "_")
            if lowered in _SECRET_KEYS or any(
                lowered.endswith(f"_{secret}") for secret in _SECRET_KEYS
            ):
                raise ValueError(f"secret-bearing source field is forbidden at {location}.{key}")
            _reject_secret_or_locator(
                item,
                location=f"{location}.{key}",
                quoted_source=lowered in _QUOTED_SOURCE_KEYS,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_or_locator(item, location=f"{location}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if lowered.startswith("bearer ") or "-----begin private key-----" in lowered:
            raise ValueError(f"source coordinates/selectors cannot carry secrets at {location}")
        if "://" in value and not quoted_source:
            raise ValueError(
                f"source coordinates/selectors cannot carry locators at {location}; an "
                "anchor may quote a URL, a coordinate or selector field may not name one"
            )


class ExternalSourceReferenceV1(_StrictSourceModel):
    tag: Literal["playbill-external-source-reference-v1"] = "playbill-external-source-reference-v1"
    kind: Literal["external"] = "external"
    source_identity: str
    producer_binding_digest: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    replayability: Literal["exact", "attested_only"]

    @field_validator("source_identity")
    @classmethod
    def _source_identity(cls, value: str) -> str:
        if not _SOURCE_ID_RE.fullmatch(value):
            raise ValueError("external source_identity must be logical and locator-free")
        return value

    @field_validator("producer_binding_digest")
    @classmethod
    def _producer_binding_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("coordinate_type", "selector_type")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        if not _SCHEMA_ID_RE.fullmatch(value):
            raise ValueError("source coordinate/selector type must be a canonical identifier")
        return value

    @field_validator("coordinate", "selector")
    @classmethod
    def _canonical_value(cls, value: object) -> object:
        normalized = normalize_canonical(value)
        _reject_secret_or_locator(normalized)
        return normalized


SourceReferenceV1 = Annotated[
    LedgerSourceReferenceV1 | CasSourceReferenceV1 | ExternalSourceReferenceV1,
    Field(discriminator="kind"),
]


class SourceSchemaRegistry:
    """Exact external coordinate/selector grammars installed by reviewed code/contracts."""

    def __init__(
        self,
        *,
        coordinate_types: tuple[str, ...],
        selector_types: tuple[str, ...],
    ) -> None:
        self.coordinate_types = self._validated(coordinate_types)
        self.selector_types = self._validated(selector_types)

    @staticmethod
    def _validated(values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values), key=lambda item: item.encode("utf-8"))):
            raise ValueError("source schema registry values must be sorted and unique")
        if any(not _SCHEMA_ID_RE.fullmatch(item) for item in values):
            raise ValueError("source schema registry contains an invalid identifier")
        return values

    def require(self, reference: ExternalSourceReferenceV1) -> None:
        if reference.coordinate_type not in self.coordinate_types:
            raise ValueError(f"unregistered external coordinate type: {reference.coordinate_type}")
        if reference.selector_type not in self.selector_types:
            raise ValueError(f"unregistered external selector type: {reference.selector_type}")


class EvidenceCommitmentV1(_StrictSourceModel):
    tag: Literal["playbill-evidence-commitment-v1"] = "playbill-evidence-commitment-v1"
    digest_kind: Literal[
        "exact_bytes",
        "canonical_value",
        "query_result",
        "provider_statement",
    ]
    digest: str
    byte_length: int | None = Field(default=None, ge=0)
    materialization: Literal["ledger", "cas", "external", "none"]

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _byte_shape(self) -> "EvidenceCommitmentV1":
        if (self.byte_length is not None) != (self.digest_kind == "exact_bytes"):
            raise ValueError("byte_length is required exactly for exact_bytes commitments")
        return self


def validate_source_commitment(
    source: LedgerSourceReferenceV1 | CasSourceReferenceV1 | ExternalSourceReferenceV1,
    commitment: EvidenceCommitmentV1,
) -> None:
    """Enforce source-kind/materialization/replayability correspondence."""

    if source.kind == "ledger" and commitment.materialization != "ledger":
        raise ValueError("ledger sources require ledger materialization")
    if source.kind == "cas" and commitment.materialization != "cas":
        raise ValueError("CAS sources require CAS materialization")
    if source.kind == "external":
        if commitment.materialization not in {"external", "none", "cas"}:
            raise ValueError(
                "external sources permit only external, none, or bounded CAS materialization"
            )
        if commitment.materialization == "external" and source.replayability != "exact":
            raise ValueError("external materialization requires exact replayability")
        if commitment.materialization == "none" and source.replayability != "attested_only":
            raise ValueError("no materialization requires attested-only replayability")


class CoverageDescriptorV1(_StrictSourceModel):
    tag: Literal["playbill-coverage-descriptor-v1"] = "playbill-coverage-descriptor-v1"
    requested_facets: tuple[str, ...] = ()
    available_facets: tuple[str, ...] = ()
    omitted_for_access: tuple[str, ...] = ()
    truncated_facets: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @field_validator(
        "requested_facets",
        "available_facets",
        "omitted_for_access",
        "truncated_facets",
        "reason_codes",
    )
    @classmethod
    def _sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("coverage fields must be sorted and unique")
        return value


class SourceHandleV1(_StrictSourceModel):
    tag: Literal["playbill-source-handle-v1"] = "playbill-source-handle-v1"
    subject: SemanticAddress
    at: SemanticReadCoordinateV1
    source: SourceReferenceV1
    commitment: EvidenceCommitmentV1
    media_type: str | None = None
    exact_spans: tuple[ContentSpan, ...] = ()
    access_class: SourceAccessClass

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str | None) -> str | None:
        if value is not None and ("/" not in value or any(char.isspace() for char in value)):
            raise ValueError("media_type must use a canonical type/subtype spelling")
        return value

    @field_validator("exact_spans")
    @classmethod
    def _spans(cls, value: tuple[ContentSpan, ...]) -> tuple[ContentSpan, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.content_digest.encode("ascii"),
                    item.start_byte,
                    item.end_byte,
                ),
            )
        )
        identities = {canonical_bytes(item.model_dump(mode="json")) for item in value}
        if value != ordered or len(identities) != len(value):
            raise ValueError("source-handle spans must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_binding(self) -> "SourceHandleV1":
        validate_source_commitment(self.source, self.commitment)
        if self.exact_spans:
            if self.commitment.digest_kind != "exact_bytes":
                raise ValueError("byte spans require an exact_bytes commitment")
            if any(span.content_digest != self.commitment.digest for span in self.exact_spans):
                raise ValueError("source-handle span digest differs from its commitment")
            if self.commitment.byte_length is None or any(
                span.end_byte > self.commitment.byte_length for span in self.exact_spans
            ):
                raise ValueError("source-handle span exceeds committed byte length")
        return self


def source_handle_digest(handle: SourceHandleV1) -> str:
    payload = handle.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-source-handle-v1",
        payload,
    ).tagged


class BodyAccessResultV1(_StrictSourceModel):
    tag: Literal["playbill-body-access-result-v1"] = "playbill-body-access-result-v1"
    status: Literal["available", "unavailable"]
    content_digest: str | None = None
    byte_length: int | None = Field(default=None, ge=0)
    body_base64: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "BodyAccessResultV1":
        if self.status == "available":
            if self.content_digest is None or self.byte_length is None or self.body_base64 is None:
                raise ValueError("available body access requires digest, length, and bytes")
            Sha256Value.from_tagged(self.content_digest)
            try:
                body = base64.b64decode(self.body_base64, validate=True)
            except ValueError as exc:
                raise ValueError("body access contains malformed base64") from exc
            if len(body) != self.byte_length:
                raise ValueError("body access byte length does not reproduce")
        elif any(
            item is not None for item in (self.content_digest, self.byte_length, self.body_base64)
        ):
            raise ValueError("unavailable body access cannot carry material")
        return self


class SourceDereferenceResultV1(_StrictSourceModel):
    tag: Literal["playbill-source-dereference-result-v1"] = "playbill-source-dereference-result-v1"
    source_handle_digest: str
    status: Literal["verified", "drifted", "attested_only", "unavailable", "denied"]
    commitment_verified: bool
    observed_commitment_digest: str | None = None
    material_kind: Literal["bytes", "canonical_value", "query_result", "metadata_only"]
    canonical_material: object | None = None
    body_access: BodyAccessResultV1 | None = None
    coverage: CoverageDescriptorV1

    @field_validator("source_handle_digest", "observed_commitment_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("canonical_material")
    @classmethod
    def _canonical_material(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _status_shape(self) -> "SourceDereferenceResultV1":
        if self.status == "verified" and not self.commitment_verified:
            raise ValueError("verified dereference must reproduce the commitment")
        if self.status in {"attested_only", "unavailable", "denied"}:
            if self.material_kind != "metadata_only" or self.canonical_material is not None:
                raise ValueError("non-material dereference must return metadata_only")
        if self.status == "drifted" and self.observed_commitment_digest is None:
            raise ValueError("drifted dereference requires the newly observed digest")
        if self.material_kind == "bytes" and self.body_access is None:
            raise ValueError("byte dereference requires a body-access result")
        return self


class OpenSourceRequestV1(_StrictSourceModel):
    tag: Literal["playbill-open-source-request-v1"] = "playbill-open-source-request-v1"
    source_handle: SourceHandleV1
    structural_context_bytes: int = Field(default=0, ge=0)
    resource_budget_bytes: int = Field(ge=0)


__all__ = [
    "AttestationCoverage",
    "BodyAccessResultV1",
    "CandidateGenerationReadCoordinateV1",
    "CasSourceReferenceV1",
    "CoverageDescriptorV1",
    "EvidenceCommitmentV1",
    "ExternalSourceReferenceV1",
    "LedgerSourceReferenceV1",
    "OpenSourceRequestV1",
    "ProvisionalSemanticReadCoordinateV1",
    "SemanticReadCoordinateV1",
    "SourceDereferenceResultV1",
    "SourceHandleV1",
    "SourceReferenceV1",
    "SourceSchemaRegistry",
    "source_handle_digest",
    "validate_local_read_coordinate",
    "validate_source_commitment",
]
