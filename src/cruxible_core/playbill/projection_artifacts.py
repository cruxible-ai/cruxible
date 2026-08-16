"""Registered Playbill artifact formats and normalized projection rows."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactFormatRegistry,
    ArtifactFormatTag,
    ArtifactKindRegistry,
    ArtifactPathKind,
)
from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.canonical import ArtifactDigest, canonical_bytes, file_digest
from cruxible_core.playbill.cas import BodyAccessContext, BodyProjectionProtocol
from cruxible_core.playbill.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    parse_claim_type,
)
from cruxible_core.playbill.documents import document_digest, parse_document
from cruxible_core.playbill.errors import (
    DocumentFormatError,
    PlaybillCasError,
    ProjectionFormatError,
    SettlementIntegrityError,
    SubjectFormatError,
)
from cruxible_core.playbill.explanation import (
    ProjectionCoordinateContext,
    accepted_artifact_explanation_facts,
    accepted_document_explanation_facts,
)
from cruxible_core.playbill.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)
from cruxible_core.playbill.semantic import SemanticAddress, whole_body_mapping
from cruxible_core.playbill.subjects import parse_subject, subject_digest
from cruxible_core.playbill.types import PrincipalRecord

if TYPE_CHECKING:
    from cruxible_core.playbill.settlement import ChangeSetRecord, ChangeSetRecordV2

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
PLAYBILL_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        ArtifactPathKind(
            "principal",
            re.compile(r"^principals/[a-z][a-z0-9_.-]{0,127}\.yaml$"),
        ),
        ArtifactPathKind(
            "document",
            re.compile(r"^documents/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "subject",
            re.compile(
                r"^subjects/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
                r"[a-z][a-z0-9_.-]{0,255}\.yaml$"
            ),
        ),
        ArtifactPathKind(
            "claim-type",
            re.compile(
                r"^claim-types/[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/"
                r"[a-z][a-z0-9_]{0,63}\.yaml$"
            ),
        ),
        ArtifactPathKind(
            "capture-contract",
            re.compile(r"^capture-contracts/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "claim",
            re.compile(r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.yaml$"),
        ),
        ArtifactPathKind(
            "line",
            re.compile(r"^lines/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
            implemented=False,
        ),
        ArtifactPathKind(
            "fixture",
            re.compile(r"^artifacts/fixtures/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "presentation",
            re.compile(r"^presentation/fixtures/[a-z][a-z0-9_.-]{0,255}\.json$"),
        ),
        ArtifactPathKind(
            "changeset",
            re.compile(r"^changesets/cs-[0-9]{20}\.json$"),
        ),
    )
)

PLAYBILL_FORMAT_RESERVATIONS = ArtifactFormatRegistry(
    tuple(
        ArtifactFormatTag(
            tag,
            implemented=tag
            in {
                "playbill-capture-contract-v1",
                "playbill-capture-envelope-v1",
                "playbill-claim-v1",
            },
        )
        for tag in (
            "playbill-accepted-state-run-input-v1",
            "playbill-capture-contract-v1",
            "playbill-capture-envelope-v1",
            "playbill-claim-v1",
            "playbill-exhaust-run-input-v1",
            "playbill-landed-capture-run-input-v1",
            "playbill-line-slot-binding-v1",
            "playbill-line-v1",
            "playbill-procedure-pin-slot-ref-v1",
        )
    )
)

RegisteredPathKind = Literal[
    "capture-contract",
    "changeset",
    "claim",
    "claim-type",
    "document",
    "fixture",
    "line",
    "presentation",
    "principal",
    "subject",
]


class _StrictArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str, *, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


class FixturePin(_StrictArtifactModel):
    target_identity: str
    target_digest: str

    @field_validator("target_identity")
    @classmethod
    def _target_identity(cls, value: str) -> str:
        return _identifier(value, label="pin target_identity")

    @field_validator("target_digest")
    @classmethod
    def _target_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class FixtureArtifact(_StrictArtifactModel):
    """Minimal semantic envelope used only to prove the PB-B compiler contract."""

    tag: Literal["playbill-fixture-v1"] = "playbill-fixture-v1"
    kind: Literal["fixture"] = "fixture"
    artifact_id: str
    revision: int = Field(ge=1, le=2**63 - 1)
    predecessor_digest: str | None = None
    pins: tuple[FixturePin, ...] = ()
    extension_facts: tuple[ProjectionFact, ...] = ()

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        return _identifier(value, label="fixture artifact_id")

    @field_validator("predecessor_digest")
    @classmethod
    def _predecessor_digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[FixturePin, ...]) -> tuple[FixturePin, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.target_identity.encode("utf-8"),
                    item.target_digest.encode("ascii"),
                ),
            )
        )
        if value != ordered or len({item.target_identity for item in value}) != len(value):
            raise ValueError("fixture pins must be sorted and unique by target_identity")
        return value

    @field_validator("extension_facts")
    @classmethod
    def _extension_facts(cls, value: tuple[ProjectionFact, ...]) -> tuple[ProjectionFact, ...]:
        def key(fact: ProjectionFact) -> tuple[bytes, int, bytes, bytes]:
            return (
                fact.schema_id.encode("utf-8"),
                fact.schema_version,
                fact.subject_identity.encode("utf-8"),
                fact.fact_key.encode("utf-8"),
            )

        ordered = tuple(sorted(value, key=key))
        if value != ordered or len({key(fact) for fact in value}) != len(value):
            raise ValueError("fixture extension facts must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _fact_subjects(self) -> "FixtureArtifact":
        if any(fact.subject_identity != self.artifact_id for fact in self.extension_facts):
            raise ValueError("fixture extension facts must name their containing artifact")
        return self


class FixturePresentation(_StrictArtifactModel):
    """Disposable rendered/cache content excluded from the canonical logical export."""

    tag: Literal["playbill-fixture-presentation-v1"] = "playbill-fixture-presentation-v1"
    subject_identity: str
    label: str

    @field_validator("subject_identity")
    @classmethod
    def _subject_identity(cls, value: str) -> str:
        return _identifier(value, label="fixture presentation subject_identity")


@dataclass(frozen=True)
class ArtifactEnvelopeRow:
    identity: str
    kind: str
    format_tag: str
    path: str
    artifact_digest: str
    predecessor_digest: str | None
    revision: int


@dataclass(frozen=True)
class PinRow:
    source_identity: str
    target_identity: str
    target_digest: str


@dataclass(frozen=True)
class ParsedProjectionTree:
    envelopes: tuple[ArtifactEnvelopeRow, ...]
    pins: tuple[PinRow, ...]
    retired_identities: tuple[str, ...]
    semantic_facts: tuple[ProjectionFact, ...]
    presentation_facts: tuple[ProjectionFact, ...]


def registered_path_kind(path: str) -> RegisteredPathKind:
    return cast(RegisteredPathKind, PLAYBILL_ARTIFACT_KINDS.resolve_path(path))


def _projected_revision(
    records: tuple[tuple[str, ChangeSetRecord | ChangeSetRecordV2], ...],
    *,
    path: str,
    input_digest: str,
    artifact_digest: str,
) -> int:
    history = tuple(
        member
        for _record_path, record in records
        for member in record.members
        if member.path == path
    )
    if any(
        (
            getattr(member, "candidate_artifact_digest", None) == artifact_digest
            if getattr(member, "candidate_artifact_digest", None) is not None
            else getattr(member, "artifact_digest", None) == input_digest
        )
        for member in history
    ):
        return len(history)
    return len(history) + 1


def _pairs_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        normalized_key = unicodedata.normalize("NFC", key)
        if normalized_key in normalized:
            raise ProjectionFormatError("artifact object has duplicate normalized keys")
        normalized.add(normalized_key)
        result[key] = value
    return result


def _load_object(content: bytes, *, path: str) -> dict[str, object]:
    try:
        decoded = content.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_pairs_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionFormatError(
            f"registered artifact must use strict canonical JSON (YAML-compatible): {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectionFormatError(f"registered artifact must be an object: {path}")
    return payload


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_bytes(model.model_dump(mode="json")) + b"\n"


def parse_projection_tree(
    blobs: dict[str, bytes],
    *,
    registry: ProjectionExtensionRegistry,
    bodies: BodyProjectionProtocol | None = None,
    coordinate: ProjectionCoordinateContext | None = None,
) -> ParsedProjectionTree:
    """Parse all registered blobs and produce one sorted, typed row stream."""

    from cruxible_core.playbill.captures import (
        CaptureFormatError,
        capture_contract_digest,
        parse_capture_contract,
    )
    from cruxible_core.playbill.claims import (
        ClaimFormatError,
        claim_artifact_digest,
        claim_statement_address,
        claim_statement_digest,
        parse_claim,
    )
    from cruxible_core.playbill.settlement import (
        parse_change_set_record,
    )

    envelopes: list[ArtifactEnvelopeRow] = []
    pins: list[PinRow] = []
    retired_identities: list[str] = []
    semantic_facts: list[ProjectionFact] = []
    presentation_facts: list[ProjectionFact] = []
    identities: dict[str, str] = {}
    change_sets: list[tuple[str, ChangeSetRecord | ChangeSetRecordV2]] = []

    for path in sorted(blobs, key=lambda item: item.encode("utf-8")):
        if registered_path_kind(path) != "changeset":
            continue
        content = blobs[path]
        payload = _load_object(content, path=path)
        try:
            record = parse_change_set_record(content, path=path)
        except SettlementIntegrityError as exc:
            raise ProjectionFormatError(
                f"change-set record failed strict validation: {path}"
            ) from exc
        expected_path = f"changesets/cs-{record.sequence:020d}.json"
        if path != expected_path:
            raise ProjectionFormatError("change-set sequence differs from its canonical path")
        change_sets.append((path, record))
    if [record.sequence for _path, record in change_sets] != list(range(1, len(change_sets) + 1)):
        raise ProjectionFormatError("change-set history must be contiguous from sequence one")
    accepted_change_sets = tuple(change_sets)

    for path in sorted(blobs, key=lambda item: item.encode("utf-8")):
        content = blobs[path]
        kind = registered_path_kind(path)
        payload = _load_object(content, path=path)
        try:
            if kind == "principal":
                principal = PrincipalRecord.model_validate(payload)
                if render_principal(principal) != content:
                    raise ProjectionFormatError(f"principal artifact is not canonical: {path}")
                continue
            if kind == "changeset":
                continue
            if kind == "document":
                if bodies is None:
                    raise ProjectionFormatError(
                        "Document projection requires the managed body-metadata resolver"
                    )
                try:
                    document = parse_document(content, path=path)
                except DocumentFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered Document failed strict validation: {path}"
                    ) from exc
                previous = identities.get(document.identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {document.identity!r}: {previous} and {path}"
                    )
                identities[document.identity] = path
                try:
                    metadata = bodies.metadata(
                        document.body_digest,
                        access=BodyAccessContext(
                            principal_id="playbill-compiler",
                            can_read_body=True,
                        ),
                    )
                except PlaybillCasError as exc:
                    raise ProjectionFormatError(
                        f"Document body failed exact digest verification: {path}"
                    ) from exc
                if not metadata.present or metadata.byte_length is None:
                    raise ProjectionFormatError(
                        f"Document body is unavailable during projection: {path}"
                    )
                envelope_digest = document_digest(document).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=document.identity,
                        kind=document.kind,
                        format_tag=document.tag,
                        path=path,
                        artifact_digest=envelope_digest,
                        predecessor_digest=document.predecessor_digest,
                        revision=document.lifecycle.revision,
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=document.identity,
                        target_identity=pin.target_identity,
                        target_digest=pin.target_digest,
                    )
                    for pin in document.pins
                )
                subject = SemanticAddress.whole_artifact(path)
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.document.subject",
                            schema_version=1,
                            subject_identity=document.identity,
                            fact_key="whole_document",
                            value={
                                "address": subject.model_dump(mode="json"),
                                "body_digest": {"$digest": document.body_digest},
                                "envelope_digest": {"$digest": envelope_digest},
                                "input_digest": {"$digest": file_digest(content).tagged},
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.document.metadata",
                            schema_version=1,
                            subject_identity=document.identity,
                            fact_key="metadata",
                            value={
                                "authority": document.authority.model_dump(mode="json"),
                                "document_kind": document.document_kind,
                                "governance_scope": list(document.governance_scope),
                                "lifecycle": document.lifecycle.model_dump(mode="json"),
                                "media_type": document.media_type,
                                "title": document.title,
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.document.references",
                            schema_version=1,
                            subject_identity=document.identity,
                            fact_key="declared",
                            value={
                                "links": [item.model_dump(mode="json") for item in document.links],
                                "pins": [item.model_dump(mode="json") for item in document.pins],
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.document.source_mapping",
                            schema_version=1,
                            subject_identity=document.identity,
                            fact_key="whole_body",
                            value=whole_body_mapping(
                                path,
                                document.body_digest,
                                metadata.byte_length,
                            ).model_dump(mode="json"),
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.document.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_document_explanation_facts(
                            document_identity=document.identity,
                            document_path=path,
                            input_digest=file_digest(content).tagged,
                            artifact_digest=envelope_digest,
                            predecessor_digest=document.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "subject":
                try:
                    subject_shell = parse_subject(content, path=path)
                except SubjectFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered Subject failed strict validation: {path}"
                    ) from exc
                identity = subject_shell.qualified_identity
                previous = identities.get(identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {identity!r}: {previous} and {path}"
                    )
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = subject_digest(subject_shell).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="subject",
                        format_tag=subject_shell.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=subject_shell.lifecycle.predecessor_digest,
                        revision=_projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if subject_shell.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in subject_shell.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.subject.identity",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="stable_referent",
                            value={
                                "address": SemanticAddress.whole_artifact(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "identity": subject_shell.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "subject_id": subject_shell.subject_id,
                                "subject_kind": subject_shell.subject_kind,
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.subject.lifecycle",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="accepted_shell",
                            value={
                                "authority": subject_shell.authority.model_dump(mode="json"),
                                "lifecycle": subject_shell.lifecycle.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.subject.references",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="declared",
                            value={
                                "pins": [pin.model_dump(mode="json") for pin in subject_shell.pins]
                            },
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.subject.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_artifact_explanation_facts(
                            artifact_family="subject",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=subject_shell.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "claim-type":
                try:
                    claim_type = parse_claim_type(content, path=path)
                except ClaimTypeFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered ClaimType failed strict validation: {path}"
                    ) from exc
                identity = claim_type.identity.qualified
                previous = identities.get(identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {identity!r}: {previous} and {path}"
                    )
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = claim_type_digest(claim_type).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="claim-type",
                        format_tag=claim_type.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=claim_type.lifecycle.predecessor_digest,
                        revision=_projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if claim_type.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in claim_type.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.claim_type.identity",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="predicate_contract",
                            value={
                                "address": SemanticAddress.whole_artifact(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "identity": claim_type.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "structure": claim_type.structure.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.claim_type.policies",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="complete_policy",
                            value={
                                "admission": claim_type.admission_policy.model_dump(mode="json"),
                                "evidence_admission": (
                                    claim_type.evidence_admission_policy.model_dump(mode="json")
                                ),
                                "resolution": claim_type.resolution_policy.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.claim_type.references",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="declared",
                            value={
                                "authority": claim_type.authority.model_dump(mode="json"),
                                "lifecycle": claim_type.lifecycle.model_dump(mode="json"),
                                "pins": [pin.model_dump(mode="json") for pin in claim_type.pins],
                            },
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.claim_type.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_artifact_explanation_facts(
                            artifact_family="claim_type",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=claim_type.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "capture-contract":
                try:
                    capture_contract = parse_capture_contract(content, path=path)
                except CaptureFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered CaptureContract failed strict validation: {path}"
                    ) from exc
                identity = capture_contract.identity.qualified
                previous = identities.get(identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {identity!r}: {previous} and {path}"
                    )
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = capture_contract_digest(capture_contract).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="capture-contract",
                        format_tag=capture_contract.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=capture_contract.lifecycle.predecessor_digest,
                        revision=_projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if capture_contract.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in capture_contract.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.capture_contract.contract",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="evidence_contract",
                            value={
                                "address": SemanticAddress.whole_artifact(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "contract": capture_contract.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.capture_contract.references",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="declared",
                            value={
                                "pins": [
                                    pin.model_dump(mode="json") for pin in capture_contract.pins
                                ]
                            },
                        ),
                    )
                )
                continue
            if kind == "claim":
                try:
                    claim = parse_claim(content, path=path)
                except ClaimFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered Claim failed strict validation: {path}"
                    ) from exc
                identity = claim.identity.qualified
                previous = identities.get(identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {identity!r}: {previous} and {path}"
                    )
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = claim_artifact_digest(claim).tagged
                statement_digest = claim_statement_digest(claim.statement).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="claim",
                        format_tag=claim.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=claim.lifecycle.predecessor_digest,
                        revision=_projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if claim.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in claim.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.claim.identity",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="lineage",
                            value={
                                "artifact_digest": {"$digest": artifact_digest},
                                "identity": claim.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "statement_address": claim_statement_address(path).model_dump(
                                    mode="json"
                                ),
                                "statement_digest": {"$digest": statement_digest},
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.claim.statement",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="proposition",
                            value=claim.statement.model_dump(mode="json"),
                        ),
                        ProjectionFact(
                            schema_id="playbill.claim.backing",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="evidence",
                            value=claim.backing.model_dump(mode="json"),
                        ),
                        ProjectionFact(
                            schema_id="playbill.claim.lifecycle",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="accepted_revision",
                            value={
                                "authority": claim.authority.model_dump(mode="json"),
                                "lifecycle": claim.lifecycle.model_dump(mode="json"),
                                "pins": [pin.model_dump(mode="json") for pin in claim.pins],
                            },
                        ),
                    )
                )
                for index, source_mapping in enumerate(claim.backing.source_mappings):
                    semantic_facts.append(
                        ProjectionFact(
                            schema_id="playbill.claim.source_mapping",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key=f"source_{index:04d}",
                            value=source_mapping.model_dump(mode="json"),
                        )
                    )
                continue
            if kind == "fixture":
                artifact = FixtureArtifact.model_validate(payload)
                if _canonical_model_bytes(artifact) != content:
                    raise ProjectionFormatError(f"fixture artifact is not canonical: {path}")
                previous = identities.get(artifact.artifact_id)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {artifact.artifact_id!r}: "
                        f"{previous} and {path}"
                    )
                identities[artifact.artifact_id] = path
                digest = file_digest(content).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=artifact.artifact_id,
                        kind=artifact.kind,
                        format_tag=artifact.tag,
                        path=path,
                        artifact_digest=digest,
                        predecessor_digest=artifact.predecessor_digest,
                        revision=artifact.revision,
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=artifact.artifact_id,
                        target_identity=pin.target_identity,
                        target_digest=pin.target_digest,
                    )
                    for pin in artifact.pins
                )
                semantic_facts.extend(artifact.extension_facts)
                continue

            presentation = FixturePresentation.model_validate(payload)
            if _canonical_model_bytes(presentation) != content:
                raise ProjectionFormatError(f"presentation artifact is not canonical: {path}")
            presentation_facts.append(
                ProjectionFact(
                    schema_id="playbill.fixture.label",
                    schema_version=1,
                    subject_identity=presentation.subject_identity,
                    fact_key="label",
                    value=presentation.label,
                )
            )
        except ValidationError as exc:
            raise ProjectionFormatError(
                f"registered artifact failed strict validation: {path}"
            ) from exc

    validated_semantic = registry.validate(semantic_facts, classification="semantic")
    validated_presentation = registry.validate(
        presentation_facts,
        classification="presentation",
    )
    return ParsedProjectionTree(
        envelopes=tuple(sorted(envelopes, key=lambda item: item.identity.encode("utf-8"))),
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.source_identity.encode("utf-8"),
                    item.target_identity.encode("utf-8"),
                ),
            )
        ),
        retired_identities=tuple(sorted(retired_identities, key=lambda item: item.encode("utf-8"))),
        semantic_facts=validated_semantic,
        presentation_facts=validated_presentation,
    )


__all__ = [
    "ArtifactEnvelopeRow",
    "FixtureArtifact",
    "FixturePin",
    "FixturePresentation",
    "ParsedProjectionTree",
    "PLAYBILL_ARTIFACT_KINDS",
    "PLAYBILL_FORMAT_RESERVATIONS",
    "PinRow",
    "parse_projection_tree",
    "registered_path_kind",
]
