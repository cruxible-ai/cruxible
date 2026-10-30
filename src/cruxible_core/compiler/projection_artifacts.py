"""Registered Cruxible artifact formats and normalized projection rows."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping, cast

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
from cruxible_client.contracts.content_memo import ParseMemo
from cruxible_client.contracts.dependencies import dependency_pins
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
    ProjectionFact,
    current_extension_registry,
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
    from cruxible_client.contracts.claims import ClaimArtifact
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.proposals.settlement import ChangeSetRecord

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_NAME = r"[a-z][a-z0-9_.-]{0,255}"
_DOTTED = r"[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*"
#: The current compiler's closed ledger path grammar: every accepted path
#: resolves to exactly one of these kinds or refuses.
ARTIFACT_KINDS = ArtifactKindRegistry(
    tuple(
        ArtifactPathKind(kind, re.compile(pattern))
        for kind, pattern in (
            ("approval-policy", r"^governance/approval-policy\.json$"),
            ("attestation", r"^attestations/[0-9a-f]{2}/[0-9a-f]{64}\.json$"),
            ("capture-contract", rf"^capture-contracts/{_NAME}\.json$"),
            ("changeset", r"^changesets/cs-[0-9]{20}\.json$"),
            ("claim", r"^claims/[0-9a-f]{2}/CLM-[0-9a-f]{32}\.json$"),
            ("claim-type", rf"^claim-types/{_DOTTED}/[a-z][a-z0-9_]{{0,63}}\.json$"),
            ("compiler-upgrade", r"^compiler-upgrade\.json$"),
            ("contract", r"^contracts/[a-z][a-z0-9_.-]{0,127}/v[1-9][0-9]{0,8}\.json$"),
            ("document", rf"^documents/{_NAME}\.json$"),
            ("exhaust-promotion", rf"^exhaust-promotions/{_NAME}\.json$"),
            ("line", rf"^lines/{_NAME}\.json$"),
            ("principal", r"^principals/[a-z][a-z0-9_.-]{0,127}\.json$"),
            ("procedure", rf"^procedures/{_NAME}\.json$"),
            ("procedure-mandate", rf"^procedure-mandates/{_NAME}\.json$"),
            ("procedure-runtime-policy", r"^governance/procedure-runtime-policy\.json$"),
            # A build is its own artifact beside the other builds of its name; a
            # single-path Provider (the built-in) is one build of its name.
            ("provider", rf"^providers/{_NAME}(?:/[0-9a-f]{{64}})?\.json$"),
            ("query-definition", rf"^query-definitions/{_NAME}\.json$"),
            ("resolution-contract", rf"^resolution-contracts/{_NAME}\.json$"),
            ("subject", rf"^subjects/{_DOTTED}/{_NAME}\.json$"),
            ("subject-kind", rf"^subject-kinds/{_DOTTED}\.json$"),
            ("trigger", rf"^triggers/{_NAME}\.json$"),
        )
    )
)


#: The current compiler's closed fact-schema registry.
PROJECTION_REGISTRY = current_extension_registry()


RegisteredPathKind = Literal[
    "compiler-upgrade",
    "resolution-contract",
    "attestation",
    "approval-policy",
    "contract",
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
    "query-definition",
    "subject",
    "subject-kind",
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
    target_digest: str | None
    # The pin's role, which every dependency edge carries.
    role: str


@dataclass(frozen=True)
class ParsedProjectionTree:
    envelopes: tuple[ArtifactEnvelopeRow, ...]
    pins: tuple[PinRow, ...]
    retired_identities: tuple[str, ...]
    semantic_facts: tuple[ProjectionFact, ...]


def registered_path_kind(path: str) -> RegisteredPathKind:
    return cast(RegisteredPathKind, ARTIFACT_KINDS.resolve_path(path))


class _IndexedRecords(tuple):  # type: ignore[type-arg]
    """One parse's change-set records with a path index built the first time it is asked.

    ``path -> ((record position, member), ...)`` in record then member order: one
    parse asks every artifact for its history, and scanning every record's
    members per artifact made a cold parse O(artifacts x members). The index
    lives on this parse's own tuple and goes with it; nothing is shared.
    """

    _by_path: Mapping[str, tuple[tuple[int, Any], ...]] | None = None


def _members_by_path(
    records: tuple[tuple[str, ChangeSetRecord], ...],
) -> Mapping[str, tuple[tuple[int, Any], ...]]:
    if isinstance(records, _IndexedRecords) and records._by_path is not None:
        return records._by_path
    found: dict[str, list[tuple[int, Any]]] = {}
    for position, (_record_path, record) in enumerate(records):
        for member in record.members:
            found.setdefault(member.path, []).append((position, member))
    index = {path: tuple(items) for path, items in found.items()}
    if isinstance(records, _IndexedRecords):
        records._by_path = index
    return index


def _accepting_positions(
    records: tuple[tuple[str, ChangeSetRecord], ...],
    *,
    path: str,
    artifact_digest: str,
) -> tuple[int, ...]:
    """Positions, newest first, of the records with a member accepting this exact revision."""

    return tuple(
        dict.fromkeys(
            position
            for position, member in reversed(_members_by_path(records).get(path, ()))
            if getattr(member, "candidate_artifact_digest", None) == artifact_digest
        )
    )


def projected_revision(
    records: tuple[tuple[str, ChangeSetRecord], ...],
    *,
    path: str,
    artifact_digest: str,
) -> int:
    history = tuple(member for _position, member in _members_by_path(records).get(path, ()))
    if any(member.candidate_artifact_digest == artifact_digest for member in history):
        return len(history)
    return len(history) + 1


def _accepted_artifact_timestamp(
    records: tuple[tuple[str, ChangeSetRecord], ...],
    *,
    path: str,
    artifact_digest: str,
) -> datetime | None:
    """Return the signed C_s time of the change set that accepted this exact revision."""

    for position in _accepting_positions(records, path=path, artifact_digest=artifact_digest):
        record = records[position][1]
        return datetime.strptime(record.candidate.timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    return None


def _accepted_artifact_coordinate(
    records: tuple[tuple[str, ChangeSetRecord], ...],
    *,
    path: str,
    artifact_digest: str,
    coordinates_by_sequence: Mapping[int, AcceptedCoordinate],
) -> AcceptedCoordinate | None:
    """Resolve the immutable generation that accepted one exact artifact revision."""

    for position in _accepting_positions(records, path=path, artifact_digest=artifact_digest):
        return coordinates_by_sequence.get(records[position][1].sequence)
    return None


def _current_member_law_result(
    records: tuple[tuple[str, ChangeSetRecord], ...],
    *,
    path: str,
    artifact_digest: str,
) -> dict[str, object] | None:
    """Return the exact accepted law result for one current artifact revision."""

    for position in _accepting_positions(records, path=path, artifact_digest=artifact_digest):
        record = records[position][1]
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


_PROCEDURE_WIRE_NODES: ParseMemo[dict[str, dict[str, Any]]] = ParseMemo(
    capacity=32, max_bytes=4 * 1024 * 1024
)


def _procedure_wire_nodes(content: bytes) -> dict[str, dict[str, Any]]:
    return _PROCEDURE_WIRE_NODES.parse(
        content,
        "procedure-wire-nodes",
        None,
        lambda: {node["id"]: node for node in json.loads(content)["definition"]["nodes"]},
    )


def _procedure_node_span(
    content: bytes,
    node: BaseModel,
    *,
    content_digest: str,
    wire_node: dict[str, Any] | None = None,
) -> ContentSpan:
    payload = node.model_dump(mode="json", by_alias=True)
    # v3 has single ids and compact predicates. The source mapping covers the
    # exact retained node, including its omitted fallthrough edges.
    if wire_node is not None:
        payload = wire_node
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
    claim: ClaimArtifact,
    *,
    path: str,
    input_digest: str,
    artifact_digest: str,
    statement_digest: str,
) -> tuple[ProjectionFact, ...]:
    """Compile only byte-dependent Claim facts; acceptance proofs stay per-build."""
    from cruxible_client.contracts.claims import claim_statement_address

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
                        if claim.retirement is not None
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


def parse_projection_tree(
    blobs: Mapping[str, bytes],
    *,
    bodies: BodyProjectionProtocol | None = None,
    coordinate: ProjectionCoordinateContext | None = None,
    accepted_coordinates_by_sequence: Mapping[int, AcceptedCoordinate] | None = None,
    verified_change_sets: tuple[tuple[str, ChangeSetRecord], ...] | None = None,
    selected_member_history: tuple[tuple[str, ChangeSetRecord], ...] | None = None,
    carry_members: bool = False,
    by_name: bool = False,
) -> ParsedProjectionTree:
    """Parse all registered blobs and produce one sorted, typed row stream."""

    by_name = by_name or coordinate is not None
    registry = PROJECTION_REGISTRY
    from cruxible_client.contracts.captures import (
        CaptureFormatError,
        capture_contract_digest,
        parse_capture_contract,
    )
    from cruxible_client.contracts.claim_verdicts import reported_claim_verdict
    from cruxible_client.contracts.claims import (
        ClaimFormatError,
        parse_claim_law_evidence,
    )
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.proposals.settlement import parse_change_set_record

    envelopes: list[ArtifactEnvelopeRow] = []
    pins: list[PinRow] = []
    retired_identities: list[str] = []
    semantic_facts: list[ProjectionFact] = []
    identities: dict[str, str] = {}
    change_sets: list[tuple[str, ChangeSetRecord]] = []

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
    if selected_member_history is None and [
        record.sequence for _path, record in change_sets
    ] != list(range(1, len(change_sets) + 1)):
        raise ProjectionFormatError("change-set history must be contiguous from sequence one")
    accepted_change_sets = _IndexedRecords(change_sets)
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
        kind = registered_path_kind(path)
        payload = {} if kind == "claim" else _load_object(content, path=path)
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
                            artifact_digest=digest,
                        ),
                    )
                )
                continue
            if kind == "procedure-runtime-policy":
                runtime_policy = parse_procedure_runtime_policy(content, path=path)
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
                            artifact_digest=digest,
                        ),
                    )
                )
                continue
            if kind == "contract":
                from cruxible_client.contracts.shapes import (
                    CONTRACT_PIN_ROLE,
                    contract_digest,
                    parse_contract,
                )

                shape = parse_contract(content, path=path)
                identity = shape.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                digest = contract_digest(shape).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        shape.artifact_format,
                        path,
                        digest,
                        shape.predecessor_digest,
                        projected_revision(
                            accepted_change_sets,
                            path=path,
                            artifact_digest=digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=ref.identity.qualified,
                        target_digest=ref.digest,
                        role=CONTRACT_PIN_ROLE,
                    )
                    for ref in shape.refs()
                )
                continue
            if kind == "subject-kind":
                from cruxible_client.contracts.subject_kinds import (
                    parse_subject_kind,
                    subject_kind_digest,
                )

                subject_kind = parse_subject_kind(content, path=path)
                identity = subject_kind.identity.qualified
                if identity in identities:
                    raise ProjectionFormatError(f"duplicate semantic identity {identity!r}")
                identities[identity] = path
                digest = subject_kind_digest(subject_kind).tagged
                envelopes.append(
                    ArtifactEnvelopeRow(
                        identity,
                        kind,
                        subject_kind.artifact_format,
                        path,
                        digest,
                        subject_kind.lifecycle.predecessor_digest,
                        projected_revision(
                            accepted_change_sets,
                            path=path,
                            artifact_digest=digest,
                        ),
                    )
                )
                if subject_kind.lifecycle.state == "retired":
                    retired_identities.append(identity)
                continue
            if kind == "trigger":
                from cruxible_client.contracts.triggers import parse_trigger, trigger_digest

                trigger = parse_trigger(content, path=path)
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(trigger, by_name=by_name)
                )
                continue
            if kind == "resolution-contract":
                from cruxible_client.contracts.resolution_contracts import (
                    parse_resolution_contract,
                    resolution_contract_digest,
                )

                contract = parse_resolution_contract(content, path=path)
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

                attestation = parse_accepted_attestation(content, path=path)
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
                if coordinate is not None:
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
                            carry_values=carry_members,
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
                        target_digest=getattr(pin, "artifact_digest", None),
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
                if coordinate is not None:
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
                            carry_values=carry_members,
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(claim_type, by_name=by_name)
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
                if coordinate is not None:
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
                            carry_values=carry_members,
                        )
                    )
                continue
            if kind == "provider":
                from cruxible_client.contracts.providers import (
                    ProviderV2,
                    parse_provider,
                    provider_digest,
                    provider_runtime_artifact_digest,
                )

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
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(provider, by_name=by_name)
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
            if kind == "procedure-mandate":
                from cruxible_client.contracts.procedure_mandates import (
                    parse_procedure_mandate_any,
                    procedure_mandate_digest,
                )

                procedure_mandate = parse_procedure_mandate_any(content, path=path)
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
                            artifact_digest=artifact_digest,
                        ),
                    )
                )
                pins.extend(
                    PinRow(
                        source_identity=identity,
                        target_identity=pin.target.qualified,
                        target_digest=getattr(pin, "artifact_digest", None),
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

                procedure = parse_procedure(content, path=path)
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(procedure, by_name=by_name)
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
                wire_nodes: dict[str, dict[str, Any]] = _procedure_wire_nodes(content)
                for node in procedure.definition.nodes:
                    span = _procedure_node_span(
                        content,
                        node,
                        content_digest=input_digest,
                        wire_node=wire_nodes.get(node.node_id),
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
                                "slots": {
                                    name: slot.model_dump(mode="json", by_alias=True)
                                    for name, slot in procedure.definition.slots.items()
                                },
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
                if coordinate is not None:
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
                if coordinate is not None:
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
                            carry_values=carry_members,
                        )
                    )
                continue
            if kind == "line":
                from cruxible_client.contracts.procedures.line_specs import (
                    line_spec_digest,
                    parse_line_spec,
                )

                line = parse_line_spec(content, path=path)
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(line, by_name=by_name)
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
                if coordinate is not None:
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
                            carry_values=carry_members,
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(query, by_name=by_name)
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
                                "pins": [
                                    pin.model_dump(mode="json")
                                    for pin in dependency_pins(query, by_name=by_name)
                                ],
                            },
                        ),
                    )
                )
                if coordinate is not None:
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
                            carry_values=carry_members,
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
                        target_digest=getattr(pin, "artifact_digest", None),
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
                if coordinate is not None:
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
                        target_digest=getattr(pin, "artifact_digest", None),
                        role=pin.role,
                    )
                    for pin in dependency_pins(capture_contract, by_name=by_name)
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
                                    pin.model_dump(mode="json")
                                    for pin in dependency_pins(capture_contract, by_name=by_name)
                                ]
                            },
                        ),
                    )
                )
                continue
            if kind == "claim":
                from cruxible_core.compiler.sealed_members import sealed_claim_member

                try:
                    member = sealed_claim_member(content, path=path)
                    claim = member.claim
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
                input_digest = member.input_digest
                artifact_digest = member.artifact_digest
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
                # Compiler-owned write passes carry immutable rows. Standalone
                # reconstruction and read adapters retain their detached value
                # contract, without parsing or normalizing the facts again.
                semantic_facts.extend(member.facts if carry_members else deepcopy(member.facts))
                if coordinate is not None:
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
                            carry_values=carry_members,
                            claim_attestations=law_evidence.verified_attestations,
                        )
                    )
                    semantic_facts.extend(explanation_facts)
                    semantic_facts.extend(
                        (
                            ProjectionFact(
                                schema_id="cruxible.claim.current_verdict",
                                schema_version=1,
                                subject_identity=identity,
                                fact_key="accepted_evaluation",
                                # In the query vocabulary, which reads
                                # ``stale_evidence`` as ``stale``.
                                value={
                                    **law_evidence.verdict_result.model_dump(mode="json"),
                                    "verdict": reported_claim_verdict(law_evidence.verdict_result),
                                },
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

    pin_dependencies: dict[tuple[str, str, str | None], PinRow] = {}
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
                    (item.target_digest or "").encode("ascii"),
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
    "ARTIFACT_KINDS",
    "PROJECTION_REGISTRY",
    "PinRow",
    "parse_projection_tree",
    "registered_path_kind",
]
