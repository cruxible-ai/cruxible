"""Registered Cruxible artifact formats and normalized projection rows."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Mapping, cast

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    approval_policy_digest,
    parse_approval_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactKindRegistry,
    ArtifactPathKind,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    artifact_bytes_for_path,
    canonical_bytes,
    file_digest,
    is_candidate_card_path,
    normalize_canonical,
    pretty_canonical_bytes,
)
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    claim_type_projection_structure,
    parse_claim_type,
)
from cruxible_client.contracts.documents import document_digest, parse_document
from cruxible_client.contracts.errors import (
    CasError,
    DocumentFormatError,
    ProjectionFormatError,
    SettlementIntegrityError,
    SubjectFormatError,
)
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    parse_procedure_runtime_policy,
    procedure_runtime_policy_digest,
)
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
from cruxible_core.query.explanation import (
    ProjectionCoordinateContext,
    accepted_artifact_explanation_facts,
    accepted_document_explanation_facts,
)
from cruxible_core.storage.cas import BodyAccessContext, BodyProjectionProtocol

if TYPE_CHECKING:
    from cruxible_client.contracts.claims import ClaimArtifactAny
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
P2_B0_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        ArtifactPathKind(
            "approval-policy",
            re.compile(r"^governance/approval-policy\.yaml$"),
        ),
        ArtifactPathKind(
            "procedure-runtime-policy",
            re.compile(r"^governance/procedure-runtime-policy\.yaml$"),
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
            "changeset",
            re.compile(r"^changesets/cs-[0-9]{20}\.json$"),
        ),
    )
)
PLAYBILL_ARTIFACT_KINDS = ArtifactKindRegistry(
    tuple(
        ArtifactPathKind(
            entry.kind,
            re.compile(entry.pattern.pattern.replace(r"\.yaml", r"\.json")),
        )
        for entry in P2_B0_ARTIFACT_KINDS.entries()
    )
)
P2_B1_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *PLAYBILL_ARTIFACT_KINDS.entries(),
        ArtifactPathKind(
            "provider-interface",
            re.compile(r"^provider-interfaces/[a-z][a-z0-9_.-]{0,255}\.json$"),
        ),
    )
)
P2_C_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *P2_B1_ARTIFACT_KINDS.entries(),
        ArtifactPathKind(
            "procedure-mandate",
            re.compile(r"^procedure-mandates/[a-z][a-z0-9_.-]{0,255}\.json$"),
        ),
    )
)

ATTESTATION_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *P2_C_ARTIFACT_KINDS.entries(),
        ArtifactPathKind(
            "attestation", re.compile(r"^attestations/[0-9a-f]{2}/[0-9a-f]{64}\.json$")
        ),
    )
)

RESOLUTION_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *ATTESTATION_ARTIFACT_KINDS.entries(),
        ArtifactPathKind(
            "resolution-contract",
            re.compile(r"^resolution-contracts/[a-z][a-z0-9_.-]{0,255}\.json$"),
        ),
    )
)

ONTOLOGY_ARTIFACT_KINDS = ArtifactKindRegistry(RESOLUTION_ARTIFACT_KINDS.entries())
UPGRADE_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *ONTOLOGY_ARTIFACT_KINDS.entries(),
        ArtifactPathKind("compiler-upgrade", re.compile(r"^compiler-upgrade\.json$")),
    )
)

PROVIDER_CONTRACT_ARTIFACT_KINDS = ArtifactKindRegistry(UPGRADE_ARTIFACT_KINDS.entries())
PROVIDER_PACKAGE_ARTIFACT_KINDS = ArtifactKindRegistry(PROVIDER_CONTRACT_ARTIFACT_KINDS.entries())
RESOURCE_BUDGET_ARTIFACT_KINDS = ArtifactKindRegistry(PROVIDER_PACKAGE_ARTIFACT_KINDS.entries())
SDK_SOURCE_ARTIFACT_KINDS = ArtifactKindRegistry(RESOURCE_BUDGET_ARTIFACT_KINDS.entries())
CLAIM_EVIDENCE_ARTIFACT_KINDS = ArtifactKindRegistry(SDK_SOURCE_ARTIFACT_KINDS.entries())
SOURCE_CHECKED_ARTIFACT_KINDS = ArtifactKindRegistry(CLAIM_EVIDENCE_ARTIFACT_KINDS.entries())
TRIGGER_CAPTURE_ARTIFACT_KINDS = ArtifactKindRegistry(SOURCE_CHECKED_ARTIFACT_KINDS.entries())
AUTHORITY_VERBS_ARTIFACT_KINDS = ArtifactKindRegistry(TRIGGER_CAPTURE_ARTIFACT_KINDS.entries())
GOVERNED_TRIGGERS_ARTIFACT_KINDS = ArtifactKindRegistry(
    (
        *AUTHORITY_VERBS_ARTIFACT_KINDS.entries(),
        ArtifactPathKind("trigger", re.compile(r"^triggers/[a-z][a-z0-9_.-]{0,255}\.json$")),
        ArtifactPathKind("blueprint", re.compile(r"^blueprints/[a-z][a-z0-9_.-]{0,255}\.json$")),
    )
)
_REVISION_31_AND_LATER = (AUTHORITY_VERBS_ARTIFACT_KINDS, GOVERNED_TRIGGERS_ARTIFACT_KINDS)


RegisteredPathKind = Literal[
    "compiler-upgrade",
    "resolution-contract",
    "attestation",
    "approval-policy",
    "blueprint",
    "procedure-runtime-policy",
    "capture-contract",
    "changeset",
    "claim",
    "claim-type",
    "document",
    "exhaust-promotion",
    "line",
    "principal",
    "procedure",
    "procedure-mandate",
    "provider",
    "provider-interface",
    "query-definition",
    "source-acquisition-policy",
    "subject",
    "trigger",
]


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
    # The pin's role, which every dependency edge carries.
    role: str


@dataclass(frozen=True)
class ParsedProjectionTree:
    envelopes: tuple[ArtifactEnvelopeRow, ...]
    pins: tuple[PinRow, ...]
    retired_identities: tuple[str, ...]
    semantic_facts: tuple[ProjectionFact, ...]


def registered_path_kind(
    path: str,
    *,
    artifact_kinds: ArtifactKindRegistry,
) -> RegisteredPathKind:
    return cast(RegisteredPathKind, artifact_kinds.resolve_path(path))


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
    payload = node.model_dump(mode="json", by_alias=True)
    compact = canonical_bytes(payload)
    pretty = pretty_canonical_bytes(payload).removesuffix(b"\n").replace(b"\n", b"\n      ")
    matches = [
        (start, encoded)
        for encoded in (compact, pretty)
        if (start := content.find(encoded)) >= 0 and content.find(encoded, start + 1) < 0
    ]
    if len(matches) != 1:
        raise ProjectionFormatError("Procedure node bytes do not have one exact source occurrence")
    start, encoded = matches[0]
    return ContentSpan(
        content_digest=content_digest,
        start_byte=start,
        end_byte=start + len(encoded),
    )


def _claim_static_facts(
    claim: ClaimArtifactAny,
    *,
    path: str,
    input_digest: str,
    artifact_digest: str,
    statement_digest: str,
) -> tuple[ProjectionFact, ...]:
    """Compile only byte-dependent Claim facts; acceptance proofs stay per-build."""
    from cruxible_client.contracts.claims import ClaimArtifact, claim_statement_address

    identity = claim.identity.qualified
    facts: list[ProjectionFact] = []
    facts.extend(
        (
            ProjectionFact(
                schema_id="cruxible.claim.identity",
                schema_version=1,
                subject_identity=identity,
                fact_key="lineage",
                value={
                    "artifact_digest": {"$digest": artifact_digest},
                    "identity": claim.identity.model_dump(mode="json"),
                    "input_digest": {"$digest": input_digest},
                    "statement_address": claim_statement_address(path).model_dump(mode="json"),
                    "statement_digest": {"$digest": statement_digest},
                },
            ),
            ProjectionFact(
                schema_id="cruxible.claim.statement",
                schema_version=1,
                subject_identity=identity,
                fact_key="proposition",
                value=claim.statement.model_dump(mode="json"),
            ),
            ProjectionFact(
                schema_id="cruxible.claim.backing",
                schema_version=1,
                subject_identity=identity,
                fact_key="evidence",
                value=claim.backing.model_dump(mode="json"),
            ),
            ProjectionFact(
                schema_id="cruxible.claim.lifecycle",
                schema_version=1,
                subject_identity=identity,
                fact_key="accepted_revision",
                value={
                    "lifecycle": claim.lifecycle.model_dump(mode="json"),
                    "pins": [pin.model_dump(mode="json") for pin in claim.pins],
                    **(
                        {"retirement": claim.retirement.model_dump(mode="json")}
                        if isinstance(claim, ClaimArtifact)
                        else {}
                    ),
                },
            ),
        )
    )
    for index, source_mapping in enumerate(claim.backing.source_mappings):
        facts.append(
            ProjectionFact(
                schema_id="cruxible.claim.source_mapping",
                schema_version=1,
                subject_identity=identity,
                fact_key=f"source_{index:04d}",
                value=source_mapping.model_dump(mode="json"),
            )
        )
    return tuple(facts)


def _requires_resource_budgets(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            (key == "max_result_bytes" and item is not None)
            or (
                key in {"max_repeat_attempts", "max_attempts"}
                and isinstance(item, int)
                and item > 25
            )
            or _requires_resource_budgets(item)
            for key, item in value.items()
        )
    return isinstance(value, (list, tuple)) and any(
        _requires_resource_budgets(item) for item in value
    )


def parse_projection_tree(
    blobs: Mapping[str, bytes],
    *,
    registry: ProjectionExtensionRegistry,
    artifact_kinds: ArtifactKindRegistry,
    artifact_codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
    bodies: BodyProjectionProtocol | None = None,
    coordinate: ProjectionCoordinateContext | None = None,
    accepted_coordinates_by_sequence: Mapping[int, AcceptedCoordinate] | None = None,
    verified_change_sets: tuple[tuple[str, ChangeSetRecordAnyVersion], ...] | None = None,
    selected_member_history: tuple[tuple[str, ChangeSetRecordAnyVersion], ...] | None = None,
) -> ParsedProjectionTree:
    """Parse all registered blobs and produce one sorted, typed row stream.

    The path grammar is the caller's compiler's, always named: no default can
    know which artifact kinds a given tree's compiler admits.
    """

    from cruxible_client.contracts.captures import (
        CaptureFormatError,
        capture_contract_digest,
        parse_capture_contract,
    )
    from cruxible_client.contracts.claim_verdicts import claim_verdict_v1_compat
    from cruxible_client.contracts.claims import (
        ClaimFormatError,
        claim_artifact_digest,
        claim_statement_digest,
        parse_claim,
        parse_claim_law_evidence,
    )
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.proposals.settlement import parse_change_set_record

    envelopes: list[ArtifactEnvelopeRow] = []
    pins: list[PinRow] = []
    retired_identities: list[str] = []
    semantic_facts: list[ProjectionFact] = []
    identities: dict[str, str] = {}
    change_sets: list[tuple[str, ChangeSetRecordAnyVersion]] = []

    if selected_member_history is not None:
        # Only a bound typed reader supplies sparse history already verified by C.
        change_sets = list(selected_member_history)
    elif verified_change_sets is not None:
        # Internal activation supplies the already verified parent prefix plus
        # its verified successor. Public/recovery parsing always uses blob bytes.
        change_sets = list(verified_change_sets)
    else:
        for path in sorted(blobs, key=lambda item: item.encode("utf-8")):
            if is_candidate_card_path(path):
                continue
            if registered_path_kind(path, artifact_kinds=artifact_kinds) != "changeset":
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
    if selected_member_history is None and [
        record.sequence for _path, record in change_sets
    ] != list(range(1, len(change_sets) + 1)):
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
        if is_candidate_card_path(path):
            continue
        content = blobs[path]
        kind = registered_path_kind(path, artifact_kinds=artifact_kinds)
        payload = _load_object(content, path=path)
        try:
            if kind == "approval-policy":
                policy = parse_approval_policy(content, path=path, codec=artifact_codec)
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
            if kind == "procedure-runtime-policy":
                if not registry.supports_artifact_kind(kind):
                    raise ProjectionFormatError(
                        "compiler coordinate does not recognize ProcedureRuntimePolicy"
                    )
                runtime_policy = parse_procedure_runtime_policy(
                    content, path=path, codec=artifact_codec
                )
                if artifact_kinds not in (
                    RESOURCE_BUDGET_ARTIFACT_KINDS,
                    SDK_SOURCE_ARTIFACT_KINDS,
                    CLAIM_EVIDENCE_ARTIFACT_KINDS,
                    SOURCE_CHECKED_ARTIFACT_KINDS,
                    TRIGGER_CAPTURE_ARTIFACT_KINDS,
                    AUTHORITY_VERBS_ARTIFACT_KINDS,
                    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                ) and (
                    runtime_policy.result_bytes_cap is not None
                    or runtime_policy.repeat_attempts_cap is not None
                ):
                    raise ProjectionFormatError(
                        "resource policy budgets require compiler revision 26"
                    )
                digest = procedure_runtime_policy_digest(runtime_policy).tagged
                identities[PROCEDURE_RUNTIME_POLICY_IDENTITY] = path
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=PROCEDURE_RUNTIME_POLICY_IDENTITY,
                        kind="procedure-runtime-policy",
                        format_tag=runtime_policy.tag,
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
            if kind == "blueprint":
                from cruxible_client.contracts.procedures.blueprints import (
                    blueprint_digest,
                    parse_blueprint,
                )
                from cruxible_client.contracts.procedures.source_compiler import (
                    verify_source_graph,
                )

                blueprint = parse_blueprint(content, path=path, codec=artifact_codec)
                verify_source_graph(blueprint)
                identity = blueprint.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                digest = blueprint_digest(blueprint).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        blueprint.artifact_format,
                        path,
                        digest,
                        blueprint.lifecycle.predecessor_digest,
                        projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=file_digest(content).tagged,
                            artifact_digest=digest,
                        ),
                    )
                )
                if blueprint.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                        role=pin.role,
                    )
                    for pin in blueprint.pins
                )
                continue
            if kind == "trigger":
                from cruxible_client.contracts.triggers import parse_trigger, trigger_digest

                trigger = parse_trigger(content, path=path, codec=artifact_codec)
                identity = trigger.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                digest = trigger_digest(trigger).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        trigger.artifact_format,
                        path,
                        digest,
                        trigger.lifecycle.predecessor_digest,
                        projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=file_digest(content).tagged,
                            artifact_digest=digest,
                        ),
                    )
                )
                if trigger.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                        role=pin.role,
                    )
                    for pin in trigger.pins
                )
                continue
            if kind == "resolution-contract":
                from cruxible_client.contracts.resolution_contracts import (
                    parse_resolution_contract,
                    resolution_contract_digest,
                )

                contract = parse_resolution_contract(content, path=path, codec=artifact_codec)
                identity = contract.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                digest = resolution_contract_digest(contract).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        contract.artifact_format,
                        path,
                        digest,
                        contract.lifecycle.predecessor_digest,
                        projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=file_digest(content).tagged,
                            artifact_digest=digest,
                        ),
                    )
                )
                if contract.lifecycle.state == "retired":
                    retired_identities.append(identity)
                continue
            if kind == "attestation":
                from cruxible_client.contracts.accepted_attestations import (
                    attestation_artifact_digest,
                    attestation_identity,
                    parse_accepted_attestation,
                )

                attestation = parse_accepted_attestation(content, path=path, codec=artifact_codec)
                identity = attestation_identity(attestation).qualified
                if identity in identities:
                    raise ProjectionFormatError("duplicate accepted attestation identity")
                identities[identity] = path
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        attestation.tag,
                        path,
                        attestation_artifact_digest(attestation).tagged,
                        None,
                        1,
                    )
                )
                continue
            if kind == "compiler-upgrade":
                from cruxible_client.contracts.compiler_upgrade import parse_compiler_upgrade

                parse_compiler_upgrade(content)
                continue
            if kind == "principal":
                principal = PrincipalRecord.model_validate(payload)
                if (
                    artifact_bytes_for_path(render_principal(principal), path, codec=artifact_codec)
                    != content
                ):
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
                    document = parse_document(content, path=path, codec=artifact_codec)
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
                except CasError as exc:
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
                        role=pin.role,
                    )
                    for pin in document.pins
                )
                subject = SemanticAddress.whole_artifact(path)
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.document.subject",
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
                            schema_id="cruxible.document.metadata",
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
                            schema_id="cruxible.document.references",
                            schema_version=1,
                            subject_identity=document.identity,
                            fact_key="declared",
                            value={
                                "links": [item.model_dump(mode="json") for item in document.links],
                                "pins": [item.model_dump(mode="json") for item in document.pins],
                            },
                        ),
                        ProjectionFact(
                            schema_id="cruxible.document.source_mapping",
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
                    "cruxible.document.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "subject":
                try:
                    subject_shell = parse_subject(content, path=path, codec=artifact_codec)
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
                        role=pin.role,
                    )
                    for pin in subject_shell.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.subject.identity",
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
                            schema_id="cruxible.subject.lifecycle",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="accepted_shell",
                            value={
                                "lifecycle": subject_shell.lifecycle.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="cruxible.subject.references",
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
                    "cruxible.subject.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "claim-type":
                try:
                    claim_type = parse_claim_type(content, path=path, codec=artifact_codec)
                except ClaimTypeFormatError as exc:
                    raise ProjectionFormatError(
                        f"registered ClaimType failed strict validation: {path}"
                    ) from exc
                if (
                    claim_type.artifact_format == "playbill-claim-type-v6"
                    and artifact_kinds not in _REVISION_31_AND_LATER
                ):
                    raise ProjectionFormatError(
                        "identity evidence rules (ClaimType v6) require compiler revision 31"
                    )
                if (
                    claim_type.artifact_format == "playbill-claim-type-v7"
                    and artifact_kinds not in _REVISION_31_AND_LATER
                ):
                    raise ProjectionFormatError("ClaimType v7 requires compiler revision 31")
                if (
                    claim_type.artifact_format == "playbill-claim-type-v5"
                    and artifact_kinds
                    not in (
                        CLAIM_EVIDENCE_ARTIFACT_KINDS,
                        SOURCE_CHECKED_ARTIFACT_KINDS,
                        TRIGGER_CAPTURE_ARTIFACT_KINDS,
                        AUTHORITY_VERBS_ARTIFACT_KINDS,
                        GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                    )
                ):
                    raise ProjectionFormatError(
                        "producer-independent ClaimTypes require compiler revision 28"
                    )
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
                        role=pin.role,
                    )
                    for pin in claim_type.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.claim_type.identity",
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
                            schema_id="cruxible.claim_type.policies",
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
                            schema_id="cruxible.claim_type.references",
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
                    "cruxible.claim_type.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "provider":
                from cruxible_client.contracts.providers import (
                    Provider,
                    ProviderV2,
                    parse_provider,
                    provider_digest,
                    provider_runtime_artifact_digest,
                )

                provider = parse_provider(content, path=path, codec=artifact_codec)
                if isinstance(provider, Provider) and artifact_kinds not in (
                    PROVIDER_PACKAGE_ARTIFACT_KINDS,
                    RESOURCE_BUDGET_ARTIFACT_KINDS,
                    SDK_SOURCE_ARTIFACT_KINDS,
                    CLAIM_EVIDENCE_ARTIFACT_KINDS,
                    SOURCE_CHECKED_ARTIFACT_KINDS,
                    TRIGGER_CAPTURE_ARTIFACT_KINDS,
                    AUTHORITY_VERBS_ARTIFACT_KINDS,
                    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                ):
                    raise ProjectionFormatError(
                        "Provider v3 requires the provider-package compiler"
                    )
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
                        role=pin.role,
                    )
                    for pin in provider.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.provider.identity",
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
                            schema_id="cruxible.provider.keys",
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
                            schema_id="cruxible.provider.provenance",
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
                if isinstance(provider, ProviderV2):
                    semantic_facts.extend(
                        (
                            ProjectionFact(
                                schema_id="cruxible.provider.runtime",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="runtime_artifact",
                                value={
                                    "external_artifact_digest": {
                                        "$digest": provider_runtime_artifact_digest(
                                            provider.runtime_artifact
                                        )
                                    },
                                    "runtime_artifact": provider.runtime_artifact.model_dump(
                                        mode="json"
                                    ),
                                },
                            ),
                            ProjectionFact(
                                schema_id="cruxible.provider.implementations",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="normalized",
                                value={
                                    "implementations": [
                                        item.model_dump(mode="json")
                                        for item in provider.implementations
                                    ]
                                },
                            ),
                        )
                    )
                continue
            if kind == "provider-interface":
                from cruxible_client.contracts.provider_interfaces import (
                    ProviderInterfaceRegistration,
                    parse_provider_interface,
                    provider_interface_digest,
                )

                registration = parse_provider_interface(
                    content,
                    path=path,
                    codec=artifact_codec,
                )
                if isinstance(
                    registration, ProviderInterfaceRegistration
                ) and artifact_kinds not in (
                    PROVIDER_PACKAGE_ARTIFACT_KINDS,
                    RESOURCE_BUDGET_ARTIFACT_KINDS,
                    SDK_SOURCE_ARTIFACT_KINDS,
                    CLAIM_EVIDENCE_ARTIFACT_KINDS,
                    SOURCE_CHECKED_ARTIFACT_KINDS,
                    TRIGGER_CAPTURE_ARTIFACT_KINDS,
                    AUTHORITY_VERBS_ARTIFACT_KINDS,
                    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                ):
                    raise ProjectionFormatError(
                        "ProviderInterface v2 requires the provider-package compiler"
                    )
                identity = registration.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = provider_interface_digest(registration).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="provider-interface",
                        format_tag=registration.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=registration.lifecycle.predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if registration.lifecycle.state == "retired":
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=pin.artifact_digest,
                        role=pin.role,
                    )
                    for pin in registration.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.provider_interface.registration",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="registration",
                            value={
                                "artifact_digest": {"$digest": artifact_digest},
                                "effect_class": registration.effect_class,
                                "interface_digest": {"$digest": registration.interface_digest},
                                "lifecycle": registration.lifecycle.model_dump(mode="json"),
                            },
                        ),
                        ProjectionFact(
                            schema_id="cruxible.provider_interface.vocabulary",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="vocabulary",
                            value={
                                "vocabulary": registration.vocabulary.model_dump(mode="json"),
                                "vocabulary_digest": {"$digest": registration.vocabulary_digest},
                            },
                        ),
                        ProjectionFact(
                            schema_id="cruxible.provider_interface.classifier",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="classifier",
                            value={
                                "classifier_digest": {"$digest": registration.classifier_digest},
                                "classifier_identity": registration.classifier_identity,
                                "classifier_version": registration.classifier_version,
                                "conformance_fixture_set_digest": {
                                    "$digest": registration.conformance_fixture_set_digest
                                },
                                "conformance_proofs": [
                                    proof.model_dump(mode="json")
                                    for proof in registration.conformance_proofs
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

                acquisition_policy = parse_acquisition_policy(
                    content, path=path, codec=artifact_codec
                )
                identity = acquisition_policy.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = acquisition_policy_digest(acquisition_policy).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="source-acquisition-policy",
                        format_tag=acquisition_policy.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=acquisition_policy.lifecycle.predecessor_digest,
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
                        role=pin.role,
                    )
                    for pin in acquisition_policy.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="cruxible.source_acquisition_policy.policy",
                        schema_version=1,
                        subject_identity=identity,
                        fact_key="complete_policy",
                        value=acquisition_policy.model_dump(mode="json"),
                    )
                )
                continue
            if kind == "procedure-mandate":
                from cruxible_client.contracts.procedure_mandates import (
                    ProcedureMandate,
                    parse_procedure_mandate_any,
                    procedure_mandate_digest,
                )

                procedure_mandate = parse_procedure_mandate_any(
                    content, path=path, codec=artifact_codec
                )
                if (
                    isinstance(procedure_mandate, ProcedureMandate)
                    and artifact_kinds not in _REVISION_31_AND_LATER
                ):
                    raise ProjectionFormatError("ProcedureMandate v2 requires compiler revision 31")
                resources = (
                    procedure_mandate.resource_ceiling
                    if isinstance(procedure_mandate, ProcedureMandate)
                    else procedure_mandate.authority_ceiling
                )
                if artifact_kinds not in (
                    RESOURCE_BUDGET_ARTIFACT_KINDS,
                    SDK_SOURCE_ARTIFACT_KINDS,
                    CLAIM_EVIDENCE_ARTIFACT_KINDS,
                    SOURCE_CHECKED_ARTIFACT_KINDS,
                    TRIGGER_CAPTURE_ARTIFACT_KINDS,
                    AUTHORITY_VERBS_ARTIFACT_KINDS,
                    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                ) and _requires_resource_budgets(resources.model_dump(mode="json")):
                    raise ProjectionFormatError(
                        "resource mandate budgets require compiler revision 26"
                    )
                identity = procedure_mandate.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                input_digest = file_digest(content).tagged
                artifact_digest = procedure_mandate_digest(procedure_mandate).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="procedure-mandate",
                        format_tag=procedure_mandate.artifact_format,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=procedure_mandate.lifecycle.predecessor_digest,
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
                        role=pin.role,
                    )
                    for pin in procedure_mandate.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="cruxible.procedure_mandate.authority",
                        schema_version=1,
                        subject_identity=identity,
                        fact_key="finite_grant",
                        value=procedure_mandate.model_dump(mode="json"),
                    )
                )
                continue
            if kind == "procedure":
                from cruxible_client.contracts.procedures.artifacts import (
                    parse_procedure,
                    procedure_artifact_digest,
                    procedure_runnability,
                )
                from cruxible_client.contracts.procedures.graph import (
                    analyze_procedure,
                    compute_procedure_node_digests,
                )
                from cruxible_client.contracts.procedures.source_compiler import (
                    verify_source_graph,
                )

                procedure = parse_procedure(content, path=path, codec=artifact_codec)
                if artifact_kinds not in (
                    SDK_SOURCE_ARTIFACT_KINDS,
                    CLAIM_EVIDENCE_ARTIFACT_KINDS,
                    SOURCE_CHECKED_ARTIFACT_KINDS,
                    TRIGGER_CAPTURE_ARTIFACT_KINDS,
                    AUTHORITY_VERBS_ARTIFACT_KINDS,
                    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                ):
                    raise ProjectionFormatError("graph-v6 Procedures require compiler revision 27")
                if (
                    any(
                        getattr(node, "kind", None) == "settle_change_set"
                        for node in procedure.definition.nodes
                    )
                    and artifact_kinds not in _REVISION_31_AND_LATER
                ):
                    raise ProjectionFormatError("settle_change_set requires compiler revision 31")
                if (
                    procedure.definition.source is not None
                    and procedure.definition.source.rules == "cruxible.procedure-source.v2"
                    and artifact_kinds
                    not in (
                        SOURCE_CHECKED_ARTIFACT_KINDS,
                        TRIGGER_CAPTURE_ARTIFACT_KINDS,
                        AUTHORITY_VERBS_ARTIFACT_KINDS,
                        GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                    )
                ):
                    raise ProjectionFormatError("source-v2 requires compiler revision 29")
                verify_source_graph(procedure)
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
                        role=pin.role,
                    )
                    for pin in procedure.pins
                )
                graph = analyze_procedure(procedure.definition)
                node_digests = compute_procedure_node_digests(procedure.definition)
                graph_fact_key = "graph_v6"
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
                            schema_id="cruxible.procedure.definition",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key="definition",
                            value={
                                "address": SemanticAddress.procedure_unit(path).model_dump(
                                    mode="json"
                                ),
                                "artifact_digest": {"$digest": artifact_digest},
                                "definition_digest": {"$digest": procedure.definition_digest},
                                "runnable": procedure_runnability(procedure.definition)[0],
                                "identity": procedure.identity.model_dump(mode="json"),
                                "input_digest": {"$digest": input_digest},
                                "measurements": [
                                    measurement.model_dump(mode="json")
                                    for measurement in procedure.definition.measurements
                                ],
                            },
                        ),
                        ProjectionFact(
                            schema_id="cruxible.procedure.graph",
                            schema_version=1,
                            subject_identity=identity,
                            fact_key=graph_fact_key,
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
                                schema_id="cruxible.procedure.source_mapping",
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
                    "cruxible.procedure.resolution_activation",
                    1,
                    classification="semantic",
                ):
                    from cruxible_client.contracts.procedures.artifacts import (
                        AcceptedProcedure,
                    )
                    from cruxible_core.procedures.resolution import (
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
                            AcceptedProcedure(
                                path=path,
                                procedure=procedure,
                                artifact_digest=artifact_digest,
                            ),
                            accepted_coordinate=accepting_coordinate,
                            activated_at=activated_at,
                        )
                        semantic_facts.extend(
                            ProjectionFact(
                                schema_id="cruxible.procedure.resolution_activation",
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
                    "cruxible.procedure.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "line":
                from cruxible_client.contracts.procedures.line_specs import (
                    line_spec_digest,
                    parse_line_spec,
                )

                line = parse_line_spec(content, path=path, codec=artifact_codec)
                if artifact_kinds is not GOVERNED_TRIGGERS_ARTIFACT_KINDS:
                    raise ProjectionFormatError("Line v6 requires compiler revision 32")
                if isinstance(line.budgets, dict):
                    result_budget = line.budgets.get("max_result_bytes")
                    if "max_result_bytes" in line.budgets and (
                        not isinstance(result_budget, int)
                        or isinstance(result_budget, bool)
                        or result_budget < 1
                    ):
                        raise ProjectionFormatError("Line result budget must be a positive integer")
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
                        role=pin.role,
                    )
                    for pin in line.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.line.spec",
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
                            schema_id="cruxible.line.source_mapping",
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
                    "cruxible.line.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "query-definition":
                from cruxible_client.contracts.query.definitions import (
                    parse_query_definition,
                    query_definition_digest,
                )

                query = parse_query_definition(content, path=path, codec=artifact_codec)
                if (
                    query.artifact_format == "playbill-query-definition-v2"
                    and artifact_kinds
                    not in (
                        ONTOLOGY_ARTIFACT_KINDS,
                        UPGRADE_ARTIFACT_KINDS,
                        PROVIDER_CONTRACT_ARTIFACT_KINDS,
                        PROVIDER_PACKAGE_ARTIFACT_KINDS,
                        RESOURCE_BUDGET_ARTIFACT_KINDS,
                        SDK_SOURCE_ARTIFACT_KINDS,
                        CLAIM_EVIDENCE_ARTIFACT_KINDS,
                        SOURCE_CHECKED_ARTIFACT_KINDS,
                        TRIGGER_CAPTURE_ARTIFACT_KINDS,
                        AUTHORITY_VERBS_ARTIFACT_KINDS,
                        GOVERNED_TRIGGERS_ARTIFACT_KINDS,
                    )
                ):
                    raise ProjectionFormatError(
                        "artifact queries require the ontology-query compiler"
                    )
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
                        role=pin.role,
                    )
                    for pin in query.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.query_definition.definition",
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
                            schema_id="cruxible.query_definition.policy",
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
                            schema_id="cruxible.query_definition.references",
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
                    "cruxible.query_definition.attestation_coverage",
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
                            accepted_coordinates=accepted_coordinates,
                        )
                    )
                continue
            if kind == "exhaust-promotion":
                from cruxible_core.exhaust.promotions import (
                    AcceptedExhaustPromotionV1,
                    exhaust_promotion_digest,
                    parse_exhaust_promotion,
                    procedure_track_record_facts,
                )

                promotion = parse_exhaust_promotion(content, path=path, codec=artifact_codec)
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
                        role=pin.role,
                    )
                    for pin in promotion.pins
                )
                semantic_facts.append(
                    ProjectionFact(
                        schema_id="cruxible.exhaust_promotion.basis",
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
                    "cruxible.procedure.track_record",
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
                    except (CasError, UnicodeDecodeError, ValueError) as exc:
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
                        "cruxible.line.track_record",
                        1,
                        classification="semantic",
                    ):
                        from cruxible_core.exhaust.line_track_records import (
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
                    capture_contract = parse_capture_contract(
                        content, path=path, codec=artifact_codec
                    )
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
                        role=pin.role,
                    )
                    for pin in capture_contract.pins
                )
                semantic_facts.extend(
                    (
                        ProjectionFact(
                            schema_id="cruxible.capture_contract.contract",
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
                            schema_id="cruxible.capture_contract.references",
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
                    claim = parse_claim(content, path=path, codec=artifact_codec)
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
                format_tag: str = claim.artifact_format
                predecessor_digest = claim.lifecycle.predecessor_digest
                retired = claim.lifecycle.state == "retired"
                claim_pins = tuple(
                    (pin.target.qualified, pin.artifact_digest, pin.role) for pin in claim.pins
                )
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity=identity,
                        kind="claim",
                        format_tag=format_tag,
                        path=path,
                        artifact_digest=artifact_digest,
                        predecessor_digest=predecessor_digest,
                        revision=projected_revision(
                            accepted_change_sets,
                            path=path,
                            input_digest=input_digest,
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                if retired:
                    retired_identities.append(identity)
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=target,
                        target_digest=digest,
                        role=role,
                    )
                    for target, digest, role in claim_pins
                )
                static_facts = _claim_static_facts(
                    claim,
                    path=path,
                    input_digest=input_digest,
                    artifact_digest=artifact_digest,
                    statement_digest=statement_digest,
                )
                semantic_facts.extend(static_facts)
                if coordinate is not None and registry.supports(
                    "cruxible.claim.current_verdict",
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
                            predecessor_digest=predecessor_digest,
                            records=accepted_change_sets,
                            coordinate=coordinate,
                            accepted_coordinates=accepted_coordinates,
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
                        if fact.schema_id != "cruxible.claim.attestation_coverage":
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
                                schema_id="cruxible.claim.current_verdict",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="accepted_evaluation",
                                value=claim_verdict_v1_compat(
                                    law_evidence.verdict_result
                                ).model_dump(mode="json"),
                            ),
                            ProjectionFact(
                                schema_id="cruxible.claim.evidence_basis",
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
            raise ProjectionFormatError(f"registered artifact kind is unsupported: {kind}")
        except ValidationError as exc:
            raise ProjectionFormatError(
                f"registered artifact failed strict validation: {path}"
            ) from exc

    pin_dependencies: dict[tuple[str, str, str], PinRow] = {}
    for pin in pins:
        # A Claim's capture-contract pins are provenance: evidence captured under
        # two versions of one contract pins both versions, so they key by version.
        version = (
            pin.target_digest
            if pin.role == "capture-contract" and pin.source_identity.startswith("Claim:")
            else ""
        )
        key = (pin.source_identity, pin.target_identity, version)
        previous_pin = pin_dependencies.get(key)
        if previous_pin is not None and previous_pin.target_digest != pin.target_digest:
            raise ProjectionFormatError(
                "one artifact pins the same dependency identity at conflicting digests"
            )
        pin_dependencies[key] = pin

    validated_semantic = registry.validate(semantic_facts, classification="semantic")

    return ParsedProjectionTree(
        envelopes=tuple(sorted(envelopes, key=lambda item: item.identity.encode("utf-8"))),
        pins=tuple(
            sorted(
                pin_dependencies.values(),
                key=lambda item: (
                    item.source_identity.encode("utf-8"),
                    item.target_identity.encode("utf-8"),
                    item.target_digest.encode("ascii"),
                ),
            )
        ),
        retired_identities=tuple(sorted(retired_identities, key=lambda item: item.encode("utf-8"))),
        semantic_facts=validated_semantic,
    )


__all__ = [
    "projected_revision",
    "ArtifactEnvelopeRow",
    "ParsedProjectionTree",
    "PLAYBILL_ARTIFACT_KINDS",
    "P2_B1_ARTIFACT_KINDS",
    "P2_C_ARTIFACT_KINDS",
    "P2_B0_ARTIFACT_KINDS",
    "PinRow",
    "parse_projection_tree",
    "registered_path_kind",
]
