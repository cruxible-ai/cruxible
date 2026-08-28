"""Deterministic repair queue derived from one accepted Playbill coordinate."""

from __future__ import annotations

import shlex
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts import PlaybillNextReason
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    CanonicalDurationV1,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResultV2,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimCitationV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_BLOCKS_PER_SOURCE,
    MAX_PROJECTION_CARDS_PER_SOURCE,
    MAX_PROJECTION_SOURCE_BYTES,
    PlaybillPresentationPolicyNoteV1,
    PlaybillPresentationPolicyV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
    ProjectionQueryBackingV1,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_slots import classify_claim_slot
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCommitmentScanProofV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.playbill.coverage.indexes import (
    WorkingOccurrenceV1,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import claim_row_visibility
from cruxible_core.playbill.query.engine import evaluate_claim_query
from cruxible_core.playbill.query.impact import (
    SOURCE_CONTRADICTED,
    SOURCE_SUPERSEDED,
    DependencyImpactRequestV1,
    build_dependency_impact,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    _claim_law_evidence,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_evidence import (
    current_verified_claim_attestations,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts
from cruxible_core.service.playbill_search import claim_resolution_statuses

NEXT_ITEM_ID_DOMAIN = "playbill-next-item-v1"
NEXT_RESULT_DIGEST_DOMAIN = "playbill-next-result-v1"
DEFAULT_EXPIRING_WITHIN_MICROSECONDS = 604_800_000_000
MAX_DEPENDENCY_LINEAGE_GENERATIONS = 256

NextDomain = Literal["accepted_state", "workspace_floor", "workspace_sources"]
NextSeverity = Literal["blocking", "repair", "warning"]
CitationLineageNote = Literal[
    "predecessor_lineage_limit_exceeded",
    "predecessor_unresolved",
]
NextReason: TypeAlias = PlaybillNextReason
NextRepairOperation = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim.retire",
    "playbill.floor.export",
    "playbill.block.repin",
    "playbill.document.propose",
]

_SEVERITY_RANK: dict[NextSeverity, int] = {"blocking": 0, "repair": 1, "warning": 2}
_ALL_DOMAINS: tuple[NextDomain, ...] = (
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
)
_PROJECTION_VISIBILITY_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=("contradicted", "stale", "supported", "uncovered", "unresolved"),
    visible_currency=("current", "not_applicable", "stale"),
    conflict_behavior="surface_conflicts",
)


class _StrictNextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillNextError(PlaybillError):
    code = "playbill.next.refused"

    @property
    def error_code(self) -> str:
        return self.code


class PlaybillNextAccessProfileInvalid(PlaybillNextError):
    code = "playbill.next.access_profile_invalid"


class PlaybillNextWorkspaceObservationInvalid(PlaybillNextError):
    code = "playbill.next.workspace_observation_invalid"


class PlaybillNextCoordinateNotAccepted(PlaybillNextError):
    code = "playbill.next.coordinate_not_accepted"


class PlaybillNextAcceptedStateInvalid(PlaybillNextError):
    code = "playbill.next.accepted_state_invalid"


class PlaybillNextDriftObservationV1(_StrictNextModel):
    citation_id: str
    expected_commitment_digest: str
    observed_commitment_digest: str

    @field_validator("citation_id", "expected_commitment_digest", "observed_commitment_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillNextSourceObservationV3(_StrictNextModel):
    tag: Literal["playbill-next-source-observation-v3"]
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    document_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    observed_source_digest: str
    byte_length: int = Field(ge=0, le=MAX_PROJECTION_SOURCE_BYTES)
    marker_summaries: tuple[ProjectionMarkerSummaryV1, ...] = Field(
        max_length=MAX_PROJECTION_BLOCKS_PER_SOURCE
    )
    occurrences: tuple[WorkingOccurrenceV1, ...] = Field(max_length=MAX_PROJECTION_CARDS_PER_SOURCE)
    scanned_commitment_digests: tuple[str, ...]
    scan_complete: bool
    scan_notes: tuple[str, ...]
    marker_notes: tuple[str, ...]

    @field_validator("observed_source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("scanned_commitment_digests")
    @classmethod
    def _commitments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            Sha256Value.from_tagged(digest)
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("next scanned commitment digests must be sorted and unique")
        return value

    @field_validator("scan_notes", "marker_notes")
    @classmethod
    def _notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next observation notes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_shape(self) -> "PlaybillNextSourceObservationV3":
        ids = tuple(marker.stamp.block_id for marker in self.marker_summaries)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next marker summaries must be sorted and unique by block ID")
        previous_end = -1
        for marker in sorted(self.marker_summaries, key=lambda item: item.start_byte):
            if marker.stamp.source_id != self.source_id:
                raise ValueError("next marker summary names a different logical source")
            if marker.start_byte < previous_end or marker.end_byte > self.byte_length:
                raise ValueError("next marker summary windows overlap or escape the source")
            previous_end = marker.end_byte
        keys = tuple(occurrence.sort_key for occurrence in self.occurrences)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("next source occurrences must be sorted and unique")
        for occurrence in self.occurrences:
            if (
                occurrence.source.plane != "external"
                or occurrence.source.identity != self.source_id
            ):
                raise ValueError("next occurrence names a different logical source")
            if occurrence.line_overlay.end_byte > self.byte_length:
                raise ValueError("next occurrence presentation window escapes the source")
        if not self.scan_complete and (self.occurrences or self.scanned_commitment_digests):
            raise ValueError("an incomplete next scan cannot assert occurrences or scanned digests")
        return self


class PlaybillNextSourceObservationV4(_StrictNextModel):
    tag: Literal["playbill-next-source-observation-v4"] = "playbill-next-source-observation-v4"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    document_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    observed_source_digest: str
    byte_length: int = Field(ge=0, le=MAX_PROJECTION_SOURCE_BYTES)
    marker_summaries: tuple[ProjectionMarkerSummaryV1, ...] = Field(
        max_length=MAX_PROJECTION_BLOCKS_PER_SOURCE
    )
    occurrences: tuple[WorkingOccurrenceV1, ...] = Field(max_length=MAX_PROJECTION_CARDS_PER_SOURCE)
    commitment_scan_proofs: tuple[CoverageCommitmentScanProofV1, ...] = Field(
        max_length=MAX_PROJECTION_CARDS_PER_SOURCE
    )
    citation_window_observations: tuple[PlaybillCitationWindowObservationV1, ...] = Field(
        max_length=MAX_PROJECTION_CARDS_PER_SOURCE
    )
    scan_notes: tuple[str, ...]
    marker_notes: tuple[str, ...]

    @field_validator("observed_source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("scan_notes", "marker_notes")
    @classmethod
    def _notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next observation notes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _source_shape(self) -> "PlaybillNextSourceObservationV4":
        expected_source = LogicalSourceIdentityV1(plane="external", identity=self.source_id)
        marker_ids = tuple(marker.stamp.block_id for marker in self.marker_summaries)
        if marker_ids != tuple(sorted(set(marker_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next marker summaries must be sorted and unique by block ID")
        previous_end = -1
        for marker in sorted(self.marker_summaries, key=lambda item: item.start_byte):
            if marker.stamp.source_id != self.source_id:
                raise ValueError("next marker summary names a different logical source")
            if marker.start_byte < previous_end or marker.end_byte > self.byte_length:
                raise ValueError("next marker summary windows overlap or escape the source")
            previous_end = marker.end_byte

        occurrence_keys = tuple(item.sort_key for item in self.occurrences)
        if occurrence_keys != tuple(sorted(set(occurrence_keys))):
            raise ValueError("next source occurrences must be sorted and unique")
        proof_keys = tuple(item.sort_key for item in self.commitment_scan_proofs)
        if proof_keys != tuple(sorted(set(proof_keys))):
            raise ValueError("next source scan proofs must be sorted and unique")
        proof_identities = {
            (item.source.sort_key, item.commitment_digest, item.byte_length)
            for item in self.commitment_scan_proofs
        }
        for proof in self.commitment_scan_proofs:
            if proof.source != expected_source:
                raise ValueError("next source scan proof names a different logical source")
        for occurrence in self.occurrences:
            if occurrence.source != expected_source:
                raise ValueError("next occurrence names a different logical source")
            if occurrence.line_overlay.end_byte > self.byte_length:
                raise ValueError("next occurrence presentation window escapes the source")
            if (
                occurrence.source.sort_key,
                occurrence.observed_commitment_digest,
                occurrence.byte_length,
            ) not in proof_identities:
                raise ValueError("every next occurrence requires its exact local scan proof")

        window_keys = tuple(
            (
                item.source.sort_key,
                item.citation_id.encode("ascii"),
                item.original_start,
                item.original_end,
            )
            for item in self.citation_window_observations
        )
        if window_keys != tuple(sorted(set(window_keys))):
            raise ValueError("next citation windows must be sorted and unique")
        for window in self.citation_window_observations:
            if window.source != expected_source:
                raise ValueError("next citation window names a different logical source")
            if window.addressable and window.original_end > self.byte_length:
                raise ValueError("addressable next citation window escapes the source")
        return self


PlaybillNextSourceObservationAny: TypeAlias = (
    PlaybillNextSourceObservationV3 | PlaybillNextSourceObservationV4
)


class PlaybillNextWorkspaceObservationV1(_StrictNextModel):
    tag: Literal["playbill-next-workspace-observation-v1"] = (
        "playbill-next-workspace-observation-v1"
    )
    floor_status: Literal["not_configured", "missing", "current", "stale", "invalid"] | None = None
    installed_coordinate: AcceptedCoordinate | None = None
    drift_observations: tuple[PlaybillNextDriftObservationV1, ...] | None = None
    source_observations: tuple[PlaybillNextSourceObservationAny, ...] | None = None
    presentation_policy: PlaybillPresentationPolicyV1 | None = None
    presentation_policy_notes: tuple[PlaybillPresentationPolicyNoteV1, ...] = ()

    @field_validator("drift_observations")
    @classmethod
    def _drift(
        cls,
        value: tuple[PlaybillNextDriftObservationV1, ...] | None,
    ) -> tuple[PlaybillNextDriftObservationV1, ...] | None:
        if value is None:
            return None
        ids = tuple(item.citation_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("ascii"))):
            raise ValueError("next drift observations must be sorted and unique by citation_id")
        return value

    @field_validator("source_observations")
    @classmethod
    def _sources(
        cls,
        value: tuple[PlaybillNextSourceObservationAny, ...] | None,
    ) -> tuple[PlaybillNextSourceObservationAny, ...] | None:
        if value is None:
            return None
        ids = tuple(item.source_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next source observations must be sorted and unique by source_id")
        return value

    @model_validator(mode="after")
    def _floor_shape(self) -> "PlaybillNextWorkspaceObservationV1":
        if self.floor_status == "current" and self.installed_coordinate is None:
            raise ValueError("a current floor observation requires its installed coordinate")
        return self


class PlaybillNextRequestV1(_StrictNextModel):
    tag: Literal["playbill-next-request-v1"] = "playbill-next-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    expiring_within: CanonicalDurationV1 = CanonicalDurationV1(
        microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS
    )
    workspace_observation: PlaybillNextWorkspaceObservationV1 | None = None
    # The result_digest of a queue this caller has already seen. A digest this
    # process still remembers yields only the rows that are new since it; one it
    # does not -- a restart, an eviction, a digest from elsewhere -- yields the
    # whole queue, which is always a correct answer to "what is outstanding".
    since_result_digest: str | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def validate_playbill_next_request(
    value: PlaybillNextRequestV1 | Mapping[str, object],
) -> PlaybillNextRequestV1:
    if isinstance(value, PlaybillNextRequestV1):
        return value
    try:
        return PlaybillNextRequestV1.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "access_profile" in roots:
            error: type[PlaybillNextError] = PlaybillNextAccessProfileInvalid
        elif "workspace_observation" in roots:
            error = PlaybillNextWorkspaceObservationInvalid
        else:
            error = PlaybillNextAcceptedStateInvalid
        raise error(f"{error.code}: {exc}") from exc


class PlaybillNextRepairV1(_StrictNextModel):
    operation: NextRepairOperation
    target: str
    required_change: str
    arguments: object = Field(default_factory=dict)
    # The operation is a dotted CLI path and the arguments are its options, so a
    # caller could always have assembled this line -- and every caller had to.
    # Composed from the digested fields beside it, so it is a pure function of
    # them and stays deterministic inside the item_id and result_digest
    # preimages it necessarily joins.
    command: str = ""

    @field_validator("arguments", mode="before")
    @classmethod
    def _arguments(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class PlaybillNextItemV1(_StrictNextModel):
    tag: Literal["playbill-next-item-v1"] = "playbill-next-item-v1"
    item_id: str
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: tuple[str, ...] = ()
    detail: object = Field(default_factory=dict)
    repair: PlaybillNextRepairV1

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("related_identities")
    @classmethod
    def _related(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next related identities must be byte-sorted and unique")
        return value

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _identity(self) -> "PlaybillNextItemV1":
        if self.item_id != playbill_next_item_id(self):
            raise ValueError("next item ID does not reproduce")
        return self


class PlaybillNextResultV1(_StrictNextModel):
    tag: Literal["playbill-next-result-v1"] = "playbill-next-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    observed_domains: tuple[NextDomain, ...]
    unobserved_domains: tuple[NextDomain, ...]
    items: tuple[PlaybillNextItemV1, ...]
    result_digest: str
    # Set only on a delta. `result_digest` then still names the WHOLE queue --
    # it is the cursor the caller echoes back next time -- so it deliberately
    # does not reproduce from the partial `items` carried here.
    delta_since: str | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "PlaybillNextResultV1":
        if set(self.observed_domains).intersection(self.unobserved_domains):
            raise ValueError("next observed and unobserved domains overlap")
        if set((*self.observed_domains, *self.unobserved_domains)) != set(_ALL_DOMAINS):
            raise ValueError("next result must account for every observation domain")
        if self.items != tuple(sorted(self.items, key=_item_sort_key)):
            raise ValueError("next items do not follow the deterministic order")
        if self.delta_since is None and self.result_digest != playbill_next_result_digest(self):
            raise ValueError("next result digest does not reproduce")
        return self


_REPAIR_COMMAND_PATHS: Mapping[str, str] = {
    "playbill.authoring.create": "playbill authoring create",
    "playbill.authoring.bind": "playbill authoring bind",
    "playbill.claim.retire": "playbill claim retire",
    "playbill.floor.export": "playbill floor export",
    "playbill.block.repin": "playbill block repin",
    "playbill.document.propose": "playbill document propose",
}

# Each of these needs a local file. The queue knows the path only if the row
# carried it, so the placeholder is filled from the arguments when they name it
# and dropped -- with the flag that introduces it -- when they do not. A bare
# `REQUEST_FILE` left in the line is not a hint, it is an unrunnable command
# presented as a runnable one, which is the one thing `command` must never be.
_REPAIR_COMMAND_OPERANDS: Mapping[str, tuple[str, ...]] = {
    "playbill.authoring.create": ("PAYLOAD_FILE",),
    "playbill.authoring.bind": ("--payload-file", "PAYLOAD_FILE"),
    "playbill.claim.retire": ("REQUEST_FILE",),
    "playbill.document.propose": ("--envelope", "ENVELOPE_FILE"),
}
_REPAIR_COMMAND_PLACEHOLDERS: Mapping[str, str] = {
    "PAYLOAD_FILE": "payload_file",
    "REQUEST_FILE": "request_file",
    "ENVELOPE_FILE": "envelope_file",
}


def _repair_operands(operation: NextRepairOperation, values: Mapping[str, object]) -> list[str]:
    """Render one operation's file operands, filling or dropping each placeholder."""

    rendered: list[str] = []
    pending_flag: str | None = None
    for operand in _REPAIR_COMMAND_OPERANDS[operation]:
        key = _REPAIR_COMMAND_PLACEHOLDERS.get(operand)
        if key is None:
            pending_flag = operand
            continue
        supplied = values.get(key)
        if not isinstance(supplied, str) or not supplied:
            pending_flag = None
            continue
        if pending_flag is not None:
            rendered.append(pending_flag)
            pending_flag = None
        rendered.append(shlex.quote(supplied))
    return rendered


def _repair_command(
    operation: NextRepairOperation,
    *,
    arguments: object,
) -> str:
    """Compose the runnable invocation for one repair operation."""

    parts = ["cruxible", _REPAIR_COMMAND_PATHS[operation]]
    values = arguments if isinstance(arguments, Mapping) else {}
    if operation == "playbill.block.repin":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
    elif operation == "playbill.claim.retire":
        claim_id = values.get("claim_id")
        if isinstance(claim_id, str):
            parts.append(shlex.quote(claim_id))
        parts.extend(_repair_operands(operation, values))
    elif operation == "playbill.floor.export":
        parts.extend(["--output", "FLOOR_DIR"])
    elif operation in _REPAIR_COMMAND_OPERANDS:
        parts.extend(_repair_operands(operation, values))
    return " ".join(parts)


def playbill_next_item_id(item: PlaybillNextItemV1) -> str:
    payload = item.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("item_id")
    return typed_digest(Sha256Value, NEXT_ITEM_ID_DOMAIN, payload).tagged


def _item(
    *,
    severity: NextSeverity,
    reason: NextReason,
    subject_identity: str,
    related_identities: tuple[str, ...] = (),
    detail: object,
    repair: PlaybillNextRepairV1,
) -> PlaybillNextItemV1:
    # Composed here rather than at each emitting site: a row whose command was
    # forgotten would be indistinguishable from one that has no command.
    repair = repair.model_copy(
        update={"command": _repair_command(repair.operation, arguments=repair.arguments)}
    )
    values = {
        "severity": severity,
        "reason": reason,
        "subject_identity": subject_identity,
        "related_identities": related_identities,
        "detail": detail,
        "repair": repair,
    }
    provisional = PlaybillNextItemV1.model_construct(
        _fields_set=None,
        item_id="sha256:" + "0" * 64,
        severity=severity,
        reason=reason,
        subject_identity=subject_identity,
        related_identities=related_identities,
        detail=detail,
        repair=repair,
    )
    return PlaybillNextItemV1.model_validate(
        {**values, "item_id": playbill_next_item_id(provisional)}
    )


def _item_sort_key(item: PlaybillNextItemV1) -> tuple[int, bytes, bytes, bytes]:
    return (
        _SEVERITY_RANK[item.severity],
        item.subject_identity.encode("utf-8"),
        item.reason.encode("utf-8"),
        item.item_id.encode("ascii"),
    )


def playbill_next_result_digest(result: PlaybillNextResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(Sha256Value, NEXT_RESULT_DIGEST_DOMAIN, payload).tagged


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    try:
        return instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    except ValueError as exc:
        raise PlaybillNextCoordinateNotAccepted(
            f"{PlaybillNextCoordinateNotAccepted.code}: coordinate is not accepted"
        ) from exc


def _claim_attestation_threshold_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    claims: tuple[ClaimArtifactAny, ...],
) -> tuple[PlaybillNextItemV1, ...]:
    """Emit v4 queue consequences from current independent attestation components."""

    internal = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    tree = instance.tree_at(coordinate.git_oid)
    claim_types: dict[str, ClaimType] = {}
    items: list[PlaybillNextItemV1] = []
    for claim in sorted(claims, key=lambda item: item.identity.qualified.encode("utf-8")):
        predicate = claim.statement.predicate
        claim_type = claim_types.get(predicate)
        if claim_type is None:
            path = claim_type_path(predicate)
            claim_type = parse_claim_type(tree[path], path=path)
            claim_types[predicate] = claim_type
        policy = claim_type.attestation_consequence_policy
        if policy is None:
            continue
        evidence = _claim_law_evidence(
            instance,
            path=claim_path(claim.identity.name),
            at=internal,
        )
        current = current_verified_claim_attestations(
            tree,
            claim,
            evidence.verified_attestations,
        )
        for rule in policy.rules:
            matching = tuple(
                item
                for item in current
                if item.current
                and item.attestation_grade == "verified_principal"
                and item.statement.provider_or_principal.kind == "Principal"
                and item.statement.claim_statement_digest
                == claim_statement_digest(claim.statement).tagged
                and item.statement.stance == rule.stance
                and item.statement.observed_at <= evaluation_time
                and (
                    item.statement.valid_until is None
                    or evaluation_time < item.statement.valid_until
                )
            )
            principal_identities = frozenset(
                item.statement.provider_or_principal.qualified for item in matching
            )
            if len(principal_identities) < rule.minimum_independent_control_components:
                continue
            attestation_digests = tuple(
                sorted(
                    (item.attestation_digest for item in matching),
                    key=lambda item: item.encode("ascii"),
                )
            )
            items.append(
                _item(
                    severity="warning",
                    reason="claim_attestation_threshold_met",
                    subject_identity=claim.identity.qualified,
                    related_identities=tuple(
                        sorted(
                            (
                                claim.statement.subject.artifact_path,
                                claim_type.identity.qualified,
                            ),
                            key=lambda item: item.encode("utf-8"),
                        )
                    ),
                    detail={
                        "claim_identity": claim.identity.qualified,
                        "claim_type_identity": claim_type.identity.qualified,
                        "claim_type_digest": claim_type_digest(claim_type).tagged,
                        "rule_id": rule.rule_id,
                        "stance": rule.stance,
                        "independent_control_component_count": len(principal_identities),
                        "minimum_independent_control_components": (
                            rule.minimum_independent_control_components
                        ),
                        "attestation_digests": list(attestation_digests),
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.create",
                        target=claim.identity.qualified,
                        required_change="resolve_attestation_threshold",
                        arguments={
                            "claim_id": claim.identity.name,
                            "rule_id": rule.rule_id,
                        },
                    ),
                )
            )
    return tuple(items)


def _claim_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDurationV1,
) -> tuple[PlaybillNextItemV1, ...]:
    listed = service_list_playbill_claims(instance, at=coordinate)
    claims = tuple(
        claim
        for claim in (_claim_from_view(view) for view in listed.claims)
        if claim.lifecycle.state == "live"
    )
    groups: dict[bytes, list[ClaimArtifactAny]] = defaultdict(list)
    for claim in claims:
        groups[
            canonical_bytes(
                {
                    "predicate": claim.statement.predicate,
                    "qualifier": claim.statement.qualifier,
                    "subject": claim.statement.subject.model_dump(mode="json"),
                }
            )
        ].append(claim)
    items = list(
        _claim_attestation_threshold_items(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            claims=claims,
        )
    )
    for group in groups.values():
        slot = classify_claim_slot(group)
        subject = group[0].statement.subject.artifact_path
        identities = tuple(
            sorted((claim.identity.qualified for claim in group), key=lambda item: item.encode())
        )
        if slot.resolution == "unresolved":
            discriminator = _qualifier_discriminator(group)
            detail: dict[str, object] = {
                "contender_count": slot.contender_count,
                "predicate": group[0].statement.predicate,
                "qualifier": group[0].statement.qualifier,
            }
            arguments: dict[str, object] = {"claim_ids": list(identities)}
            if discriminator is not None:
                detail["suggested_qualifier_field"] = discriminator
                arguments["qualifier_field"] = discriminator
            items.append(
                _item(
                    severity="blocking",
                    reason="claim_conflicted",
                    subject_identity=subject,
                    related_identities=identities,
                    detail=detail,
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.create",
                        target=subject,
                        required_change="revise_claims_into_distinct_qualifiers",
                        arguments=arguments,
                    ),
                )
            )
            continue
        for claim in group:
            verdict = service_evaluate_playbill_claim_verdict(
                instance,
                claim_identity=claim.identity.qualified,
                evaluation_time=evaluation_time,
                at=coordinate,
            ).verdict
            if verdict.verdict == "stale_evidence":
                expirations = (
                    verdict.freshness_expirations
                    if isinstance(verdict, ClaimVerdictResultV2)
                    else ()
                )
                items.append(
                    _item(
                        severity="repair",
                        reason="claim_stale_evidence",
                        subject_identity=claim.identity.qualified,
                        related_identities=(subject,),
                        detail={
                            "expired_capture_digests": [
                                item.capture_digest
                                for item in expirations
                                if evaluation_time >= item.expires_at
                            ],
                            "predicate": claim.statement.predicate,
                            "verdict": verdict.verdict,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.authoring.bind",
                            target=claim.identity.qualified,
                            required_change="recapture_expired_evidence",
                            arguments={"claim_id": claim.identity.name},
                        ),
                    )
                )
                continue
            if isinstance(verdict, ClaimVerdictResultV2) and verdict.verdict in {
                "supported",
                "contradicted",
                "unresolved",
            }:
                lead_end = evaluation_time + timedelta(microseconds=expiring_within.microseconds)
                supporting = set(verdict.supporting_evidence_digests)
                current_support_expirations = tuple(
                    item
                    for item in verdict.freshness_expirations
                    if item.capture_digest in supporting and evaluation_time < item.expires_at
                )
                expiring = tuple(
                    item
                    for item in current_support_expirations
                    if evaluation_time < item.expires_at <= lead_end
                )
                if expiring and not any(
                    item.expires_at > lead_end for item in current_support_expirations
                ):
                    items.append(
                        _item(
                            severity="warning",
                            reason="evidence_expiring",
                            subject_identity=claim.identity.qualified,
                            related_identities=(subject,),
                            detail={
                                "expirations": [item.model_dump(mode="json") for item in expiring],
                                "predicate": claim.statement.predicate,
                            },
                            repair=PlaybillNextRepairV1(
                                operation="playbill.authoring.bind",
                                target=claim.identity.qualified,
                                required_change="recapture_expiring_evidence",
                                arguments={"claim_id": claim.identity.name},
                            ),
                        )
                    )
            if verdict.verdict != "uncovered":
                continue
            items.append(
                _item(
                    severity="repair",
                    reason="claim_uncovered",
                    subject_identity=claim.identity.qualified,
                    related_identities=(subject,),
                    detail={
                        "currency": verdict.currency,
                        "predicate": claim.statement.predicate,
                        "verdict": verdict.verdict,
                        "policy_hint": (
                            "Review the ClaimType evidence_admission_policy; empty or "
                            "mismatched rules commonly leave a Claim uncovered."
                        ),
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.bind",
                        target=claim.identity.qualified,
                        required_change="add_admissible_evidence",
                        arguments={"claim_id": claim.identity.name},
                    ),
                )
            )
    return tuple(items)


def _qualifier_discriminator(claims: list[ClaimArtifactAny]) -> str | None:
    """Name the first field whose scalar values separate all semantic contenders."""

    contender_values: dict[bytes, Mapping[str, object]] = {}
    for claim in claims:
        if not isinstance(claim.statement.object, LiteralClaimObject):
            return None
        value = claim.statement.object.value
        if not isinstance(value, Mapping):
            return None
        contender_values.setdefault(
            canonical_bytes(claim.statement.object.model_dump(mode="json")), value
        )
    common = set.intersection(*(set(value) for value in contender_values.values()))
    ordered_fields = sorted(common, key=lambda item: item.encode("utf-8"))
    for field in ordered_fields:
        values = tuple(value[field] for value in contender_values.values())
        if not all(item is None or isinstance(item, (bool, int, str)) for item in values):
            continue
        if len({canonical_bytes(item) for item in values}) == len(values):
            return field
    return None


@dataclass(frozen=True)
class _CitationCommitment:
    citation_id: str
    commitment_digest: str
    byte_length: int
    claim_identity: str
    source_id: str | None
    source_digest: str | None
    original_start: int | None = None
    original_end: int | None = None
    whole_source: bool = False
    lineage_note: CitationLineageNote | None = None


@dataclass(frozen=True)
class _SourceAssociation:
    citation_id: str
    claim_identity: str
    commitment_digest: str
    source_id: str
    qualifying_publication: bool
    stale_publication: bool


def _whole_source_selection(envelope: object) -> bool:
    source = getattr(envelope, "source", None)
    if not isinstance(source, ExternalSourceReferenceV1):
        return False
    coordinate = source.coordinate
    selector = source.selector
    if not isinstance(coordinate, Mapping) or not isinstance(selector, Mapping):
        return False
    length = coordinate.get("source_byte_length")
    window = selector.get("working_selection", selector)
    if not isinstance(window, Mapping) or not isinstance(length, int) or isinstance(length, bool):
        return False
    return (
        window.get("start_byte") == 0
        and window.get("end_byte") == length
        and getattr(getattr(envelope, "commitment", None), "byte_length", None) == length
    )


def _source_selection_span(envelope: object) -> tuple[int, int] | None:
    """Read the accepted original byte window without inferring a locator."""

    source = getattr(envelope, "source", None)
    if not isinstance(source, ExternalSourceReferenceV1):
        return None
    selector = source.selector
    if not isinstance(selector, Mapping):
        return None
    window = selector.get("working_selection", selector)
    if not isinstance(window, Mapping):
        return None
    start, end = window.get("start_byte"), window.get("end_byte")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end
    ):
        return None
    return start, end


def _citation_commitments(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
) -> dict[str, _CitationCommitment]:
    listed = service_list_playbill_claims(instance, at=coordinate)
    internal_coordinate = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    result: dict[str, _CitationCommitment] = {}
    history = instance.accepted_history()
    target_index = next(
        index for index, generation in enumerate(history) if generation.oid == coordinate.git_oid
    )
    facts = build_accepted_query_facts(instance, coordinate=internal_coordinate)
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    visible_claims = {
        row.accepted.claim.identity.qualified
        for row in facts.claims
        if claim_row_visibility(
            row,
            subject=subjects.get(row.subject_path),
            providers=providers,
            policy=_PROJECTION_VISIBILITY_POLICY,
            evaluation_time=evaluation_time,
        )
        is not None
    }
    try:
        for view in listed.claims:
            claim = _claim_from_view(view)
            if claim.lifecycle.state != "live" or claim.identity.qualified not in visible_claims:
                continue
            evidence = _claim_law_evidence(
                instance,
                path=claim_path(claim.identity.name),
                at=internal_coordinate,
            )
            effective_captures = {item.capture_digest for item in evidence.verdict_captures}
            predecessor_digest = claim.lifecycle.predecessor_digest
            lineage_note: CitationLineageNote | None = None
            if predecessor_digest is not None:
                path = claim_path(claim.identity.name)
                first_scanned = max(0, target_index - MAX_DEPENDENCY_LINEAGE_GENERATIONS)
                predecessor = next(
                    (
                        parsed
                        for generation in reversed(history[first_scanned:target_index])
                        for content in (instance.tree_at(generation.oid).get(path),)
                        if content is not None
                        for parsed in (parse_claim(content, path=path),)
                        if claim_artifact_digest(parsed).tagged == predecessor_digest
                    ),
                    None,
                )
                if predecessor is None:
                    lineage_note = (
                        "predecessor_lineage_limit_exceeded"
                        if first_scanned > 0
                        else "predecessor_unresolved"
                    )
                elif claim_statement_digest(predecessor.statement) != claim_statement_digest(
                    claim.statement
                ):
                    effective_captures.difference_update(predecessor.backing.capture_digests)
            for citation in claim_citation_references(claim):
                if citation.capture_digest not in effective_captures:
                    continue
                envelope = parse_capture_envelope(
                    store.read(citation.capture_digest, access=access)
                )
                source_id: str | None = None
                source_digest: str | None = None
                if (
                    isinstance(envelope.source, ExternalSourceReferenceV1)
                    and envelope.source.coordinate_type == FOREIGN_SOURCE_COORDINATE_TYPE
                    and isinstance(envelope.source.coordinate, Mapping)
                ):
                    observed_digest = envelope.source.coordinate.get("source_content_digest")
                    if isinstance(observed_digest, str):
                        try:
                            Sha256Value.from_tagged(observed_digest)
                        except ValueError:
                            pass
                        else:
                            source_id = envelope.source.source_identity
                            source_digest = observed_digest
                selection_span = _source_selection_span(envelope)
                result[citation.citation_id] = _CitationCommitment(
                    citation_id=citation.citation_id,
                    commitment_digest=envelope.commitment.digest,
                    byte_length=envelope.commitment.byte_length or 0,
                    claim_identity=claim.identity.qualified,
                    source_id=source_id,
                    source_digest=source_digest,
                    original_start=None if selection_span is None else selection_span[0],
                    original_end=None if selection_span is None else selection_span[1],
                    whole_source=_whole_source_selection(envelope),
                    lineage_note=lineage_note,
                )
    except Exception as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: citation inventory is invalid"
        ) from exc
    return result


def _source_associations(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
) -> tuple[_SourceAssociation, ...]:
    """Fold historical citation pins without relying on the live-only coverage index."""

    listed = service_list_playbill_claims(instance, at=coordinate, include_retired=True)
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    associations: list[_SourceAssociation] = []
    verdicts: dict[str, str] = {}
    try:
        for view in listed.claims:
            claim = _claim_from_view(view)
            for reference in claim_citation_references(claim):
                envelope = parse_capture_envelope(
                    store.read(reference.capture_digest, access=access)
                )
                source = envelope.source
                if (
                    not isinstance(source, ExternalSourceReferenceV1)
                    or source.coordinate_type != FOREIGN_SOURCE_COORDINATE_TYPE
                ):
                    continue
                qualifying = (
                    isinstance(reference, ClaimCitationV1)
                    and reference.role == "copy"
                    and reference.origin == "self_published"
                )
                stale = False
                if qualifying:
                    if claim.lifecycle.state == "retired":
                        stale = True
                    else:
                        verdict = verdicts.get(claim.identity.qualified)
                        if verdict is None:
                            verdict = service_evaluate_playbill_claim_verdict(
                                instance,
                                claim_identity=claim.identity.qualified,
                                evaluation_time=evaluation_time,
                                at=coordinate,
                            ).verdict.verdict
                            verdicts[claim.identity.qualified] = verdict
                        stale = verdict == "contradicted"
                associations.append(
                    _SourceAssociation(
                        citation_id=reference.citation_id,
                        claim_identity=claim.identity.qualified,
                        commitment_digest=envelope.commitment.digest,
                        source_id=source.source_identity,
                        qualifying_publication=qualifying,
                        stale_publication=stale,
                    )
                )
    except Exception as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: publication association fold is invalid"
        ) from exc
    return tuple(
        sorted(
            associations,
            key=lambda item: (
                item.source_id.encode("utf-8"),
                item.commitment_digest.encode("ascii"),
                item.claim_identity.encode("utf-8"),
                item.citation_id.encode("ascii"),
            ),
        )
    )


def _self_published_source_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    if observation.presentation_policy_notes:
        return ()
    policy = observation.presentation_policy or PlaybillPresentationPolicyV1()
    archival = set(policy.archival_source_ids)
    observed = {
        item.source_id: item
        for item in observation.source_observations
        if isinstance(
            item,
            (
                PlaybillNextSourceObservationV3,
                PlaybillNextSourceObservationV4,
            ),
        )
        and (
            not item.scan_notes
            if isinstance(item, PlaybillNextSourceObservationV4)
            else item.scan_complete
        )
        and not item.scan_notes
        and not item.marker_notes
    }
    associations = _source_associations(
        instance,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
    )
    grouped: dict[tuple[str, str], list[_SourceAssociation]] = defaultdict(list)
    for association in associations:
        grouped[(association.source_id, association.commitment_digest)].append(association)

    items: list[PlaybillNextItemV1] = []
    for (source_id, commitment_digest), group in sorted(
        grouped.items(), key=lambda item: (item[0][0].encode("utf-8"), item[0][1].encode("ascii"))
    ):
        source = observed.get(source_id)
        if source is None or source_id in archival:
            continue
        occurrences = tuple(
            item
            for item in source.occurrences
            if item.observed_commitment_digest == commitment_digest
        )
        if len(occurrences) != 1:
            continue
        occurrence = occurrences[0]
        if any(
            occurrence.line_overlay.start_byte < marker.end_byte
            and occurrence.line_overlay.end_byte > marker.start_byte
            for marker in source.marker_summaries
        ):
            continue
        if any(not item.qualifying_publication for item in group):
            continue
        if any(item.qualifying_publication and not item.stale_publication for item in group):
            continue
        stale = tuple(
            sorted(
                {item.claim_identity for item in group if item.stale_publication},
                key=lambda item: item.encode("utf-8"),
            )
        )
        if not stale:
            continue
        items.append(
            _item(
                severity="warning",
                reason="self_published_source_stale",
                subject_identity=source_id,
                related_identities=stale,
                detail={
                    "source_id": source_id,
                    "commitment_digest": commitment_digest,
                    "occurrence_identity_digest": occurrence.identity_digest,
                    "stale_claim_identities": list(stale),
                },
                repair=PlaybillNextRepairV1(
                    operation="playbill.authoring.create",
                    target=source_id,
                    required_change="review_self_published_passage",
                    arguments={
                        "source_id": source_id,
                        "occurrence_identity_digest": occurrence.identity_digest,
                    },
                ),
            )
        )
    return tuple(items)


def _bounded_claim_lineages(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    current_claims: Mapping[str, ClaimArtifactAny],
) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """Resolve only authenticated accepted history, capped at 256 generations."""

    history = instance.accepted_history()
    target_index = next(
        index for index, item in enumerate(history) if item.oid == coordinate.git_oid
    )
    lineages: dict[str, list[str]] = {
        path: [claim_artifact_digest(claim).tagged] for path, claim in current_claims.items()
    }
    expected: dict[str, str] = {
        path: claim.lifecycle.predecessor_digest
        for path, claim in current_claims.items()
        if claim.lifecycle.predecessor_digest is not None
    }
    scanned = history[max(0, target_index - MAX_DEPENDENCY_LINEAGE_GENERATIONS) : target_index]
    for generation in reversed(scanned):
        if not expected:
            break
        tree = instance.tree_at(generation.oid)
        for path in tuple(sorted(expected, key=lambda item: item.encode("utf-8"))):
            raw = tree.get(path)
            if raw is None:
                continue
            claim = parse_claim(raw, path=path)
            digest = claim_artifact_digest(claim).tagged
            if digest != expected[path]:
                continue
            lineages[path].append(digest)
            predecessor = claim.lifecycle.predecessor_digest
            if predecessor is None:
                expected.pop(path)
            else:
                expected[path] = predecessor
    return (
        {
            path: tuple(sorted(set(digests), key=lambda item: item.encode("ascii")))
            for path, digests in lineages.items()
        },
        frozenset(expected),
    )


def _claim_dependency_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
) -> tuple[PlaybillNextItemV1, ...]:
    """Coalesce stale recorded backing-input edges through the existing impact walker."""

    if not access_profile.permits("instance"):
        return ()
    facts = build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        include_retired=True,
    )
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    visible_rows = tuple(
        row
        for row in facts.claims
        if claim_row_visibility(
            row,
            subject=subjects.get(row.subject_path),
            providers=providers,
            policy=_PROJECTION_VISIBILITY_POLICY,
            evaluation_time=evaluation_time,
        )
        is not None
    )
    visible_facts = facts.model_copy(update={"claims": visible_rows})
    current_claims = {row.accepted.path: row.accepted.claim for row in visible_rows}
    lineages, incomplete = _bounded_claim_lineages(
        instance,
        coordinate=coordinate,
        current_claims=current_claims,
    )
    recorded_input_digests = frozenset(
        digest for row in visible_rows for digest in row.accepted.claim.backing.input_claim_digests
    )
    dependency_sources = tuple(
        row
        for row in visible_rows
        if recorded_input_digests.intersection(lineages[row.accepted.path])
    )

    by_dependent: dict[str, list[dict[str, object]]] = defaultdict(list)
    public_coordinate = AcceptedCoordinate.from_internal(coordinate)
    for source in dependency_sources:
        impact = build_dependency_impact(
            DependencyImpactRequestV1(
                at=public_coordinate,
                address=SemanticAddress.claim_statement(source.accepted.path),
                evaluation_time=evaluation_time,
            ),
            facts=visible_facts,
            source_lineages=lineages,
            include_retired_sources=True,
        )
        for dependent in impact.dependents:
            if (
                dependent.kind != "Claim"
                or dependent.dependency_kind != "backing_input"
                or not dependent.repair_candidate
                or not ({SOURCE_SUPERSEDED, SOURCE_CONTRADICTED} & set(dependent.impact_reasons))
            ):
                continue
            by_dependent[dependent.identity].append(
                {
                    "source_claim_identity": source.accepted.claim.identity.qualified,
                    "used_artifact_digest": dependent.used_artifact_digest,
                    "current_artifact_digest": dependent.current_artifact_digest,
                    "impact_reasons": list(dependent.impact_reasons),
                    "lineage_complete": source.accepted.path not in incomplete,
                }
            )

    items: list[PlaybillNextItemV1] = []
    for identity in sorted(by_dependent, key=lambda item: item.encode("utf-8")):
        stale_inputs = sorted(
            by_dependent[identity],
            key=lambda item: (
                str(item["source_claim_identity"]).encode("utf-8"),
                str(item["used_artifact_digest"]).encode("ascii"),
            ),
        )
        related = tuple(
            sorted(
                {str(item["source_claim_identity"]) for item in stale_inputs},
                key=lambda item: item.encode("utf-8"),
            )
        )
        items.append(
            _item(
                severity="repair",
                reason="claim_dependency_stale",
                subject_identity=identity,
                related_identities=related,
                detail={"stale_inputs": stale_inputs},
                repair=PlaybillNextRepairV1(
                    operation="playbill.authoring.create",
                    target=identity,
                    required_change="reauthor_claim_from_current_inputs",
                    arguments={"claim_id": identity.removeprefix("Claim:")},
                ),
            )
        )
    return tuple(items)


def _source_citation_item(
    *,
    citation_id: str,
    commitment: _CitationCommitment,
    observed: PlaybillNextSourceObservationAny | None,
    coordinate: PlaybillAcceptedCoordinate,
) -> PlaybillNextItemV1 | None:
    source_id = commitment.source_id
    captured_source_digest = commitment.source_digest
    assert source_id is not None and captured_source_digest is not None
    if observed is None:
        return _citation_unobserved_item(commitment)
    if isinstance(observed, PlaybillNextSourceObservationV4):
        expected_source = LogicalSourceIdentityV1(plane="external", identity=source_id)
        proved = any(
            proof.source == expected_source
            and proof.commitment_digest == commitment.commitment_digest
            and proof.byte_length == commitment.byte_length
            for proof in observed.commitment_scan_proofs
        )
        if not proved:
            return _citation_unobserved_item(commitment)
        occurrences = tuple(
            item
            for item in observed.occurrences
            if item.source == expected_source
            and item.observed_commitment_digest == commitment.commitment_digest
            and item.byte_length == commitment.byte_length
        )
        if len(occurrences) == 1:
            return None
        if len(occurrences) > 1:
            return _citation_drift_item(
                commitment,
                coordinate=coordinate,
                drift_state="ambiguous",
                occurrences=occurrences,
            )
        if commitment.original_start is None or commitment.original_end is None:
            return _citation_unobserved_item(commitment)
        windows = tuple(
            item
            for item in observed.citation_window_observations
            if item.citation_id == citation_id
            and item.commitment_digest == commitment.commitment_digest
            and item.original_start == commitment.original_start
            and item.original_end == commitment.original_end
        )
        if len(windows) != 1:
            return _citation_unobserved_item(commitment)
        window = windows[0]
        if not window.addressable:
            return _citation_drift_item(
                commitment,
                coordinate=coordinate,
                drift_state="gone",
            )
        if window.observed_window_digest == commitment.commitment_digest:
            raise PlaybillNextWorkspaceObservationInvalid(
                f"{PlaybillNextWorkspaceObservationInvalid.code}: "
                "a complete zero-occurrence proof contradicts its unchanged original window"
            )
        return _citation_drift_item(
            commitment,
            coordinate=coordinate,
            drift_state="changed",
            observed_window_digest=window.observed_window_digest,
        )

    unobserved = (
        isinstance(observed, PlaybillNextSourceObservationV3) and not observed.scan_complete
    )
    if isinstance(observed, PlaybillNextSourceObservationV3):
        if observed.scan_complete:
            matched = any(
                item.observed_commitment_digest == commitment.commitment_digest
                for item in observed.occurrences
            )
            whole_source_current = (
                commitment.whole_source
                and observed.observed_source_digest == captured_source_digest
            )
            if whole_source_current or (not commitment.whole_source and matched):
                return None
            if commitment.commitment_digest not in observed.scanned_commitment_digests:
                unobserved = True
    if unobserved:
        return _citation_unobserved_item(commitment)
    return _citation_drift_item(
        commitment,
        coordinate=coordinate,
        drift_state="changed",
        observed_window_digest=observed.observed_source_digest,
    )


def _citation_unobserved_item(commitment: _CitationCommitment) -> PlaybillNextItemV1:
    source_id = commitment.source_id
    assert source_id is not None
    lineage_detail = (
        {} if commitment.lineage_note is None else {"lineage_note": commitment.lineage_note}
    )
    return _item(
        severity="warning",
        reason="citation_source_unobserved",
        subject_identity=commitment.claim_identity,
        related_identities=(commitment.citation_id,),
        detail={
            "citation_id": commitment.citation_id,
            "source_id": source_id,
            "expected_source_digest": commitment.source_digest,
            **lineage_detail,
        },
        repair=PlaybillNextRepairV1(
            operation="playbill.authoring.bind",
            target=commitment.claim_identity,
            required_change="observe_cited_source",
            arguments={
                "claim_id": commitment.claim_identity.removeprefix("Claim:"),
                "citation_id": commitment.citation_id,
                "source_id": source_id,
            },
        ),
    )


def _citation_drift_item(
    commitment: _CitationCommitment,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    drift_state: Literal["changed", "gone", "ambiguous"],
    observed_window_digest: str | None = None,
    occurrences: tuple[WorkingOccurrenceV1, ...] = (),
) -> PlaybillNextItemV1:
    source_id = commitment.source_id
    source = (
        None
        if source_id is None
        else LogicalSourceIdentityV1(plane="external", identity=source_id).model_dump(mode="json")
    )
    occurrence_spans = [
        {
            "end_byte": item.line_overlay.end_byte,
            "identity_digest": item.identity_digest,
            "start_byte": item.line_overlay.start_byte,
        }
        for item in sorted(
            occurrences,
            key=lambda item: (
                item.line_overlay.start_byte,
                item.line_overlay.end_byte,
                item.identity_digest.encode("ascii"),
            ),
        )
    ]
    detail: dict[str, object] = {
        "accepted_claim_identity": commitment.claim_identity,
        "accepted_coordinate": coordinate.model_dump(mode="json"),
        "citation_id": commitment.citation_id,
        "drift_state": drift_state,
        "expected_commitment_digest": commitment.commitment_digest,
        "exact_occurrence_count": len(occurrences),
        "exact_occurrence_spans": occurrence_spans,
        "logical_source": source,
        "original_span": (
            None
            if commitment.original_start is None or commitment.original_end is None
            else {
                "end_byte": commitment.original_end,
                "start_byte": commitment.original_start,
            }
        ),
    }
    if observed_window_digest is not None:
        detail["observed_window_digest"] = observed_window_digest
    if commitment.lineage_note is not None:
        detail["lineage_note"] = commitment.lineage_note
    gone = drift_state == "gone"
    return _item(
        severity="repair",
        reason="citation_drifted",
        subject_identity=commitment.claim_identity,
        related_identities=(commitment.citation_id,),
        detail=detail,
        repair=PlaybillNextRepairV1(
            operation="playbill.claim.retire" if gone else "playbill.authoring.bind",
            target=commitment.claim_identity,
            required_change=(
                "retire_claim_with_attribution" if gone else "adjudicate_citation_drift"
            ),
            arguments={
                "claim_id": commitment.claim_identity.removeprefix("Claim:"),
                **(
                    {"expected_coordinate": coordinate.model_dump(mode="json")}
                    if gone
                    else {
                        "citation_id": commitment.citation_id,
                        **({} if source_id is None else {"source_id": source_id}),
                    }
                ),
            },
        ),
    )


def _workspace_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[tuple[NextDomain, ...], tuple[PlaybillNextItemV1, ...]]:
    if observation is None:
        return (), ()
    domains: list[NextDomain] = []
    items: list[PlaybillNextItemV1] = []
    if observation.floor_status is not None or observation.installed_coordinate is not None:
        domains.append("workspace_floor")
        status = observation.floor_status
        reason: NextReason | None
        if status in {"not_configured", "missing"}:
            reason = "floor_missing"
        elif status == "invalid":
            reason = "floor_invalid"
        elif observation.installed_coordinate is not None and (
            observation.installed_coordinate
            != AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json"))
        ):
            reason = "floor_stale"
        elif status == "stale" and observation.installed_coordinate is None:
            reason = "floor_stale"
        else:
            reason = None
        if reason is not None:
            items.append(
                _item(
                    severity="warning" if reason != "floor_invalid" else "blocking",
                    reason=reason,
                    subject_identity=coordinate.git_oid,
                    detail={
                        "installed_coordinate": (
                            None
                            if observation.installed_coordinate is None
                            else observation.installed_coordinate.model_dump(mode="json")
                        ),
                        "reported_status": status,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.floor.export",
                        target=instance.descriptor.instance_id,
                        required_change="replace_installed_floor",
                        arguments={},
                    ),
                )
            )
    if observation.drift_observations is not None and access_profile.permits("instance"):
        domains.append("workspace_sources")
        commitments = _citation_commitments(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
        )
        for drift in observation.drift_observations:
            expected = commitments.get(drift.citation_id)
            if expected is None or expected.commitment_digest != drift.expected_commitment_digest:
                raise PlaybillNextWorkspaceObservationInvalid(
                    f"{PlaybillNextWorkspaceObservationInvalid.code}: "
                    f"citation {drift.citation_id} does not match accepted state"
                )
            if drift.observed_commitment_digest == drift.expected_commitment_digest:
                continue
            items.append(
                _citation_drift_item(
                    expected,
                    coordinate=coordinate,
                    drift_state="changed",
                    observed_window_digest=drift.observed_commitment_digest,
                )
            )
    elif observation.source_observations is not None and access_profile.permits("instance"):
        domains.append("workspace_sources")
        observed = {source.source_id: source for source in observation.source_observations}
        commitments = _citation_commitments(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
        )
        for citation_id in sorted(commitments, key=lambda item: item.encode("ascii")):
            commitment = commitments[citation_id]
            if commitment.source_id is None or commitment.source_digest is None:
                continue
            item = _source_citation_item(
                citation_id=citation_id,
                commitment=commitment,
                observed=observed.get(commitment.source_id),
                coordinate=coordinate,
            )
            if item is not None:
                items.append(item)
    return tuple(domains), tuple(items)


def _document_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    tree = instance.tree_at(coordinate.git_oid)
    items: list[PlaybillNextItemV1] = []
    for source in observation.source_observations:
        document_id = getattr(source, "document_id", None)
        if document_id is None:
            continue
        path = document_path(document_id)
        content = tree.get(path)
        if content is None:
            continue
        document = parse_document(content, path=path)
        if document.body_digest == source.observed_source_digest:
            continue
        identity = f"document:{document_id}"
        items.append(
            _item(
                severity="warning",
                reason="document_modified",
                subject_identity=identity,
                related_identities=(source.source_id,),
                detail={
                    "document_id": document_id,
                    "source_id": source.source_id,
                    "accepted_body_digest": document.body_digest,
                    "observed_source_digest": source.observed_source_digest,
                },
                repair=PlaybillNextRepairV1(
                    operation="playbill.document.propose",
                    target=identity,
                    required_change="repropose_modified_document",
                    arguments={
                        "document_id": document_id,
                        "source_id": source.source_id,
                    },
                ),
            )
        )
    return tuple(items)


def _projection_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Evaluate locally declared blocks only after every backing is visible."""

    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    sources = tuple(
        source
        for source in observation.source_observations
        if isinstance(
            source,
            (
                PlaybillNextSourceObservationV3,
                PlaybillNextSourceObservationV4,
            ),
        )
        and (
            not source.scan_notes
            if isinstance(source, PlaybillNextSourceObservationV4)
            else source.scan_complete
        )
        and not source.marker_notes
        and source.marker_summaries
    )
    if not sources:
        return ()

    tree = instance.tree_at(coordinate.git_oid)
    facts = build_accepted_query_facts(instance, coordinate=coordinate)
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    claims = {row.accepted.claim.identity.qualified: row for row in facts.claims}
    resolution_statuses = claim_resolution_statuses(
        instance,
        claims=tuple(row.accepted.claim for row in facts.claims),
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
    )
    items: list[PlaybillNextItemV1] = []
    for source in sources:
        for marker in source.marker_summaries:
            visible = True
            stale: list[str] = []
            retired: list[str] = []
            overturned: list[str] = []
            for backing in marker.stamp.backing:
                if isinstance(backing, ProjectionClaimBackingV1):
                    claim = claims.get(backing.identity.qualified)
                    if claim is None:
                        path = claim_path(backing.identity.name)
                        raw = tree.get(path)
                        if (
                            raw is not None
                            and parse_claim(raw, path=path).lifecycle.state == "retired"
                        ):
                            retired.append(backing.identity.qualified)
                            continue
                        visible = False
                        break
                    if (
                        claim_row_visibility(
                            claim,
                            subject=subjects.get(claim.subject_path),
                            providers=providers,
                            policy=_PROJECTION_VISIBILITY_POLICY,
                            evaluation_time=evaluation_time,
                        )
                        is None
                    ):
                        visible = False
                        break
                    if resolution_statuses[claim.accepted.claim.identity.name] == "overturned":
                        overturned.append(backing.identity.qualified)
                        continue
                    if claim.accepted.statement_digest != backing.statement_digest:
                        stale.append(backing.identity.qualified)
                elif isinstance(backing, ProjectionQueryBackingV1):
                    try:
                        definition = accepted_query_definition(
                            instance,
                            name=backing.identity.name,
                            coordinate=coordinate,
                        )
                        result = evaluate_claim_query(
                            definition,
                            facts=facts,
                            coordinate=coordinate,
                            evaluation_time=evaluation_time,
                            parameters={
                                item.name: item.value
                                for item in backing.resolved_parameter_bindings
                            },
                        )
                    except (PlaybillError, ValueError):
                        visible = False
                        break
                    if result.verdict != "completed" or result.truncation.clipped_budgets:
                        visible = False
                        break
                    if projection_query_semantic_result_digest(result) != (
                        backing.semantic_result_digest
                    ):
                        stale.append(backing.identity.qualified)

            if not visible:
                continue
            target = f"{source.source_id}#{marker.stamp.block_id}"
            identities = tuple(
                sorted(
                    (backing.identity.qualified for backing in marker.stamp.backing),
                    key=lambda value: value.encode("utf-8"),
                )
            )
            arguments = {"source_id": source.source_id, "block_id": marker.stamp.block_id}
            if marker.observed_body_digest != marker.stamp.body_digest:
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_dirty",
                        subject_identity=target,
                        related_identities=identities,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "expected_body_digest": marker.stamp.body_digest,
                            "observed_body_digest": marker.observed_body_digest,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="verify_alignment_then_repin_or_edit",
                            arguments=arguments,
                        ),
                    )
                )
            if retired:
                related = tuple(sorted(retired, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "retired_backings": list(related),
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="depublish_retired_backing_block",
                            arguments=arguments,
                        ),
                    )
                )
            if overturned:
                related = tuple(sorted(overturned, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "overturned_backings": list(related),
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="depublish_overturned_backing_block",
                            arguments=arguments,
                        ),
                    )
                )
            if stale:
                related = tuple(sorted(stale, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity="repair",
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "stale_backings": list(related),
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="review_block_supersede_prose_then_repin",
                            arguments=arguments,
                        ),
                    )
                )
    return tuple(items)


def service_playbill_next(
    instance: PlaybillInstance,
    *,
    request: PlaybillNextRequestV1,
) -> PlaybillNextResultV1:
    """Fold accepted state and explicit client observations into one repair queue."""

    coordinate = _resolve_coordinate(instance, request.at)
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    workspace_domains, workspace_items = _workspace_items(
        instance,
        coordinate=public_coordinate,
        evaluation_time=request.evaluation_time,
        access_profile=request.access_profile,
        observation=request.workspace_observation,
    )
    observed = tuple(
        domain
        for domain in _ALL_DOMAINS
        if domain == "accepted_state" or domain in workspace_domains
    )
    unobserved = tuple(domain for domain in _ALL_DOMAINS if domain not in observed)
    items = tuple(
        sorted(
            (
                *_claim_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    expiring_within=request.expiring_within,
                ),
                *workspace_items,
                *_projection_items(
                    instance,
                    coordinate=coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
                *_self_published_source_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
                *_claim_dependency_items(
                    instance,
                    coordinate=coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                ),
                *_document_items(
                    instance,
                    coordinate=coordinate,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
            ),
            key=_item_sort_key,
        )
    )
    values = {
        "coordinate": public_coordinate,
        "evaluation_time": request.evaluation_time,
        "observed_domains": observed,
        "unobserved_domains": unobserved,
        "items": items,
    }
    provisional = PlaybillNextResultV1.model_construct(
        _fields_set=None,
        result_digest="sha256:" + "0" * 64,
        coordinate=public_coordinate,
        evaluation_time=request.evaluation_time,
        observed_domains=observed,
        unobserved_domains=unobserved,
        items=items,
    )
    result_digest = playbill_next_result_digest(provisional)
    full = PlaybillNextResultV1.model_validate({**values, "result_digest": result_digest})
    _remember_queue(result_digest, full.items)
    if request.since_result_digest is None:
        return full
    return _delta_of(full, since=request.since_result_digest)


# Bounded, per-process memory of which rows each queue digest stood for. A miss
# -- restart, eviction, a digest minted elsewhere -- is not an error: it yields
# the whole queue, which answers the caller's question either way.
_QUEUE_MEMO: OrderedDict[str, frozenset[str]] = OrderedDict()
_QUEUE_MEMO_LIMIT = 32


def _remember_queue(result_digest: str, items: tuple[PlaybillNextItemV1, ...]) -> None:
    _QUEUE_MEMO.pop(result_digest, None)
    _QUEUE_MEMO[result_digest] = frozenset(item.item_id for item in items)
    while len(_QUEUE_MEMO) > _QUEUE_MEMO_LIMIT:
        _QUEUE_MEMO.popitem(last=False)


def _delta_of(full: PlaybillNextResultV1, *, since: str) -> PlaybillNextResultV1:
    """Return only the rows new since a remembered queue, keeping the full cursor.

    `result_digest` still names the whole queue: it is what the caller echoes
    back next time, so it must not describe the subset carried here.
    """

    seen = _QUEUE_MEMO.get(since)
    if seen is None:
        return full
    fresh = tuple(item for item in full.items if item.item_id not in seen)
    return full.model_copy(update={"items": fresh, "delta_since": since})


__all__ = [
    "DEFAULT_EXPIRING_WITHIN_MICROSECONDS",
    "NEXT_ITEM_ID_DOMAIN",
    "NEXT_RESULT_DIGEST_DOMAIN",
    "PlaybillNextAccessProfileInvalid",
    "PlaybillNextAcceptedStateInvalid",
    "PlaybillNextCoordinateNotAccepted",
    "PlaybillNextDriftObservationV1",
    "PlaybillNextItemV1",
    "PlaybillNextRequestV1",
    "PlaybillNextResultV1",
    "PlaybillNextSourceObservationV3",
    "PlaybillNextSourceObservationV4",
    "PlaybillNextWorkspaceObservationInvalid",
    "PlaybillNextWorkspaceObservationV1",
    "playbill_next_item_id",
    "playbill_next_result_digest",
    "service_playbill_next",
    "validate_playbill_next_request",
]
