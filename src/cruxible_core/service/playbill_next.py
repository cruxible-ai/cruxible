"""Deterministic repair queue derived from one accepted Playbill coordinate."""

from __future__ import annotations

import shlex
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts import (
    PLAYBILL_HAND_EDIT_NEXT_REASONS,
    PlaybillBlockSyncReadRequestV1,
    PlaybillNextReason,
    ProviderLaneStatusV1,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
    CanonicalDurationV1,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEventPayloadV1,
    ClaimAttestationEventV1,
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
    ClaimArtifactV3,
    ClaimCitationV1,
    ClaimLawEvidenceAny,
    LiteralClaimObject,
    _is_claim_type_rederivation,
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
    PlaybillPresentationPolicyAny,
    PlaybillPresentationPolicyNoteV1,
    PlaybillPresentationPolicyV1,
    PlaybillProjectionCoverageObservationV1,
    ProjectionArtifactBackingV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
    ProjectionQueryBackingV1,
    projection_query_semantic_result_digest,
    upgrade_playbill_presentation_policy,
)
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.procedures.artifacts import parse_procedure
from cruxible_client.contracts.query.definitions import QueryEvaluationPolicyV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.citation_relations import (
    RELATION_RETIRED_CONFLICT_SCHEMA,
    RELATION_SOURCE_USE_SCHEMA,
    external_source_relation_subject,
    logical_source_relation_subject,
    retired_activation_live_candidates,
)
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
    CaptureAdmissionAccountV1,
    _claim_admission_accounts,
    _claim_from_view,
    _claim_law_evidence,
    _claim_law_evidence_by_artifact_index,
    _claim_law_evidence_index,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_evidence import (
    current_verified_claim_attestations,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.playbill_projection_sync import (
    service_read_playbill_block_sync_backing,
)
from cruxible_core.service.playbill_publications import bound_publication_registrations
from cruxible_core.service.playbill_query import build_accepted_query_facts
from cruxible_core.service.playbill_search import claim_resolution_statuses

NEXT_ITEM_ID_DOMAIN = "playbill-next-item-v1"
NEXT_RESULT_DIGEST_DOMAIN = "playbill-next-result-v1"
NEXT_RESULT_V2_DIGEST_DOMAIN = "playbill-next-result-v2"
DEFAULT_EXPIRING_WITHIN_MICROSECONDS = 604_800_000_000
MAX_DEPENDENCY_LINEAGE_GENERATIONS = 256

NextDomain = Literal[
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
    "workspace_projections",
]
NextSeverity = Literal["blocking", "repair", "warning"]
CitationLineageNote = Literal[
    "predecessor_lineage_limit_exceeded",
    "predecessor_unresolved",
]
NextReason: TypeAlias = PlaybillNextReason
HAND_EDIT_NEXT_REASONS = PLAYBILL_HAND_EDIT_NEXT_REASONS
NextRepairOperation = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim.retire",
    "playbill.floor.export",
    "playbill.block.repin",
    "playbill.block.sync",
    "playbill.document.propose",
    "hand_edit",
]

