"""Registered Playbill artifact formats and normalized projection rows."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    approval_policy_digest,
    parse_approval_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactFormatRegistry,
    ArtifactFormatTag,
    ArtifactKindRegistry,
    ArtifactPathKind,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    canonical_bytes,
    file_digest,
    normalize_canonical,
)
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    claim_type_projection_structure,
    parse_claim_type,
)
from cruxible_client.contracts.documents import document_digest, parse_document
from cruxible_client.contracts.errors import (
    DocumentFormatError,
    PlaybillCasError,
    ProjectionFormatError,
    SettlementIntegrityError,
    SubjectFormatError,
)
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
)
from cruxible_client.contracts.semantic import (
    ContentSpan,
    SemanticAddress,
    SourceMapping,
    whole_body_mapping,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.cas import BodyAccessContext, BodyProjectionProtocol
from cruxible_core.playbill.explanation import (
    ProjectionCoordinateContext,
    accepted_artifact_explanation_facts,
    accepted_document_explanation_facts,
)

if TYPE_CHECKING:
    from cruxible_core.playbill.projection import AcceptedCoordinate
    from cruxible_core.playbill.settlement import ChangeSetRecordAnyVersion

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
PLAYBILL_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        ArtifactPathKind(
            "approval-policy",
            re.compile(r"^governance/approval-policy\.yaml$"),
        ),
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
            "provider",
            re.compile(r"^providers/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "source-acquisition-policy",
            re.compile(r"^source-acquisition-policies/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "standing-mandate",
            re.compile(r"^standing-mandates/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "claim",
            re.compile(r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.yaml$"),
        ),
        ArtifactPathKind(
            "procedure",
            re.compile(r"^procedures/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "line",
            re.compile(r"^lines/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "query-definition",
            re.compile(r"^query-definitions/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
        ),
        ArtifactPathKind(
            "exhaust-promotion",
            re.compile(r"^exhaust-promotions/[a-z][a-z0-9_.-]{0,255}\.yaml$"),
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
                "playbill-approval-policy-v1",
                "playbill-capture-contract-v1",
                "playbill-capture-envelope-v1",
                "playbill-claim-v2",
                "playbill-claim-v3",
                "playbill-accepted-state-run-input-v1",
                "playbill-exhaust-run-input-v1",
                "playbill-exhaust-promotion-v1",
                "playbill-landed-capture-run-input-v1",
                "playbill-line-slot-binding-v1",
                "playbill-line-v1",
                "playbill-procedure-pin-slot-ref-v1",
                "playbill-procedure-pin-slot-v1",
                "playbill-procedure-v1",
                "playbill-procedure-v2",
                "playbill-provider-v1",
                "playbill-query-definition-v1",
                "playbill-source-acquisition-policy-v1",
                "playbill-standing-mandate-v1",
            },
        )
        for tag in (
            "playbill-approval-policy-v1",
            "playbill-accepted-state-run-input-v1",
            "playbill-capture-contract-v1",
            "playbill-capture-envelope-v1",
            "playbill-claim-v2",
            "playbill-claim-v3",
            "playbill-exhaust-run-input-v1",
            "playbill-exhaust-promotion-v1",
            "playbill-landed-capture-run-input-v1",
            "playbill-line-slot-binding-v1",
            "playbill-line-v1",
            "playbill-procedure-pin-slot-v1",
            "playbill-procedure-pin-slot-ref-v1",
            "playbill-procedure-v1",
            "playbill-procedure-v2",
            "playbill-provider-v1",
            "playbill-query-definition-v1",
            "playbill-source-acquisition-policy-v1",
            "playbill-standing-mandate-v1",
        )
    )
)

RegisteredPathKind = Literal[
    "approval-policy",
    "capture-contract",
    "changeset",
    "claim",
    "claim-type",
    "document",
    "exhaust-promotion",
    "fixture",
    "line",
    "presentation",
    "principal",
    "procedure",
    "provider",
    "query-definition",
    "source-acquisition-policy",
    "standing-mandate",
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


def projected_revision(
    records: tuple[tuple[str, ChangeSetRecordAnyVersion], ...],
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


def _accepted_artifact_timestamp(
    records: tuple[tuple[str, ChangeSetRecordAnyVersion], ...],
    *,
    path: str,
    artifact_digest: str,
) -> datetime | None:
    """Return the signed C_s time of the change set that accepted this exact revision."""

    for _record_path, record in reversed(records):
        if not any(
            member.path == path
            and getattr(member, "candidate_artifact_digest", None) == artifact_digest
            for member in record.members
        ):
            continue
        return datetime.strptime(record.candidate.timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    return None


def _accepted_artifact_coordinate(
    records: tuple[tuple[str, ChangeSetRecordAnyVersion], ...],
    *,
    path: str,
    artifact_digest: str,
    coordinates_by_sequence: Mapping[int, AcceptedCoordinate],
) -> AcceptedCoordinate | None:
    """Resolve the immutable generation that accepted one exact artifact revision."""

    for _record_path, record in reversed(records):
        if any(
            member.path == path
            and getattr(member, "candidate_artifact_digest", None) == artifact_digest
            for member in record.members
        ):
            return coordinates_by_sequence.get(record.sequence)
    return None


def _current_member_law_result(
    records: tuple[tuple[str, ChangeSetRecordAnyVersion], ...],
    *,
    path: str,
    artifact_digest: str,
) -> dict[str, object] | None:
    """Return the exact accepted law result for one current artifact revision."""

    for _record_path, record in reversed(records):
        if not any(
            member.path == path
            and getattr(member, "candidate_artifact_digest", None) == artifact_digest
            for member in record.members
        ):
            continue
        for evidence in getattr(record, "law_evidence", ()):
            if evidence.path == path:
                return dict(evidence.result)
    return None


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


def _whole_semantic_mapping(
    address: SemanticAddress,
    *,
    content_digest: str,
    byte_length: int,
) -> SourceMapping:
    return SourceMapping(
        subject=address,
        spans=(
            ContentSpan(
                content_digest=content_digest,
                start_byte=0,
                end_byte=byte_length,
            ),
        ),
    )


def _procedure_node_span(
    content: bytes,
    node: BaseModel,
    *,
    content_digest: str,
) -> ContentSpan:
    encoded = canonical_bytes(node.model_dump(mode="json", by_alias=True))
    start = content.find(encoded)
    if start < 0 or content.find(encoded, start + 1) >= 0:
        raise ProjectionFormatError("Procedure node bytes do not have one exact source occurrence")
    return ContentSpan(
        content_digest=content_digest,
        start_byte=start,
        end_byte=start + len(encoded),
    )


def parse_projection_tree(
    blobs: dict[str, bytes],
    *,
    registry: ProjectionExtensionRegistry,
    bodies: BodyProjectionProtocol | None = None,
    coordinate: ProjectionCoordinateContext | None = None,
    accepted_coordinates_by_sequence: Mapping[int, AcceptedCoordinate] | None = None,
) -> ParsedProjectionTree:
    """Parse all registered blobs and produce one sorted, typed row stream."""

    from cruxible_client.contracts.captures import (
        CaptureFormatError,
        capture_contract_digest,
        parse_capture_contract,
    )
    from cruxible_client.contracts.claim_verdicts import claim_verdict_v1_compat
    from cruxible_client.contracts.claims import (
        ClaimArtifactV3,
        ClaimFormatError,
        claim_artifact_digest,
        claim_statement_address,
        claim_statement_digest,
        parse_claim,
        parse_claim_law_evidence,
    )
    from cruxible_core.playbill.projection import AcceptedCoordinate
    from cruxible_core.playbill.settlement import parse_change_set_record

    envelopes: list[ArtifactEnvelopeRow] = []
    pins: list[PinRow] = []
    retired_identities: list[str] = []
    semantic_facts: list[ProjectionFact] = []
    presentation_facts: list[ProjectionFact] = []
    identities: dict[str, str] = {}
    change_sets: list[tuple[str, ChangeSetRecordAnyVersion]] = []

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
    accepted_coordinates = dict(accepted_coordinates_by_sequence or {})
    if coordinate is not None and change_sets:
        latest_sequence = change_sets[-1][1].sequence
        accepted_coordinates.setdefault(
            latest_sequence,
            AcceptedCoordinate(
                git_oid=coordinate.git_oid,
                semantic_root=coordinate.semantic_root,
                generation_root=coordinate.generation_root,
                compiler_digest=coordinate.compiler_digest,
            ),
        )

    for path in sorted(blobs, key=lambda item: item.encode("utf-8")):
        content = blobs[path]
        kind = registered_path_kind(path)
        payload = _load_object(content, path=path)
        try:
            if kind == "approval-policy":
                policy = parse_approval_policy(content, path=path)
                digest = approval_policy_digest(policy).tagged
                identities[APPROVAL_POLICY_IDENTITY] = path
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=APPROVAL_POLICY_IDENTITY,
                        kind="approval-policy",
                        format_tag=policy.tag,
                        path=path,
                        artifact_digest=digest,
                        predecessor_digest=None,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=file_digest(content).tagged,
                            artifact_digest=digest,
                        ),
                    )
                )
                continue
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
                        revision=projected_revision(
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
                        revision=projected_revision(
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
                                "structure": claim_type_projection_structure(claim_type),
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
            if kind == "provider":
                from cruxible_client.contracts.providers import parse_provider, provider_digest

                provider = parse_provider(content, path=path)
                identity = provider.identity.qualified
                previous = identities.get(identity)
                if previous is not None:
                    raise ProjectionFormatError(
                        f"duplicate semantic identity {identity!r}: {previous} and {path}"
                    )
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = provider_digest(provider).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="provider",
                        format_tag=provider.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=provider.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in provider.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.provider.identity",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="provider",
                            value={
                                "address": SemanticAddress.whole_artifact(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "identity": provider.identity.model_dump(mode="json"),
                                "lifecycle": provider.lifecycle.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.provider.keys",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="verification",
                            value={
                                "signing_keys": [
                                    item.model_dump(mode="json") for item in provider.signing_keys
                                ]
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.provider.provenance",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="control",
                            value={
                                "control_domain": provider.control_domain,
                                "upstream_provenance": [
                                    item.model_dump(mode="json")
                                    for item in provider.upstream_provenance
                                ],
                            },
                        ),
                    )
                )
                continue
            if kind == "source-acquisition-policy":
                from cruxible_client.contracts.acquisition_policies import (
                    acquisition_policy_digest,
                    parse_acquisition_policy,
                )

                policy = parse_acquisition_policy(content, path=path)
                identity = policy.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = acquisition_policy_digest(policy).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="source-acquisition-policy",
                        format_tag=policy.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=policy.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in policy.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="playbill.source_acquisition_policy.policy",
                        schema_version=1,
                        subject_identity=identity,
                        fact_key="complete_policy",
                        value=policy.model_dump(mode="json"),
                    )
                )
                continue
            if kind == "standing-mandate":
                from cruxible_client.contracts.standing_mandates import (
                    parse_standing_mandate,
                    standing_mandate_digest,
                )

                mandate = parse_standing_mandate(content, path=path)
                identity = mandate.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = standing_mandate_digest(mandate).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="standing-mandate",
                        format_tag=mandate.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=mandate.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in mandate.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="playbill.standing_mandate.authority",
                        schema_version=1,
                        subject_identity=identity,
                        fact_key="finite_grant",
                        value=mandate.model_dump(mode="json"),
                    )
                )
                continue
            if kind == "procedure":
                from cruxible_client.contracts.procedures.artifacts import (
                    parse_procedure,
                    procedure_artifact_digest,
                )
                from cruxible_client.contracts.procedures.graph import (
                    analyze_procedure_v3,
                    compute_procedure_node_digests_v3,
                )

                procedure = parse_procedure(content, path=path)
                identity = procedure.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = procedure_artifact_digest(procedure).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="procedure",
                        format_tag=procedure.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=procedure.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if procedure.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in procedure.pins
                )
                graph = analyze_procedure_v3(procedure.definition)
                node_digests = compute_procedure_node_digests_v3(procedure.definition)
                mappings: list[tuple[str, SourceMapping]] = [
                    (
                        "unit",
                        _whole_semantic_mapping(
                            SemanticAddress.procedure_unit(path),
                            content_digest=input_digest,
                            byte_length=len(content),
                        ),
                    )
                ]
                for node in procedure.definition.nodes:
                    span = _procedure_node_span(
                        content,
                        node,
                        content_digest=input_digest,
                    )
                    mappings.append(
                        (
                            f"node.{node.node_id}",
                            SourceMapping(
                                subject=SemanticAddress.procedure_node(path, node.node_id),
                                spans=(span,),
                            ),
                        )
                    )
                    for label, target in graph.edges[node.node_id].items():
                        mappings.append(
                            (
                                f"arm.{len(mappings):04d}",
                                SourceMapping(
                                    subject=SemanticAddress.procedure_arm(
                                        path,
                                        from_node_id=node.node_id,
                                        arm_label=cast(
                                            Literal["next", "on_true", "on_false"], label
                                        ),
                                        target_node_id=target,
                                    ),
                                    spans=(span,),
                                ),
                            )
                        )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.procedure.definition",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="definition",
                            value={
                                "address": SemanticAddress.procedure_unit(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "definition_digest": {"$digest": procedure.definition_digest},
                                "directly_runnable": procedure.directly_runnable,
                                "identity": procedure.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "measurements": [
                                    measurement.model_dump(mode="json")
                                    for measurement in procedure.definition.measurements
                                ],
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.procedure.graph",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="graph_v3",
                            value={
                                "edges": graph.edges,
                                "nodes": [
                                    {
                                        "address": SemanticAddress.procedure_node(
                                            path, node_id
                                        ).model_dump(mode="json"),
                                        "kind": graph.kinds[node_id],
                                        "local_digest": {
                                            "$digest": node_digests[node_id].local_digest
                                        },
                                        "node_id": node_id,
                                        "subtree_digest": {
                                            "$digest": node_digests[node_id].subtree_digest
                                        },
                                    }
                                    for node_id in graph.node_ids
                                ],
                                "pin_slots": [
                                    slot.model_dump(mode="json")
                                    for slot in procedure.definition.pin_slots
                                ],
                            },
                        ),
                        *(
                            ProjectionFact(
                                schema_id="playbill.procedure.source_mapping",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key=fact_key,
                                value=mapping.model_dump(mode="json"),
                            )
                            for fact_key, mapping in mappings
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.procedure.resolution_activation",
                    1,
                    classification="semantic",
                ):
                    from cruxible_client.contracts.procedures.artifacts import (
                        AcceptedProcedureV1,
                    )
                    from cruxible_core.playbill.procedures.resolution import (
                        derive_resolution_activations,
                    )

                    activated_at = _accepted_artifact_timestamp(
                        accepted_change_sets,
                        path=path,
                        artifact_digest=artifact_digest,
                    )
                    accepting_coordinate = _accepted_artifact_coordinate(
                        accepted_change_sets,
                        path=path,
                        artifact_digest=artifact_digest,
                        coordinates_by_sequence=accepted_coordinates,
                    )
                    if activated_at is not None and accepting_coordinate is not None:
                        activations = derive_resolution_activations(
                            AcceptedProcedureV1(
                                path=path,
                                procedure=procedure,
                                artifact_digest=artifact_digest,
                            ),
                            accepted_coordinate=accepting_coordinate,
                            activated_at=activated_at,
                        )
                        semantic_facts.extend(
                            ProjectionFact(
                                schema_id="playbill.procedure.resolution_activation",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key=activation.measurement_name,
                                value=activation.model_dump(mode="json"),
                            )
                            for activation in activations
                        )
                    elif procedure.definition.measurements:
                        raise ProjectionFormatError(
                            "Procedure measurement activation lacks its accepting coordinate"
                        )
                if coordinate is not None and registry.supports(
                    "playbill.procedure.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_artifact_explanation_facts(
                            artifact_family="procedure",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=procedure.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "line":
                from cruxible_client.contracts.procedures.line_specs import (
                    line_spec_digest,
                    parse_line_spec,
                )

                line = parse_line_spec(content, path=path)
                identity = line.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = line_spec_digest(line).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="line",
                        format_tag=line.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=line.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if line.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in line.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.line.spec",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="instantiation",
                            value={
                                "address": SemanticAddress.line(path).model_dump(mode="json"),
                                "artifact_digest": {"$digest": artifact_digest},
                                "input_digest": {"$digest": input_digest},
                                "line": line.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.line.source_mapping",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="line",
                            value=_whole_semantic_mapping(
                                SemanticAddress.line(path),
                                content_digest=input_digest,
                                byte_length=len(content),
                            ).model_dump(mode="json"),
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.line.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_artifact_explanation_facts(
                            artifact_family="line",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=line.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "query-definition":
                from cruxible_client.contracts.query.definitions import (
                    parse_query_definition,
                    query_definition_digest,
                )

                query = parse_query_definition(content, path=path)
                identity = query.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = query_definition_digest(query).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="query-definition",
                        format_tag=query.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=query.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if query.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in query.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="playbill.query_definition.definition",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="declaration",
                            value={
                                "address": SemanticAddress.whole_artifact(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "identity": query.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "query": query.model_dump(mode="json"),
                                "referenced_predicates": list(query.referenced_predicates),
                                "subject_kinds": list(query.subject_kinds),
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.query_definition.policy",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="evaluation",
                            value={
                                "default_budgets": query.default_budgets.model_dump(mode="json"),
                                "evaluation_policy": query.evaluation_policy.model_dump(
                                    mode="json"
                                ),
                                "maximum_budgets": query.maximum_budgets.model_dump(mode="json"),
                                "result_cardinality": query.result_cardinality,
                                "result_shape": query.result_shape,
                            },
                        ),
                        ProjectionFact(
                            schema_id="playbill.query_definition.references",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="declared",
                            value={
                                "lifecycle": query.lifecycle.model_dump(mode="json"),
                                "pins": [pin.model_dump(mode="json") for pin in query.pins],
                            },
                        ),
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.query_definition.attestation_coverage",
                    1,
                    classification="semantic",
                ):
                    semantic_facts.extend(
                        accepted_artifact_explanation_facts(
                            artifact_family="query_definition",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=query.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                continue
            if kind == "exhaust-promotion":
                from cruxible_core.playbill.exhaust.promotions import (
                    AcceptedExhaustPromotionV1,
                    exhaust_promotion_digest,
                    parse_exhaust_promotion,
                    procedure_track_record_facts,
                )

                promotion = parse_exhaust_promotion(content, path=path)
                identity = promotion.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = exhaust_promotion_digest(promotion)
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="exhaust-promotion",
                        format_tag=promotion.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=promotion.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if promotion.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                    )
                    for pin in promotion.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="playbill.exhaust_promotion.basis",
                        schema_version=1,
                        subject_identity=identity,
                        fact_key="verified_range",
                        value={
                            "artifact_digest": {"$digest": artifact_digest},
                            "input_digest": {"$digest": input_digest},
                            "promotion": promotion.model_dump(mode="json"),
                        },
                    )
                )
                if coordinate is not None and registry.supports(
                    "playbill.procedure.track_record",
                    1,
                    classification="semantic",
                ):
                    if bodies is None:
                        raise ProjectionFormatError(
                            "ExhaustPromotion projection requires its canonical output CAS object"
                        )
                    accepted_coordinate = _accepted_artifact_coordinate(
                        accepted_change_sets,
                        path=path,
                        artifact_digest=artifact_digest,
                        coordinates_by_sequence=accepted_coordinates,
                    )
                    if accepted_coordinate is None:
                        raise ProjectionFormatError(
                            "ExhaustPromotion projection lacks its accepting coordinate"
                        )
                    try:
                        output = normalize_canonical(
                            json.loads(
                                bodies.read(
                                    promotion.output_digest,
                                    access=BodyAccessContext(
                                        principal_id="playbill-projection",
                                        can_read_body=True,
                                    ),
                                )
                            )
                        )
                    except (PlaybillCasError, UnicodeDecodeError, ValueError) as exc:
                        raise ProjectionFormatError(
                            "ExhaustPromotion canonical output is missing or malformed"
                        ) from exc
                    accepted_promotion = AcceptedExhaustPromotionV1(
                        path=path,
                        promotion=promotion,
                        artifact_digest=artifact_digest,
                        accepted_coordinate=accepted_coordinate,
                    )
                    semantic_facts.extend(
                        procedure_track_record_facts(accepted_promotion, output=output)
                    )
                    if registry.supports(
                        "playbill.line.track_record",
                        1,
                        classification="semantic",
                    ):
                        from cruxible_core.playbill.exhaust.line_track_records import (
                            LineTrackRecordError,
                            line_track_record_facts,
                        )

                        try:
                            semantic_facts.extend(
                                line_track_record_facts(accepted_promotion, output=output)
                            )
                        except LineTrackRecordError as exc:
                            raise ProjectionFormatError(
                                "ExhaustPromotion declares an unprojectable Line track record"
                            ) from exc
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
                        revision=projected_revision(
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
                        revision=projected_revision(
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
                                "lifecycle": claim.lifecycle.model_dump(mode="json"),
                                "pins": [pin.model_dump(mode="json") for pin in claim.pins],
                                **(
                                    {"retirement": claim.retirement.model_dump(mode="json")}
                                    if isinstance(claim, ClaimArtifactV3)
                                    else {}
                                ),
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
                if coordinate is not None and registry.supports(
                    "playbill.claim.current_verdict",
                    1,
                    classification="semantic",
                ):
                    explanation_facts = list(
                        accepted_artifact_explanation_facts(
                            artifact_family="claim",
                            subject_identity=identity,
                            artifact_path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                            predecessor_digest=claim.lifecycle.predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                        )
                    )
                    raw_result = _current_member_law_result(
                        accepted_change_sets,
                        path=path,
                        artifact_digest=artifact_digest,
                    )
                    raw_claim_evidence = (
                        None if raw_result is None else raw_result.get("claim_evidence")
                    )
                    if raw_claim_evidence is None:
                        raise ProjectionFormatError(
                            f"accepted Claim has no exact law evidence: {path}"
                        )
                    law_evidence = parse_claim_law_evidence(raw_claim_evidence)
                    for index, fact in enumerate(explanation_facts):
                        if fact.schema_id != "playbill.claim.attestation_coverage":
                            continue
                        if not isinstance(fact.value, dict):
                            raise ProjectionFormatError(
                                "Claim attestation coverage projection is malformed"
                            )
                        value = dict(fact.value)
                        value["claim_attestations"] = [
                            item.model_dump(mode="json")
                            for item in law_evidence.verified_attestations
                        ]
                        explanation_facts[index] = fact.model_copy(update={"value": value})
                    semantic_facts.extend(explanation_facts)
                    if law_evidence.verdict_result is None:
                        raise ProjectionFormatError(
                            f"accepted PC-C Claim has no verdict result: {path}"
                        )
                    semantic_facts.extend(
                        (
                            ProjectionFact(
                                schema_id="playbill.claim.current_verdict",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="accepted_evaluation",
                                value=claim_verdict_v1_compat(
                                    law_evidence.verdict_result
                                ).model_dump(mode="json"),
                            ),
                            ProjectionFact(
                                schema_id="playbill.claim.evidence_basis",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="accepted_evaluation",
                                value={
                                    "admissions": list(law_evidence.evidence_basis),
                                    "attestations": [
                                        item.model_dump(mode="json")
                                        for item in law_evidence.verified_attestations
                                    ],
                                    "verdict_evidence": {
                                        "contradicting": list(
                                            law_evidence.verdict_result.contradicting_evidence_digests
                                        ),
                                        "supporting": list(
                                            law_evidence.verdict_result.supporting_evidence_digests
                                        ),
                                        "unsure": list(
                                            law_evidence.verdict_result.unsure_evidence_digests
                                        ),
                                    },
                                },
                            ),
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

    pin_dependencies: dict[tuple[str, str], PinRow] = {}
    for pin in pins:
        key = (pin.source_identity, pin.target_identity)
        previous_pin = pin_dependencies.get(key)
        if previous_pin is not None and previous_pin.target_digest != pin.target_digest:
            raise ProjectionFormatError(
                "one artifact pins the same dependency identity at conflicting digests"
            )
        pin_dependencies[key] = pin

    validated_semantic = registry.validate(semantic_facts, classification="semantic")
    validated_presentation = registry.validate(
        presentation_facts,
        classification="presentation",
    )
    return ParsedProjectionTree(
        envelopes=tuple(sorted(envelopes, key=lambda item: item.identity.encode("utf-8"))),
        pins=tuple(
            sorted(
                pin_dependencies.values(),
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
    "projected_revision",
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