_SEVERITY_RANK: dict[NextSeverity, int] = {"blocking": 0, "repair": 1, "warning": 2}
_ALL_DOMAINS: tuple[NextDomain, ...] = (
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
    "workspace_projections",
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
    presentation_policy: PlaybillPresentationPolicyAny | None = None
    presentation_policy_notes: tuple[PlaybillPresentationPolicyNoteV1, ...] = ()
    projection_coverage: PlaybillProjectionCoverageObservationV1 | None = None

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


class PlaybillNextRequestV2(PlaybillNextRequestV1):
    tag: Literal["playbill-next-request-v2"] = "playbill-next-request-v2"  # type: ignore[assignment]
    at_attestation_head_digest: str | None = None

    @field_validator("at_attestation_head_digest")
    @classmethod
    def _attestation_head(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


PlaybillNextRequestAny: TypeAlias = PlaybillNextRequestV1 | PlaybillNextRequestV2


def validate_playbill_next_request(
    value: PlaybillNextRequestAny | Mapping[str, object],
) -> PlaybillNextRequestAny:
    if isinstance(value, (PlaybillNextRequestV1, PlaybillNextRequestV2)):
        return value
    try:
        model = (
            PlaybillNextRequestV2
            if value.get("tag") == "playbill-next-request-v2"
            else PlaybillNextRequestV1
        )
        return model.model_validate(value)
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
    # Runnable operations are dotted CLI paths whose arguments are their options.
    # Their command is composed from the digested fields beside it, so it stays
    # deterministic inside the item_id and result_digest preimages. Hand edits
    # instead carry a target and required change and never claim a command.
    command: str | None = None

    @field_validator("arguments", mode="before")
    @classmethod
    def _arguments(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _hand_edit_shape(self) -> "PlaybillNextRepairV1":
        if self.operation == "hand_edit":
            if not self.target.strip() or not self.required_change.strip():
                raise ValueError("hand-edit repairs require a target and required change")
            if self.command is not None:
                raise ValueError("hand-edit repairs cannot claim a runnable command")
        return self


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
    # Set only on a delta. The carried items are the deterministic symmetric
    # difference from that earlier queue while `result_digest` remains the
    # current whole-queue cursor. The server remembers that full queue so the
    # digest remains usable on the next call.
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


class PlaybillNextResultV2(PlaybillNextResultV1):
    tag: Literal["playbill-next-result-v2"] = "playbill-next-result-v2"  # type: ignore[assignment]
    attestation_head_digest: str
    removed_item_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        exclude_if=lambda value: not value,
    )

    @field_validator("attestation_head_digest")
    @classmethod
    def _attestation_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("removed_item_ids")
    @classmethod
    def _removed_item_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item_id in value:
            Sha256Value.from_tagged(item_id)
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("removed next item IDs must be ASCII byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _v2_digest(self) -> "PlaybillNextResultV2":
        if self.removed_item_ids and self.delta_since is None:
            raise ValueError("removed next item IDs are valid only on a delta")
        carried_ids = frozenset(item.item_id for item in self.items)
        if not set(self.removed_item_ids).issubset(carried_ids):
            raise ValueError("removed next item IDs must name carried delta rows")
        if self.delta_since is None and self.result_digest != playbill_next_result_digest(self):
            raise ValueError("next v2 result digest does not reproduce")
        return self


_REPAIR_COMMAND_PATHS: Mapping[str, str] = {
    "playbill.authoring.create": "playbill authoring create",
    "playbill.authoring.bind": "playbill authoring bind",
    "playbill.claim.retire": "playbill claim retire",
    "playbill.floor.export": "playbill floor export",
    "playbill.block.repin": "playbill block repin",
    "playbill.block.sync": "playbill block sync",
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
_ATTESTATION_REPAIR_EXAMPLES: Mapping[str, str] = {
    "adjudicate_contradicting_evidence": "claim-adjudicate-contradicting-evidence",
    "cite_supporting_evidence": "claim-cite-supporting-evidence",
    "adjudicate_unreviewed_evidence": "claim-adjudicate-unreviewed-evidence",
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
) -> str | None:
    """Compose the runnable invocation for one repair operation."""

    if operation == "hand_edit":
        return None
    parts = ["cruxible", _REPAIR_COMMAND_PATHS[operation]]
    values = arguments if isinstance(arguments, Mapping) else {}
    if operation == "playbill.block.repin":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
        else:
            return None
        claim_id = values.get("claim_id")
        if isinstance(claim_id, str) and claim_id:
            parts.extend(["--claim", shlex.quote(claim_id)])
    elif operation == "playbill.block.sync":
        if values.get("all") is True:
            parts.append("--all")
        else:
            return None
    elif operation == "playbill.claim.retire":
        claim_id = values.get("claim_id")
        if isinstance(claim_id, str):
            parts.append(shlex.quote(claim_id))
        parts.extend(_repair_operands(operation, values))
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
    command = _repair_command(repair.operation, arguments=repair.arguments)
    example = _ATTESTATION_REPAIR_EXAMPLES.get(repair.required_change)
    if example is not None and isinstance(repair.arguments, Mapping):
        claim_id = repair.arguments.get("claim_id")
        capture_digest = repair.arguments.get("capture_digest")
        if isinstance(claim_id, str) and isinstance(capture_digest, str):
            command = " ".join(
                (
                    "cruxible playbill authoring create --example",
                    shlex.quote(example),
                    "--claim-id",
                    shlex.quote(claim_id),
                    "--capture-digest",
                    shlex.quote(capture_digest),
                )
            )
    repair = repair.model_copy(update={"command": command})
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


def playbill_next_result_digest(result: PlaybillNextResultV1 | PlaybillNextResultV2) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    domain = (
        NEXT_RESULT_V2_DIGEST_DOMAIN
        if isinstance(result, PlaybillNextResultV2)
        else NEXT_RESULT_DIGEST_DOMAIN
    )
    if isinstance(result, PlaybillNextResultV2):
        payload.pop("delta_since")
        payload.pop("removed_item_ids", None)
    return typed_digest(Sha256Value, domain, payload).tagged


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
    law_evidence: Mapping[str, ClaimLawEvidenceAny],
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = (),
) -> tuple[PlaybillNextItemV1, ...]:
    """Emit v4 queue consequences from current independent attestation components."""

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
        evidence = law_evidence.get(claim_path(claim.identity.name))
        if evidence is None:
            raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
        current = current_verified_claim_attestations(
            tree,
            claim,
            evidence.verified_attestations,
        )
        exact_door = tuple(
            (event, payload)
            for event, payload in door_events
            if payload.attestation.statement.claim_identity == claim.identity
            and payload.attestation.statement.claim_artifact_digest
            == claim_artifact_digest(claim).tagged
            and payload.attestation.statement.attestation_basis == "examined_existing"
        )
        latest_door_by_principal: dict[
            str, tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]
        ] = {}
        for event, payload in exact_door:
            principal_id = payload.attesting_principal_id
            previous = latest_door_by_principal.get(principal_id)
            if previous is None or event.sequence > previous[0].sequence:
                latest_door_by_principal[principal_id] = (event, payload)
        superseded_legacy = frozenset(latest_door_by_principal)
        for rule in policy.rules:
            matching_legacy = tuple(
                item
                for item in current
                if item.current
                and item.attestation_grade == "verified_principal"
                and item.statement.provider_or_principal.kind == "Principal"
                and item.statement.claim_statement_digest
                == claim_statement_digest(claim.statement).tagged
                and item.statement.stance == rule.stance
                and item.statement.provider_or_principal.name not in superseded_legacy
                and item.statement.observed_at <= evaluation_time
                and (
                    item.statement.valid_until is None
                    or evaluation_time < item.statement.valid_until
                )
            )
            matching_door = tuple(
                payload
                for _event, payload in latest_door_by_principal.values()
                if payload.current_at_append
                and payload.attestation.statement.stance == rule.stance
                and payload.attestation.statement.attested_at <= evaluation_time
                and (
                    payload.attestation.statement.valid_until is None
                    or evaluation_time < payload.attestation.statement.valid_until
                )
            )
            principal_identities = frozenset(
                (
                    *(item.statement.provider_or_principal.name for item in matching_legacy),
                    *(item.attesting_principal_id for item in matching_door),
                )
            )
            if len(principal_identities) < rule.minimum_independent_control_components:
                continue
            attestation_digests = tuple(
                sorted(
                    (
                        *(item.attestation_digest for item in matching_legacy),
                        *(item.envelope_digest for item in matching_door),
                    ),
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
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = (),
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
    internal = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    law_evidence = _claim_law_evidence_index(instance, at=internal)
    items = list(
        _claim_attestation_threshold_items(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            claims=claims,
            law_evidence=law_evidence,
            door_events=door_events,
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


@dataclass(frozen=True)
class _CitationRelationUse:
    capture_digest: str
    citation_id: str
    claim_artifact_digest: str
    claim_identity: str
    lifecycle: Literal["live", "retired"]
    commitment_digest: str
    byte_length: int
    source: ExternalSourceReferenceV1
    original_start: int | None
    original_end: int | None

    @property
    def external_key(self) -> str:
        return external_source_relation_subject(self.source)


def _digest_value(value: object) -> str | None:
    if isinstance(value, str):
        try:
            Sha256Value.from_tagged(value)
        except ValueError:
            return None
        return value
    if isinstance(value, Mapping) and isinstance(value.get("$digest"), str):
        raw = cast(str, value["$digest"])
        try:
            Sha256Value.from_tagged(raw)
        except ValueError:
            return None
        return raw
    return None


def _relation_use(value: Mapping[str, object]) -> _CitationRelationUse:
    source = ExternalSourceReferenceV1.model_validate(value.get("source"))
    commitment = value.get("commitment")
    lifecycle = value.get("claim_lifecycle")
    if not isinstance(commitment, Mapping) or lifecycle not in {"live", "retired"}:
        raise ValueError("citation relation use has an invalid lifecycle or commitment")
    capture_digest = _digest_value(value.get("capture_digest"))
    claim_artifact_digest = _digest_value(value.get("claim_artifact_digest"))
    commitment_digest = _digest_value(commitment.get("digest"))
    byte_length = commitment.get("byte_length")
    citation_id = value.get("citation_id")
    claim_identity = value.get("claim_identity")
    if (
        capture_digest is None
        or claim_artifact_digest is None
        or commitment_digest is None
        or not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or not isinstance(citation_id, str)
        or not isinstance(claim_identity, str)
    ):
        raise ValueError("citation relation use is incomplete")
    selector = source.selector
    raw_window = (
        selector.get("working_selection", selector) if isinstance(selector, Mapping) else None
    )
    start = raw_window.get("start_byte") if isinstance(raw_window, Mapping) else None
    end = raw_window.get("end_byte") if isinstance(raw_window, Mapping) else None
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end
    ):
        start = end = None
    return _CitationRelationUse(
        capture_digest=capture_digest,
        citation_id=citation_id,
        claim_artifact_digest=claim_artifact_digest,
        claim_identity=claim_identity,
        lifecycle=cast(Literal["live", "retired"], lifecycle),
        commitment_digest=commitment_digest,
        byte_length=byte_length,
        source=source,
        original_start=start,
        original_end=end,
    )


def post_retirement_examined_support_suppresses_claim_cites_retired(
    claim_artifact_digest: str,
    *,
    claim_identity: str | None = None,
    retired_activation_sequence: int | None = None,
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = (),
    accepted_sequence_by_semantic_root: Mapping[str, int] | None = None,
) -> bool:
    """Suppress only after a current Claim was examined after the cited retirement."""

    if (
        claim_identity is None
        or retired_activation_sequence is None
        or accepted_sequence_by_semantic_root is None
    ):
        return False
    latest_by_principal: dict[
        str, tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]
    ] = {}
    for event, payload in door_events:
        statement = payload.attestation.statement
        if (
            statement.claim_identity.qualified != claim_identity
            or statement.claim_artifact_digest != claim_artifact_digest
        ):
            continue
        previous = latest_by_principal.get(payload.attesting_principal_id)
        if previous is None or event.sequence > previous[0].sequence:
            latest_by_principal[payload.attesting_principal_id] = (event, payload)
    return any(
        payload.current_at_append
        and payload.attestation.statement.attestation_basis == "examined_existing"
        and payload.attestation.statement.stance == "support"
        and (
            accepted_sequence_by_semantic_root.get(
                payload.attestation.statement.referent_coordinate.semantic_root,
                -1,
            )
            >= retired_activation_sequence
        )
        for _event, payload in latest_by_principal.values()
    )


def _claim_retirement_sequences(instance: PlaybillInstance) -> dict[str, int]:
    """Return the replay-proven activation sequence of every retired Claim."""

    retired: dict[str, int] = {}
    for generation in instance.accepted_history():
        record = getattr(generation, "record", None)
        if record is None:
            continue
        tree = instance.tree_at(generation.oid)
        for member in record.members:
            if member.artifact_kind != "claim" or member.path not in tree:
                continue
            claim = parse_claim(tree[member.path], path=member.path)
            if claim.lifecycle.state == "retired":
                retired[claim.identity.qualified] = generation.sequence
    return retired


def _complete_retirement_activation_sequence(
    witnesses: tuple[str, ...],
    *,
    retired_claim_count: int,
    retirement_sequences: Mapping[str, int],
) -> int | None:
    """Return the latest retirement only when display witnesses are complete."""

    if (
        not witnesses
        or retired_claim_count != len(witnesses)
        or any(witness not in retirement_sequences for witness in witnesses)
    ):
        return None
    return max(retirement_sequences[witness] for witness in witnesses)


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


def _claim_cites_retired_item(
    *,
    coordinate: PlaybillAcceptedCoordinate,
    live_claim_identity: str,
    live_claim_artifact_digest: str,
    relation_kind: str,
    live_citation_id: str,
    retired_claim_count: int,
    retired_citation_count: int,
    retired_claim_witnesses: tuple[str, ...],
    retired_activation_sequence: int | None = None,
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = (),
    accepted_sequence_by_semantic_root: Mapping[str, int] | None = None,
) -> PlaybillNextItemV1 | None:
    if post_retirement_examined_support_suppresses_claim_cites_retired(
        live_claim_artifact_digest,
        claim_identity=live_claim_identity,
        retired_activation_sequence=retired_activation_sequence,
        door_events=door_events,
        accepted_sequence_by_semantic_root=accepted_sequence_by_semantic_root,
    ):
        return None
    return _item(
        severity="warning",
        reason="claim_cites_retired",
        subject_identity=live_claim_identity,
        related_identities=retired_claim_witnesses,
        detail={
            "accepted_coordinate": coordinate.model_dump(mode="json"),
            "live_citation_id": live_citation_id,
            "relation_kind": relation_kind,
            "retired_citation_count": retired_citation_count,
            "retired_claim_count": retired_claim_count,
            "retired_claim_witnesses": list(retired_claim_witnesses),
        },
        repair=PlaybillNextRepairV1(
            operation="playbill.claim.retire",
            target=live_claim_identity,
            required_change="retire_or_replace_claim_citing_retired_evidence",
            arguments={
                "claim_id": live_claim_identity.removeprefix("Claim:"),
                "expected_coordinate": coordinate.model_dump(mode="json"),
            },
        ),
    )


def _unique_relation_occurrence(
    use: _CitationRelationUse,
    observed: PlaybillNextSourceObservationV4,
) -> WorkingOccurrenceV1 | None:
    expected_source = LogicalSourceIdentityV1(plane="external", identity=use.source.source_identity)
    if observed.scan_notes or observed.marker_notes:
        return None
    if not any(
        proof.source == expected_source
        and proof.commitment_digest == use.commitment_digest
        and proof.byte_length == use.byte_length
        for proof in observed.commitment_scan_proofs
    ):
        return None
    occurrences = tuple(
        occurrence
        for occurrence in observed.occurrences
        if occurrence.source == expected_source
        and occurrence.observed_commitment_digest == use.commitment_digest
        and occurrence.byte_length == use.byte_length
    )
    return occurrences[0] if len(occurrences) == 1 else None


def _citation_relation_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = (),
) -> tuple[tuple[PlaybillNextItemV1, ...], frozenset[tuple[str, str]]]:
    """Serve retirement relations from the immutable accepted-coordinate slice."""

    if not access_profile.permits("instance"):
        return (), frozenset()
    if not any(path.startswith("claims/") for path in instance.tree_at(coordinate.git_oid)):
        return (), frozenset()
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    retirement_sequences = _claim_retirement_sequences(instance) if door_events else {}
    accepted_sequence_by_semantic_root = (
        {
            generation.semantic_root.tagged: generation.sequence
            for generation in instance.accepted_history()
            if getattr(generation, "semantic_root", None) is not None
            and getattr(generation, "sequence", None) is not None
        }
        if door_events
        else {}
    )
    exact_by_claim: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    uses_by_source: dict[str, list[_CitationRelationUse]] = defaultdict(list)
    observed_sources = {
        item.source_id: item
        for item in (() if observation is None else observation.source_observations or ())
        if isinstance(item, PlaybillNextSourceObservationV4)
    }
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            for fact in projection.semantic_facts(
                RELATION_RETIRED_CONFLICT_SCHEMA,
                subject_identity="claim-cites-retired",
            ):
                if not isinstance(fact.value, Mapping):
                    raise ValueError("retired conflict has an invalid value")
                identity = fact.value.get("live_claim_identity")
                if not isinstance(identity, str):
                    raise ValueError("retired conflict has no live Claim")
                exact_by_claim[identity].append(fact.value)
            for source_id in sorted(observed_sources, key=lambda item: item.encode("utf-8")):
                for fact in projection.semantic_facts(
                    RELATION_SOURCE_USE_SCHEMA,
                    subject_identity=logical_source_relation_subject(source_id),
                ):
                    if not isinstance(fact.value, Mapping):
                        raise ValueError("citation relation use has an invalid value")
                    uses_by_source[source_id].append(_relation_use(fact.value))
    except (PlaybillError, ValueError, ValidationError) as exc:
        raise PlaybillNextAcceptedStateInvalid(
            f"{PlaybillNextAcceptedStateInvalid.code}: citation relation projection is invalid"
        ) from exc

    items: list[PlaybillNextItemV1] = []
    exact_subjects: set[str] = set()
    for live_identity in sorted(exact_by_claim, key=lambda item: item.encode("utf-8")):
        facts = exact_by_claim[live_identity]
        preferred = next(
            (
                matching
                for relation_kind in ("capture", "exact_external", "same_version_span")
                if (
                    matching := [
                        item for item in facts if item.get("relation_kind") == relation_kind
                    ]
                )
            ),
            facts,
        )
        raw_witnesses = tuple(
            witness
            for item in preferred
            for raw in (item.get("retired_claim_witnesses"),)
            if isinstance(raw, (list, tuple))
            for witness in raw
            if isinstance(witness, str)
        )
        witnesses = tuple(
            sorted(
                set(raw_witnesses),
                key=lambda item: item.encode("utf-8"),
            )[:8]
        )
        live_digest = _digest_value(preferred[0].get("live_claim_artifact_digest"))
        live_citation = preferred[0].get("live_citation_id")
        if live_digest is None or not isinstance(live_citation, str):
            raise PlaybillNextAcceptedStateInvalid(
                f"{PlaybillNextAcceptedStateInvalid.code}: retired conflict is incomplete"
            )
        retired_claim_count = sum(
            value
            for fact in preferred
            for value in (fact.get("retired_claim_count"),)
            if isinstance(value, int) and not isinstance(value, bool)
        )
        item = _claim_cites_retired_item(
            coordinate=public_coordinate,
            live_claim_identity=live_identity,
            live_claim_artifact_digest=live_digest,
            relation_kind=str(preferred[0].get("relation_kind")),
            live_citation_id=live_citation,
            retired_claim_count=retired_claim_count,
            retired_citation_count=sum(
                value
                for fact in preferred
                for value in (fact.get("retired_citation_count"),)
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            retired_claim_witnesses=witnesses,
            retired_activation_sequence=_complete_retirement_activation_sequence(
                witnesses,
                retired_claim_count=retired_claim_count,
                retirement_sequences=retirement_sequences,
            ),
            door_events=door_events,
            accepted_sequence_by_semantic_root=accepted_sequence_by_semantic_root,
        )
        if item is not None:
            items.append(item)
        exact_subjects.add(live_identity)

    suppressed_publications: set[tuple[str, str]] = set()
    for source_id in sorted(uses_by_source, key=lambda item: item.encode("utf-8")):
        observed = observed_sources[source_id]
        current: list[tuple[int, int, str, _CitationRelationUse, WorkingOccurrenceV1]] = []
        for use in uses_by_source[source_id]:
            if (
                use.source.coordinate_type != FOREIGN_SOURCE_COORDINATE_TYPE
                or use.source.selector_type != FOREIGN_SOURCE_SELECTOR_TYPE
            ):
                continue
            occurrence = _unique_relation_occurrence(use, observed)
            if (
                occurrence is None
                or occurrence.line_overlay.start_byte >= occurrence.line_overlay.end_byte
            ):
                continue
            current.append(
                (
                    occurrence.line_overlay.start_byte,
                    occurrence.line_overlay.end_byte,
                    use.lifecycle,
                    use,
                    occurrence,
                )
            )

        # An event sweep marks each live Claim at most once. Work is O(m_s log m_s + w_s),
        # never the live-by-retired Cartesian product.
        events: list[tuple[int, int, str, int, _CitationRelationUse]] = []
        for start, end, lifecycle, use, _occurrence in current:
            events.append((start, 1, lifecycle, end, use))
            events.append((end, 0, lifecycle, end, use))
        active_retired: dict[str, _CitationRelationUse] = {}
        active_live: Counter[str] = Counter()
        emitted_span: set[str] = set()

        def emit_span(live_use: _CitationRelationUse) -> None:
            if live_use.claim_identity in exact_subjects or live_use.claim_identity in emitted_span:
                return
            retired = tuple(active_retired.values())
            if not retired:
                return
            retired_claim_identities = {entry.claim_identity for entry in retired}
            witnesses = tuple(
                sorted(
                    retired_claim_identities,
                    key=lambda item: item.encode("utf-8"),
                )[:8]
            )
            row = _claim_cites_retired_item(
                coordinate=public_coordinate,
                live_claim_identity=live_use.claim_identity,
                live_claim_artifact_digest=live_use.claim_artifact_digest,
                relation_kind="current_span_overlap",
                live_citation_id=live_use.citation_id,
                retired_claim_count=len(retired_claim_identities),
                retired_citation_count=len(retired),
                retired_claim_witnesses=witnesses,
                retired_activation_sequence=_complete_retirement_activation_sequence(
                    witnesses,
                    retired_claim_count=len(retired_claim_identities),
                    retirement_sequences=retirement_sequences,
                ),
                door_events=door_events,
                accepted_sequence_by_semantic_root=accepted_sequence_by_semantic_root,
            )
            if row is not None:
                items.append(row)
            emitted_span.add(live_use.claim_identity)

        live_use_by_claim: dict[str, _CitationRelationUse] = {}
        for _position, order, lifecycle, _end, use in sorted(
            events,
            key=lambda event: (
                event[0],
                event[1],
                event[2].encode("ascii"),
                event[4].citation_id.encode("ascii"),
            ),
        ):
            if order == 0:
                if lifecycle == "retired":
                    active_retired.pop(use.citation_id, None)
                else:
                    active_live[use.claim_identity] -= 1
                    if active_live[use.claim_identity] <= 0:
                        active_live.pop(use.claim_identity, None)
                        live_use_by_claim.pop(use.claim_identity, None)
                continue
            if lifecycle == "retired":
                live_candidates = retired_activation_live_candidates(
                    active_retired,
                    live_use_by_claim,
                )
                active_retired[use.citation_id] = use
                for live_use in live_candidates:
                    emit_span(live_use)
            else:
                active_live[use.claim_identity] += 1
                live_use_by_claim[use.claim_identity] = use
                emit_span(use)

        document_id = observed.document_id
        if (
            document_id is None
            or instance.tree_at(coordinate.git_oid).get(document_path(document_id)) is None
        ):
            continue
        live_intervals = sorted(
            (start, end)
            for start, end, lifecycle, _use, _occurrence in current
            if lifecycle == "live"
        )
        live_union: list[tuple[int, int]] = []
        for start, end in live_intervals:
            if live_union and start <= live_union[-1][1]:
                live_union[-1] = (live_union[-1][0], max(live_union[-1][1], end))
            else:
                live_union.append((start, end))
        uncovered = [
            entry
            for entry in current
            if entry[2] == "retired"
            and not any(
                entry[0] < live_end and entry[1] > live_start for live_start, live_end in live_union
            )
        ]
        components: list[list[tuple[int, int, str, _CitationRelationUse, WorkingOccurrenceV1]]] = []
        for entry in sorted(
            uncovered,
            key=lambda item: (item[0], item[1], item[3].citation_id.encode("ascii")),
        ):
            if components and entry[0] <= max(item[1] for item in components[-1]):
                components[-1].append(entry)
            else:
                components.append([entry])
        for component in components:
            start = min(entry[0] for entry in component)
            end = max(entry[1] for entry in component)
            claims = {entry[3].claim_identity for entry in component}
            citations = {entry[3].citation_id for entry in component}
            occurrence_ids = {entry[4].identity_digest for entry in component}
            witnesses = tuple(sorted(claims, key=lambda item: item.encode("utf-8"))[:8])
            items.append(
                _item(
                    severity="warning",
                    reason="retired_claim_source_stale",
                    subject_identity=f"document:{document_id}",
                    related_identities=witnesses,
                    detail={
                        "document_id": document_id,
                        "end_byte": end,
                        "occurrence_identity_witnesses": sorted(
                            occurrence_ids, key=lambda item: item.encode("ascii")
                        )[:8],
                        "retired_citation_count": len(citations),
                        "retired_claim_count": len(claims),
                        "retired_claim_witnesses": list(witnesses),
                        "source_id": source_id,
                        "start_byte": start,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.document.propose",
                        target=f"document:{document_id}",
                        required_change="revise_retired_claim_source_span",
                        arguments={
                            "document_id": document_id,
                            "end_byte": end,
                            "source_id": source_id,
                            "start_byte": start,
                        },
                    ),
                )
            )
            suppressed_publications.update(
                (source_id, entry[3].commitment_digest) for entry in component
            )
    return tuple(items), frozenset(suppressed_publications)


def _self_published_source_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
    suppressed_relations: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[PlaybillNextItemV1, ...]:
    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    if observation.presentation_policy_notes:
        return ()
    policy = upgrade_playbill_presentation_policy(
        observation.presentation_policy or PlaybillPresentationPolicyV1()
    )
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
        if (source_id, commitment_digest) in suppressed_relations:
            continue
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


@dataclass(frozen=True)
class _AttestationLineageArtifact:
    claim: ClaimArtifactAny
    artifact_digest: str
    tree: dict[str, bytes]


def _attestation_claim_lineage(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim_identity: str,
) -> tuple[tuple[_AttestationLineageArtifact, ...], bool]:
    """Recover one Claim lineage at the request coordinate, oldest first."""

    path = claim_path(claim_identity)
    history = instance.accepted_history()
    target_index = next(
        index for index, item in enumerate(history) if item.oid == coordinate.git_oid
    )
    current_tree = instance.tree_at(coordinate.git_oid)
    raw = current_tree.get(path)
    if raw is None:
        return (), True
    current = parse_claim(raw, path=path)
    found = [
        _AttestationLineageArtifact(
            claim=current,
            artifact_digest=claim_artifact_digest(current).tagged,
            tree=current_tree,
        )
    ]
    expected = current.lifecycle.predecessor_digest
    scanned = history[max(0, target_index - MAX_DEPENDENCY_LINEAGE_GENERATIONS) : target_index]
    for generation in reversed(scanned):
        if expected is None:
            break
        tree = instance.tree_at(generation.oid)
        predecessor_raw = tree.get(path)
        if predecessor_raw is None:
            continue
        predecessor = parse_claim(predecessor_raw, path=path)
        digest = claim_artifact_digest(predecessor).tagged
        if digest != expected:
            continue
        found.append(
            _AttestationLineageArtifact(
                claim=predecessor,
                artifact_digest=digest,
                tree=tree,
            )
        )
        expected = predecessor.lifecycle.predecessor_digest
    return tuple(reversed(found)), expected is not None


def _attestation_resolving_accounts(
    instance: PlaybillInstance,
    *,
    lineage: tuple[_AttestationLineageArtifact, ...],
    law_by_artifact: Mapping[tuple[str, str], ClaimLawEvidenceAny],
) -> tuple[dict[str, tuple[CaptureAdmissionAccountV1, ...]], bool]:
    """Select immutable accounts under authored/rederivation authority law."""

    if not lineage:
        return {}, True
    path = claim_path(lineage[-1].claim.identity.name)
    authority_digest: str | None = None
    predecessor: _AttestationLineageArtifact | None = None
    accounts_by_authority: dict[str, tuple[CaptureAdmissionAccountV1, ...]] = {}
    artifact_accounts: dict[str, tuple[CaptureAdmissionAccountV1, ...]] = {}
    incomplete = False
    for artifact in lineage:
        mechanically_rederived = False
        if predecessor is not None:
            mechanically_rederived = _is_claim_type_rederivation(
                artifact.claim,
                predecessor=predecessor.claim,
                claim_type_digest=artifact.claim.statement.claim_type_digest,
                claim_type_identity=artifact.claim.statement.claim_type,
            )
        if authority_digest is None or not mechanically_rederived:
            authority_digest = artifact.artifact_digest
        if authority_digest not in accounts_by_authority:
            authority = next(item for item in lineage if item.artifact_digest == authority_digest)
            law = law_by_artifact.get((path, authority_digest))
            if law is None:
                accounts_by_authority[authority_digest] = ()
                incomplete = True
            else:
                try:
                    accounts_by_authority[authority_digest] = _claim_admission_accounts(
                        instance,
                        claim=authority.claim,
                        tree=authority.tree,
                        law=law,
                    )
                except (PlaybillError, ValueError):
                    accounts_by_authority[authority_digest] = ()
                    incomplete = True
        artifact_accounts[artifact.artifact_digest] = accounts_by_authority[authority_digest]
        predecessor = artifact
    return artifact_accounts, incomplete


def _claim_attestation_door_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...],
) -> tuple[PlaybillNextItemV1, ...]:
    """Fold new-capture memberships against immutable acceptance-time accounts."""

    law_by_artifact = _claim_law_evidence_by_artifact_index(instance, at=coordinate)
    lineage_cache: dict[str, tuple[tuple[_AttestationLineageArtifact, ...], bool]] = {}
    account_cache: dict[str, tuple[dict[str, tuple[CaptureAdmissionAccountV1, ...]], bool]] = {}
    items: list[PlaybillNextItemV1] = []
    reason_by_stance: Mapping[str, tuple[NextReason, NextSeverity, str]] = {
        "contradict": (
            "claim_contradicting_evidence_available",
            "repair",
            "adjudicate_contradicting_evidence",
        ),
        "support": (
            "claim_new_evidence_supporting",
            "warning",
            "cite_supporting_evidence",
        ),
        "unsure": (
            "claim_new_evidence_unreviewed",
            "warning",
            "adjudicate_unreviewed_evidence",
        ),
    }
    for event, payload in door_events:
        statement = payload.attestation.statement
        if statement.attestation_basis != "new_capture":
            continue
        claim_id = statement.claim_identity.name
        cached_lineage = lineage_cache.get(claim_id)
        if cached_lineage is None:
            cached_lineage = _attestation_claim_lineage(
                instance,
                coordinate=coordinate,
                claim_identity=claim_id,
            )
            lineage_cache[claim_id] = cached_lineage
        lineage, lineage_incomplete = cached_lineage
        cached_accounts = account_cache.get(claim_id)
        if cached_accounts is None:
            cached_accounts = _attestation_resolving_accounts(
                instance,
                lineage=lineage,
                law_by_artifact=law_by_artifact,
            )
            account_cache[claim_id] = cached_accounts
        accounts_by_artifact, account_incomplete = cached_accounts
        lineage_digests = {item.artifact_digest for item in lineage}
        membership_proven = statement.claim_artifact_digest in lineage_digests
        terminal = bool(
            membership_proven
            and lineage
            and isinstance(lineage[-1].claim, ClaimArtifactV3)
            and lineage[-1].claim.lifecycle.state == "retired"
        )
        for capture_digest in statement.cited_capture_digests:
            resolved = terminal or any(
                any(
                    account.capture_digest == capture_digest and account.status == "admitted"
                    for account in accounts
                )
                for accounts in accounts_by_artifact.values()
            )
            if resolved:
                continue
            lineage_status = (
                "incomplete"
                if lineage_incomplete or account_incomplete or not membership_proven
                else "proven"
            )
            reason, severity, required_change = reason_by_stance[statement.stance]
            items.append(
                _item(
                    severity=severity,
                    reason=reason,
                    subject_identity=statement.claim_identity.qualified,
                    related_identities=tuple(
                        sorted(
                            (
                                f"Capture:{capture_digest}",
                                f"Principal:{statement.attesting_principal_id}",
                            ),
                            key=lambda item: item.encode("utf-8"),
                        )
                    ),
                    detail={
                        "claim_id": claim_id,
                        "claim_artifact_digest": statement.claim_artifact_digest,
                        "capture_digest": capture_digest,
                        "attestation_event_digest": event.event_digest,
                        "attestation_basis": statement.attestation_basis,
                        "stance": statement.stance,
                        "attesting_principal": statement.attesting_principal_id,
                        "current_at_append": payload.current_at_append,
                        "lineage_status": lineage_status,
                    },
                    repair=PlaybillNextRepairV1(
                        operation="playbill.authoring.create",
                        target=statement.claim_identity.qualified,
                        required_change=required_change,
                        arguments={
                            "claim_id": claim_id,
                            "capture_digest": capture_digest,
                        },
                    ),
                )
            )
    return tuple(items)


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
    projection = observation.projection_coverage
    if (
        projection is not None
        and access_profile.permits("instance")
        and projection.coordinate.model_dump(mode="json")
        == AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json")).model_dump(
            mode="json"
        )
        and "workspace_projections" not in domains
    ):
        domains.append("workspace_projections")
    return tuple(domains), tuple(items)


def _procedure_projection_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Advise on live Procedures absent from one complete local catalog observation."""

    if (
        observation is None
        or observation.projection_coverage is None
        or observation.presentation_policy_notes
        or not access_profile.permits("instance")
    ):
        return ()
    coverage = observation.projection_coverage
    if coverage.coordinate.model_dump(mode="json") != PlaybillAcceptedCoordinate.from_internal(
        coordinate
    ).model_dump(mode="json"):
        return ()
    policy = upgrade_playbill_presentation_policy(
        observation.presentation_policy or PlaybillPresentationPolicyV1()
    )
    if not policy.projection_advisories.procedure or "Procedure" not in coverage.complete_kinds:
        return ()
    covered = {
        item.artifact.qualified for item in coverage.bindings if item.artifact.kind == "Procedure"
    }
    missing: list[tuple[str, dict[str, object]]] = []
    tree = instance.tree_at(coordinate.git_oid)
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("procedures/") or not path.endswith(".json"):
            continue
        procedure = parse_procedure(tree[path], path=path)
        if procedure.lifecycle.state != "live" or procedure.identity.qualified in covered:
            continue
        catalog_entry: dict[str, object] = {
            "kind": "procedure",
            "procedure_identity": procedure.identity.model_dump(mode="json"),
            "locator": f"procedures/{procedure.identity.name}.md",
        }
        missing.append((procedure.identity.qualified, catalog_entry))
    if not missing:
        return ()
    missing.sort(key=lambda item: item[0].encode("utf-8"))
    identities = tuple(item[0] for item in missing)
    entries = [item[1] for item in missing]
    return (
        _item(
            severity="warning",
            reason="procedure_projection_missing",
            subject_identity=".playbill/sources.yaml",
            related_identities=identities,
            detail={
                "unprojected_procedure_ids": list(identities),
                "catalog_entries": entries,
                "message": "accepted Procedures have no configured workspace projection",
            },
            repair=PlaybillNextRepairV1(
                operation="hand_edit",
                target=".playbill/sources.yaml",
                required_change="add_procedure_projection_catalog_entries",
                arguments={"catalog_entries": entries},
            ),
        ),
    )


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


def _registered_publication_blocks(
    instance: PlaybillInstance,
) -> frozenset[tuple[str, str]] | None:
    """Fold durable bound publication identities from latest intent events."""

    registrations = bound_publication_registrations(instance)
    if registrations is None:
        return None
    return frozenset(
        (item.preparation.source_id, item.preparation.block_id) for item in registrations
    )


def _projection_marker_invalid_item(
    *,
    source_id: str,
    block_id: str | None,
    marker_status: Literal["invalid", "registered_marker_missing"],
) -> PlaybillNextItemV1:
    target = f"{source_id}#{block_id}" if block_id is not None else source_id
    detail: dict[str, str] = {
        "source_id": source_id,
        "error_code": "playbill.projection.marker_invalid",
        "marker_status": marker_status,
    }
    arguments = {"source_id": source_id}
    if block_id is not None:
        detail["block_id"] = block_id
        arguments["block_id"] = block_id
    return _item(
        severity="blocking",
        reason="projection_marker_invalid",
        subject_identity=target,
        related_identities=(),
        detail=detail,
        repair=PlaybillNextRepairV1(
            operation="playbill.block.repin",
            target=target,
            required_change="restore_projection_frame_then_repin",
            arguments=arguments,
        ),
    )


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
    observed_sources = tuple(
        source
        for source in observation.source_observations
        if isinstance(
            source,
            (
                PlaybillNextSourceObservationV3,
                PlaybillNextSourceObservationV4,
            ),
        )
    )
    if not observed_sources:
        return ()

    registrations = _registered_publication_blocks(instance)
    items: list[PlaybillNextItemV1] = []
    for source in observed_sources:
        observed_block_ids = {marker.stamp.block_id for marker in source.marker_summaries}
        registered_block_ids = (
            {block_id for source_id, block_id in registrations if source_id == source.source_id}
            if registrations is not None
            else set()
        )
        missing_block_ids = registered_block_ids - observed_block_ids
        marker_invalid = bool(source.marker_notes)
        if marker_invalid and not missing_block_ids:
            items.append(
                _projection_marker_invalid_item(
                    source_id=source.source_id,
                    block_id=None,
                    marker_status="invalid",
                )
            )
        for block_id in sorted(missing_block_ids, key=lambda value: value.encode("utf-8")):
            items.append(
                _projection_marker_invalid_item(
                    source_id=source.source_id,
                    block_id=block_id,
                    marker_status=("invalid" if marker_invalid else "registered_marker_missing"),
                )
            )

    sources = tuple(source for source in observed_sources if source.marker_summaries)
    if not sources:
        return tuple(items)

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
    for source in sources:
        for marker in source.marker_summaries:
            registration = (source.source_id, marker.stamp.block_id)
            if (
                marker.stamp.block_id.startswith("pub-")
                and registrations is not None
                and registration not in registrations
            ):
                target = f"{source.source_id}#{marker.stamp.block_id}"
                items.append(
                    _item(
                        severity="warning",
                        reason="unregistered_projection_block",
                        subject_identity=target,
                        related_identities=(),
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.document.propose",
                            target=target,
                            required_change="remove_or_register_projection_block",
                            arguments={
                                "source_id": source.source_id,
                                "block_id": marker.stamp.block_id,
                            },
                        ),
                    )
                )
            visible = True
            stale: list[str] = []
            body_digest_detail: dict[str, str] = {}
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
                elif isinstance(backing, ProjectionArtifactBackingV1):
                    try:
                        if backing.identity.kind == "ClaimType":
                            path = claim_type_path(backing.identity.name)
                            raw = tree[path]
                            claim_type = parse_claim_type(raw, path=path)
                            artifact_identity = claim_type.identity
                            digest = claim_type_digest(claim_type).tagged
                        else:
                            subject_kind, separator, subject_id = backing.identity.name.partition(
                                "/"
                            )
                            if not separator:
                                raise ValueError("Subject identity has no kind separator")
                            path = subject_path(subject_kind, subject_id)
                            raw = tree[path]
                            subject = parse_subject(raw, path=path)
                            artifact_identity = subject.identity
                            digest = subject_digest(subject).tagged
                    except (KeyError, PlaybillError, ValueError):
                        visible = False
                        break
                    if artifact_identity != backing.identity:
                        visible = False
                        break
                    if digest != backing.artifact_digest:
                        stale.append(backing.identity.qualified)

            if not visible:
                continue
            target = f"{source.source_id}#{marker.stamp.block_id}"
            syncable = (
                (registration in registrations if registrations is not None else False)
                and len(marker.stamp.backing) == 1
                and isinstance(
                    marker.stamp.backing[0],
                    (ProjectionClaimBackingV1, ProjectionArtifactBackingV1),
                )
            )
            if syncable:
                claim_backing = marker.stamp.backing[0]
                try:
                    sync_read = service_read_playbill_block_sync_backing(
                        instance,
                        request=PlaybillBlockSyncReadRequestV1(stamp=marker.stamp),
                    )
                except PlaybillError as exc:
                    items.append(
                        _item(
                            severity="blocking",
                            reason="projection_marker_invalid",
                            subject_identity=target,
                            related_identities=(claim_backing.identity.qualified,),
                            detail={
                                "source_id": source.source_id,
                                "block_id": marker.stamp.block_id,
                                "error_code": "playbill.projection.backing_lineage_unreadable",
                                "message": str(exc),
                            },
                            repair=PlaybillNextRepairV1(
                                operation="playbill.block.repin",
                                target=target,
                                required_change="repin_explicit_current_claim_backing",
                                arguments={
                                    "source_id": source.source_id,
                                    "block_id": marker.stamp.block_id,
                                    "claim_id": claim_backing.identity.name,
                                },
                            ),
                        )
                    )
                    syncable = False
                else:
                    terminal_body_digest = sync_read.body_digest
                    if (
                        sync_read.status in {"current", "successor"}
                        and terminal_body_digest is not None
                        and terminal_body_digest != marker.stamp.body_digest
                    ):
                        identity = claim_backing.identity.qualified
                        if identity not in stale:
                            stale.append(identity)
                        body_digest_detail = {
                            "stamped_body_digest": marker.stamp.body_digest,
                            "terminal_body_digest": terminal_body_digest,
                        }
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
                            **body_digest_detail,
                        },
                        repair=PlaybillNextRepairV1(
                            operation=(
                                "playbill.block.sync" if syncable else "playbill.block.repin"
                            ),
                            target=target,
                            required_change=(
                                "sync_unique_publication_successor"
                                if syncable
                                else "review_block_supersede_prose_then_repin"
                            ),
                            arguments={"all": True} if syncable else arguments,
                        ),
                    )
                )
    return tuple(items)


def service_playbill_next(
    instance: PlaybillInstance,
    *,
    request: PlaybillNextRequestAny,
    provider_lane: ProviderLaneStatusV1 | None = None,
) -> PlaybillNextResultV1 | PlaybillNextResultV2:
    """Fold accepted state and explicit client observations into one repair queue."""

    coordinate = _resolve_coordinate(instance, request.at)
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    attestation_head: str | None = None
    door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...] = ()
    if isinstance(request, PlaybillNextRequestV2):
        store = instance.claim_attestation_evidence_store()
        attestation_head = request.at_attestation_head_digest or store.head()
        door_events = store.fold_events(at_head=attestation_head)
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
    relation_items, suppressed_publications = _citation_relation_items(
        instance,
        coordinate=coordinate,
        access_profile=request.access_profile,
        observation=request.workspace_observation,
        door_events=door_events,
    )
    items = tuple(
        sorted(
            (
                *_claim_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    expiring_within=request.expiring_within,
                    door_events=door_events,
                ),
                *(
                    _claim_attestation_door_items(
                        instance,
                        coordinate=coordinate,
                        door_events=door_events,
                    )
                    if isinstance(request, PlaybillNextRequestV2)
                    else ()
                ),
                *workspace_items,
                *_projection_items(
                    instance,
                    coordinate=coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
                *_procedure_projection_items(
                    instance,
                    coordinate=coordinate,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                ),
                *relation_items,
                *_self_published_source_items(
                    instance,
                    coordinate=public_coordinate,
                    evaluation_time=request.evaluation_time,
                    access_profile=request.access_profile,
                    observation=request.workspace_observation,
                    suppressed_relations=suppressed_publications,
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
                *(
                    (
                        _item(
                            severity="warning",
                            reason="provider_lane_unavailable",
                            subject_identity="provider-runtime",
                            detail={
                                "code": provider_lane.code,
                                "detail": provider_lane.detail,
                            },
                            repair=PlaybillNextRepairV1(
                                operation="hand_edit",
                                target="daemon/provider-runtime.json",
                                required_change=(
                                    "repair_provider_runtime_configuration_or_use_a_shorter_"
                                    "state_root_then_retry"
                                ),
                            ),
                        ),
                    )
                    if provider_lane is not None and provider_lane.state == "unavailable"
                    else ()
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
    result_model: type[PlaybillNextResultV1] | type[PlaybillNextResultV2]
    if isinstance(request, PlaybillNextRequestV2):
        assert attestation_head is not None
        result_model = PlaybillNextResultV2
        values["attestation_head_digest"] = attestation_head
    else:
        result_model = PlaybillNextResultV1
    provisional = result_model.model_construct(
        _fields_set=None,
        result_digest="sha256:" + "0" * 64,
        **values,
    )
    result_digest = playbill_next_result_digest(provisional)
    full = result_model.model_validate({**values, "result_digest": result_digest})
    _remember_queue(result_digest, full.items)
    if request.since_result_digest is None:
        return full
    return _delta_of(full, since=request.since_result_digest)


# Bounded, per-process memory of which rows each queue digest stood for. A miss
# -- restart, eviction, a digest minted elsewhere -- is not an error: it yields
# the whole queue, which answers the caller's question either way.
_QUEUE_MEMO: OrderedDict[str, tuple[PlaybillNextItemV1, ...]] = OrderedDict()
_QUEUE_MEMO_LIMIT = 32
_QUEUE_MEMO_LOCK = RLock()


def _remember_queue(result_digest: str, items: tuple[PlaybillNextItemV1, ...]) -> None:
    with _QUEUE_MEMO_LOCK:
        _QUEUE_MEMO.pop(result_digest, None)
        _QUEUE_MEMO[result_digest] = items
        while len(_QUEUE_MEMO) > _QUEUE_MEMO_LIMIT:
            _QUEUE_MEMO.popitem(last=False)


def _delta_of(
    full: PlaybillNextResultV1 | PlaybillNextResultV2,
    *,
    since: str,
) -> PlaybillNextResultV1 | PlaybillNextResultV2:
    """Return the reproducible symmetric difference from a remembered queue."""

    with _QUEUE_MEMO_LOCK:
        previous = _QUEUE_MEMO.get(since)
    if previous is None:
        return full
    previous_by_id = {item.item_id: item for item in previous}
    current_ids = frozenset(item.item_id for item in full.items)
    previous_ids = frozenset(previous_by_id)
    changed = tuple(
        sorted(
            (
                *(item for item in full.items if item.item_id not in previous_ids),
                *(previous_by_id[item_id] for item_id in previous_ids - current_ids),
            ),
            key=_item_sort_key,
        )
    )
    # The digest is the whole-queue cursor, not a digest of the displayed
    # symmetric-difference subset. That base invariant keeps a repeated delta
    # request idempotent and prevents a subset from overwriting the full queue
    # in the per-process memo (delta_since is intentionally outside v2's
    # accepted-state digest preimage).
    update: dict[str, object] = {"items": changed, "delta_since": since}
    if isinstance(full, PlaybillNextResultV2):
        update["removed_item_ids"] = tuple(
            sorted(previous_ids - current_ids, key=lambda item: item.encode("ascii"))
        )
    return full.model_copy(update=update)


__all__ = [
    "DEFAULT_EXPIRING_WITHIN_MICROSECONDS",
    "NEXT_ITEM_ID_DOMAIN",
    "NEXT_RESULT_DIGEST_DOMAIN",
    "NEXT_RESULT_V2_DIGEST_DOMAIN",
    "PlaybillNextAccessProfileInvalid",
    "PlaybillNextAcceptedStateInvalid",
    "PlaybillNextCoordinateNotAccepted",
    "PlaybillNextDriftObservationV1",
    "PlaybillNextItemV1",
    "PlaybillNextRequestV1",
    "PlaybillNextRequestV2",
    "PlaybillNextResultV1",
    "PlaybillNextResultV2",
    "PlaybillNextSourceObservationV3",
    "PlaybillNextSourceObservationV4",
    "PlaybillNextWorkspaceObservationInvalid",
    "PlaybillNextWorkspaceObservationV1",
    "playbill_next_item_id",
    "playbill_next_result_digest",
    "service_playbill_next",
    "validate_playbill_next_request",
]
