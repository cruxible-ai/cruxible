"""Deterministic repair queue derived from one accepted Playbill coordinate."""

from __future__ import annotations

import shlex
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts import (
    PlaybillNextReason,
    ProviderLaneStatusV1,
)
from cruxible_client.contracts.accepted_attestations import AcceptedClaimAttestationEvidenceV1
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.authoring.models import PlaybillBlockSyncReadRequestV1
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
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEventPayloadV1,
    ClaimAttestationEventV1,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatementV2,
    ClaimAttestationV2,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResultAny,
    ClaimVerdictResultV2,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV3,
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
    MAX_PROJECTION_CARDS_PER_SOURCE,
    PlaybillPresentationPolicyAny,
    PlaybillPresentationPolicyNoteV1,
    PlaybillPresentationPolicyV1,
    PlaybillProjectionCoverageObservationV1,
    ProjectionMarkerSummaryV1,
    upgrade_playbill_presentation_policy,
)
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.claims.claim_slots import classify_claim_slot
from cruxible_core.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCommitmentScanProofV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.coverage.indexes import (
    WorkingOccurrenceV1,
)
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.query.backends import claim_row_visibility
from cruxible_core.query.impact import (
    SOURCE_CONTRADICTED,
    SOURCE_SUPERSEDED,
    DependencyImpactRequestV1,
    build_dependency_impact,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.authoring.projection_sync import (
    PROJECTION_VISIBILITY_POLICY,
    ProjectionCheckContext,
)
from cruxible_core.service.claims.claims import (
    CaptureAdmissionAccountV1,
    _claim_admission_accounts,
    _claim_from_view,
    _claim_law_evidence,
    _claim_law_evidence_by_artifact_index,
    _claim_law_evidence_index,
    service_list_playbill_claims,
)
from cruxible_core.service.discovery.query import (
    _AcceptedQueryFactsRead,
    build_accepted_query_facts,
)
from cruxible_core.service.discovery.search import claim_resolution_statuses
from cruxible_core.service.evidence.evidence import (
    ClaimVerdictReadContext,
    accepted_claim_attestations,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.proposals.proposals import stale_unreadmitted_proposals
from cruxible_core.service.proposals.publications import (
    ProjectionBlockRegistration,
    registered_projection_blocks,
)
from cruxible_core.storage.cas import BodyAccessContext

NEXT_ITEM_ID_DOMAIN = "playbill-next-item-v1"
NEXT_RESULT_DIGEST_DOMAIN = "playbill-next-result-v1"
NEXT_RESULT_V2_DIGEST_DOMAIN = "playbill-next-result-v2"
DEFAULT_EXPIRING_WITHIN_MICROSECONDS = 604_800_000_000
MAX_DEPENDENCY_LINEAGE_NODES = 4096

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
NextRepairOperation = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim.retire",
    "playbill.floor.export",
    "playbill.block.depublish",
    "playbill.block.repin",
    "playbill.block.sync",
    "playbill.document.propose",
    "playbill.proposal.readmit",
    "hand_edit",
]

# A citation reads unobserved for one of two kinds of reason. Either the
# citation itself is no longer where the source says it is - a finding about
# that citation, repaired one citation at a time - or the source's own coverage
# scan never produced the evidence the gate consults, in which case every
# citation to that source reads unobserved whatever the citation says and the
# repair is to the scan, not to any citation.
#
# These are the notes an observation raises for the second kind: the span never
# arrived, or arrived reporting itself less than complete, or a per-source count
# cap discarded a whole class of its evidence. Anything else - an individual
# proof, window, card or occurrence the observation dropped - is not
# whole-source and is deliberately absent, because collapsing on it would put a
# cause in the served row that the row cannot prove.
_COVERAGE_SOURCE_SCAN_DEFECT_NOTES = frozenset(
    {
        # the span never arrived, or does not answer this read
        "coverage_access_mismatch",
        "coverage_coordinate_mismatch",
        "coverage_result_version_unsupported",
        "coverage_span_ambiguous",
        "coverage_span_missing",
        # the span arrived reporting itself less than complete
        "coverage_denied",
        "coverage_partial",
        "coverage_stale",
        "coverage_unavailable",
        # a per-source count cap discarded a whole class of the evidence
        "coverage_card_limit_exceeded",
        "coverage_proof_limit_exceeded",
        "coverage_window_limit_exceeded",
    }
)
_SEVERITY_RANK: dict[NextSeverity, int] = {"blocking": 0, "repair": 1, "warning": 2}
_ALL_DOMAINS: tuple[NextDomain, ...] = (
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
    "workspace_projections",
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
    byte_length: int = Field(ge=0)
    marker_summaries: tuple[ProjectionMarkerSummaryV1, ...] = Field()
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
    byte_length: int = Field(ge=0)
    marker_summaries: tuple[ProjectionMarkerSummaryV1, ...] = Field()
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


class PlaybillNextFindingV1(_StrictNextModel):
    """One more finding about the same underlying fact as the row that carries it."""

    tag: Literal["playbill-next-finding-v1"] = "playbill-next-finding-v1"
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: tuple[str, ...] = ()
    detail: object = Field(default_factory=dict)
    repair: PlaybillNextRepairV1

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
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
    # The row's other findings about the same block, source, document,
    # evidence or conflicted slot, each keeping its own reason, detail and
    # repair. Absent on a row that stands alone, so its bytes do not change.
    findings: tuple[PlaybillNextFindingV1, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

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


#: The states each environment facet of the queue's status header can report.
_HEALTH_STATES: dict[str, frozenset[str]] = {
    "instance": frozenset({"active", "decommissioned"}),
    "floor": frozenset(
        {"not_observed", "not_configured", "current", "missing", "stale", "invalid"}
    ),
    "ledger_mirror": frozenset(
        {"not_configured", "current", "publishing", "behind", "never_published"}
    ),
    "provider_lane": frozenset({"not_reported", "available", "unavailable"}),
    "procedure_catalog": frozenset({"not_observed", "not_required", "complete", "missing"}),
}
#: Facet states that call for attention; every other state is healthy or unobserved.
_HEALTH_ATTENTION: dict[str, frozenset[str]] = {
    "instance": frozenset({"decommissioned"}),
    "floor": frozenset({"missing", "stale"}),
    "ledger_mirror": frozenset({"behind", "never_published"}),
    "provider_lane": frozenset({"unavailable"}),
    "procedure_catalog": frozenset({"missing"}),
}


class PlaybillNextHealthV1(_StrictNextModel):
    """One environment facet: its state, what it saw, and the repair if it needs one."""

    tag: Literal["playbill-next-health-v1"] = "playbill-next-health-v1"
    state: str
    detail: object = Field(default_factory=dict)
    repair: PlaybillNextRepairV1 | None = None

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class PlaybillNextStatusV1(_StrictNextModel):
    """The environment the queue was read in, beside the work rather than in it.

    These are conditions of the instance and its workspace -- a decommissioned
    instance, an unexported floor, a lagging ledger mirror, an unavailable
    provider lane, an incomplete Procedure catalog -- not work items about
    accepted state. `blocking` is set only when no write can succeed.
    """

    tag: Literal["playbill-next-status-v1"] = "playbill-next-status-v1"
    blocking: bool
    instance: PlaybillNextHealthV1
    floor: PlaybillNextHealthV1
    ledger_mirror: PlaybillNextHealthV1
    provider_lane: PlaybillNextHealthV1
    procedure_catalog: PlaybillNextHealthV1
    #: Rows parked by a current ``unsure`` attestation whose basis is unchanged.
    held: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _states(self) -> "PlaybillNextStatusV1":
        for facet, states in _HEALTH_STATES.items():
            if getattr(self, facet).state not in states:
                raise ValueError(f"next {facet} status has an unknown state")
        if self.blocking != (self.instance.state == "decommissioned"):
            raise ValueError("next status blocks exactly a decommissioned instance")
        return self

    def attention(self) -> tuple[tuple[str, PlaybillNextHealthV1], ...]:
        """The facets calling for attention, in a fixed order."""

        return tuple(
            (facet, getattr(self, facet))
            for facet in _HEALTH_STATES
            if getattr(self, facet).state in _HEALTH_ATTENTION[facet]
        )


class PlaybillNextResultV1(_StrictNextModel):
    tag: Literal["playbill-next-result-v1"] = "playbill-next-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    observed_domains: tuple[NextDomain, ...]
    unobserved_domains: tuple[NextDomain, ...]
    status: PlaybillNextStatusV1
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
    "playbill.block.depublish": "playbill block depublish",
    "playbill.block.repin": "playbill block repin",
    "playbill.block.sync": "playbill block sync",
    "playbill.document.propose": "playbill document propose",
    "playbill.proposal.readmit": "playbill proposal readmit",
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
        claims = values.get("claim")
        if isinstance(claims, (list, tuple)):
            for value in claims:
                if isinstance(value, str) and value:
                    parts.extend(["--claim", shlex.quote(value.removeprefix("Claim:"))])
    elif operation == "playbill.block.depublish":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
        else:
            return None
    elif operation == "playbill.block.sync":
        if values.get("all") is True:
            parts.append("--all")
        else:
            return None
    elif operation == "playbill.proposal.readmit":
        proposal_id = values.get("proposal_id")
        if not isinstance(proposal_id, str):
            return None
        parts.append(shlex.quote(proposal_id))
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


#: Rows about the same underlying fact that the queue reports as one row.
_BLOCK_REASONS = frozenset(
    {
        "projection_backing_stale",
        "projection_dirty",
        "unregistered_projection_block",
        "projection_marker_invalid",
    }
)
#: Stances heading a new-evidence row, strongest first.
_EVIDENCE_REASON_ORDER: tuple[NextReason, ...] = (
    "claim_contradicting_evidence_available",
    "claim_new_evidence_unreviewed",
    "claim_new_evidence_supporting",
)


#: Rows a supporting capture can resolve, in the order one is chosen to carry it.
_SUPPORT_RESOLVES: tuple[NextReason, ...] = (
    "evidence_expiring",
    "claim_uncovered",
    "claim_attestation_threshold_met",
)


def _detail_value(item: PlaybillNextItemV1, key: str) -> str | None:
    value = item.detail.get(key) if isinstance(item.detail, Mapping) else None
    return value if isinstance(value, str) else None


def _group_key(
    item: PlaybillNextItemV1, *, edited_sources: frozenset[str]
) -> tuple[str, ...] | None:
    if item.reason in _BLOCK_REASONS:
        return ("block", item.subject_identity)
    if item.reason in _EVIDENCE_REASON_ORDER:
        capture = _detail_value(item, "capture_digest")
        return None if capture is None else ("evidence", item.subject_identity, capture)
    source_id = _detail_value(item, "source_id")
    if source_id is None:
        return None
    if item.reason == "citation_source_unobserved":
        return ("unobserved-source", source_id)
    if item.reason == "document_modified" or (
        item.reason == "citation_drifted" and source_id in edited_sources
    ):
        return ("edited-document", source_id)
    return None


def _group_head(key: tuple[str, ...], members: list[PlaybillNextItemV1]) -> PlaybillNextItemV1:
    if key[0] == "edited-document":
        return next(item for item in members if item.reason == "document_modified")
    if key[0] == "evidence":
        return min(members, key=lambda item: (_EVIDENCE_REASON_ORDER.index(item.reason),))
    return min(members, key=_item_sort_key)


def _group_items(items: tuple[PlaybillNextItemV1, ...]) -> tuple[PlaybillNextItemV1, ...]:
    """Report each underlying fact once, with every finding about it inside.

    A block is one row whatever made it stale, dirty or unregistered; an
    unobserved source is one row however many citations point at it; an
    edited document carries the citations its edit moved; one captured piece
    of new evidence is one row whoever attested to it. The head keeps its own
    reason and repair at the group's highest severity; the rest ride along.
    """

    items, supporting = _fold_supporting(items)
    edited_sources = frozenset(
        source
        for item in items
        if item.reason == "document_modified"
        and (source := _detail_value(item, "source_id")) is not None
    )
    grouped: dict[tuple[str, ...], list[PlaybillNextItemV1]] = defaultdict(list)
    singles: list[PlaybillNextItemV1] = []
    for item in items:
        if item.reason in _SUPPORT_RESOLVES and item.subject_identity in supporting:
            # The first row the capture would resolve carries it; later ones don't.
            singles.append(_with_findings(item, supporting.pop(item.subject_identity)))
            continue
        key = _group_key(item, edited_sources=edited_sources)
        if key is None:
            singles.append(item)
        else:
            grouped[key].append(item)
    for key, members in grouped.items():
        if len(members) == 1:
            singles.append(members[0])
            continue
        head = _group_head(key, members)
        rest = sorted((item for item in members if item is not head), key=_item_sort_key)
        singles.append(_with_findings(head, rest))
    return tuple(singles)


def _fold_supporting(
    items: tuple[PlaybillNextItemV1, ...],
) -> tuple[tuple[PlaybillNextItemV1, ...], dict[str, list[PlaybillNextItemV1]]]:
    """Take supporting captures out of the queue unless they bear on work in it.

    Supporting evidence is not work. It stays beside a contradicting or
    unreviewed stance on the same capture, rides inside the expiring,
    uncovered or threshold row it would resolve for the same Claim, and is
    otherwise silent.
    """

    contested = {
        (item.subject_identity, _detail_value(item, "capture_digest"))
        for item in items
        if item.reason in _EVIDENCE_REASON_ORDER and item.reason != "claim_new_evidence_supporting"
    }
    resolvable = {item.subject_identity for item in items if item.reason in _SUPPORT_RESOLVES}
    kept: list[PlaybillNextItemV1] = []
    folded: dict[str, list[PlaybillNextItemV1]] = defaultdict(list)
    # Resolvable rows first, in carrier order, so the first one reached carries.
    carrier_order = {reason: rank for rank, reason in enumerate(_SUPPORT_RESOLVES)}
    for item in sorted(items, key=lambda item: carrier_order.get(item.reason, len(carrier_order))):
        if item.reason != "claim_new_evidence_supporting":
            kept.append(item)
        elif (item.subject_identity, _detail_value(item, "capture_digest")) in contested:
            kept.append(item)
        elif item.subject_identity in resolvable:
            folded[item.subject_identity].append(item)
    for members in folded.values():
        members.sort(key=_item_sort_key)
    return tuple(kept), dict(folded)


def _with_findings(
    head: PlaybillNextItemV1, rest: Iterable[PlaybillNextItemV1]
) -> PlaybillNextItemV1:
    rest = tuple(rest)
    findings = tuple(
        PlaybillNextFindingV1(
            severity=item.severity,
            reason=item.reason,
            subject_identity=item.subject_identity,
            related_identities=item.related_identities,
            detail=item.detail,
            repair=item.repair,
        )
        for item in rest
    )
    severity = min((head, *rest), key=lambda item: _SEVERITY_RANK[item.severity]).severity
    related = tuple(
        sorted(
            {
                identity
                for item in (head, *rest)
                for identity in (*item.related_identities, item.subject_identity)
                if identity != head.subject_identity
            },
            key=lambda identity: identity.encode("utf-8"),
        )
    )
    values = {
        **head.model_dump(exclude={"item_id", "tag", "findings"}),
        "severity": severity,
        "related_identities": related,
        "repair": head.repair,
        "findings": findings,
    }
    provisional = PlaybillNextItemV1.model_construct(
        _fields_set=None, item_id="sha256:" + "0" * 64, **values
    )
    return PlaybillNextItemV1.model_validate(
        {**values, "item_id": playbill_next_item_id(provisional)}
    )


#: How long an ``unsure`` examined attestation parks a standing row when
#: neither the attestation's ``valid_until`` nor its ClaimType says.
DEFAULT_UNSURE_HOLD = timedelta(days=30)
#: Rows nothing new arrives to break: a hold on them lapses instead.
_STANDING_HOLD_REASONS: frozenset[str] = frozenset({"claim_stale_evidence", "claim_uncovered"})


@dataclass(frozen=True)
class _UnsureHold:
    referent: AcceptedCoordinate
    attested_at: datetime
    valid_until: datetime | None


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


class _Holds:
    """Current ``unsure`` examined attestations, and whether each covers a row.

    An agent that examined a contested Claim and will not force a judgment
    attests ``unsure``. That parks the row only while the basis the agent
    looked at is unchanged: a new contender, a revised upstream Claim, newer
    evidence, or a further expiry brings it back. Standing rows -- stale or
    uncovered evidence -- have no such arrival, so a hold on them lapses at its
    ``valid_until``, else after the ClaimType's ``unsure_hold_for``, else after
    ``DEFAULT_UNSURE_HOLD``.
    """

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        coordinate: AcceptedProjectionCoordinate,
        claims: tuple[ClaimArtifactAny, ...],
        door_events: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...],
        evaluation_time: datetime,
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._evaluation_time = evaluation_time
        live = {
            claim.identity.qualified: claim for claim in claims if claim.lifecycle.state == "live"
        }
        self._current = {
            identity: claim_artifact_digest(claim).tagged for identity, claim in live.items()
        }
        self._claims = live
        self._hold_for: dict[str, timedelta] = {}
        self._seen: dict[tuple[str, tuple[tuple[str, str], ...]], bool] = {}
        # The latest examined stance per Claim and principal; a later support or
        # contradict by the same principal ends that principal's hold.
        latest: dict[
            tuple[str, str], tuple[tuple[datetime, int, int], ClaimAttestationStatementV2]
        ] = {}

        def consider(
            statement: ClaimAttestationStatementV2, order: tuple[datetime, int, int]
        ) -> None:
            identity = statement.claim_identity.qualified
            if (
                statement.attestation_basis != "examined_existing"
                or self._current.get(identity) != statement.claim_artifact_digest
            ):
                return
            key = (identity, statement.attesting_principal_id)
            if key not in latest or latest[key][0] < order:
                latest[key] = (order, statement)

        if self._current:
            with instance.bind_accepted_projection(coordinate) as projection:
                accepted = projection.typed.claim_attestations(
                    basis="examined_existing", current_claims_only=True
                )
            for envelope in accepted:
                consider(envelope.statement, (envelope.statement.attested_at, 0, 0))
        for event, payload in door_events:
            if payload.current_at_append is False:
                continue
            statement = payload.attestation.statement
            consider(statement, (statement.attested_at, 1, event.sequence))
        holds: dict[str, list[_UnsureHold]] = defaultdict(list)
        for (identity, _principal), (_order, statement) in latest.items():
            if (
                statement.stance == "unsure"
                and statement.attested_at <= evaluation_time
                and (statement.valid_until is None or evaluation_time < statement.valid_until)
            ):
                holds[identity].append(
                    _UnsureHold(
                        referent=AcceptedCoordinate.model_validate(
                            statement.referent_coordinate.model_dump(mode="json")
                        ),
                        attested_at=statement.attested_at,
                        valid_until=statement.valid_until,
                    )
                )
        self._holds = dict(holds)

    def __bool__(self) -> bool:
        return bool(self._holds)

    def covers(self, row: PlaybillNextItemV1 | PlaybillNextFindingV1) -> bool:
        detail = row.detail if isinstance(row.detail, Mapping) else {}
        if row.reason == "claim_conflicted":
            arguments = row.repair.arguments if isinstance(row.repair.arguments, Mapping) else {}
            contenders = arguments.get("claim_ids")
            if not isinstance(contenders, list) or not contenders:
                return False
            versions = self._versions(contenders)
            # Every contender examined, each by someone who saw all of them.
            return versions is not None and all(
                any(self._saw(hold, versions) for hold in self._holds.get(identity, ()))
                for identity, _digest in versions
            )
        holds = self._holds.get(row.subject_identity, ())
        if row.reason in _STANDING_HOLD_REASONS:
            last_expired = _instant(detail.get("last_expired_at"))
            return any(
                self._evaluation_time < self._lapses(row.subject_identity, hold)
                and (last_expired is None or last_expired <= hold.attested_at)
                for hold in holds
            )
        if row.reason in {
            "claim_contradicting_evidence_available",
            "claim_new_evidence_unreviewed",
        }:
            attested = _instant(detail.get("attested_at"))
            return attested is not None and any(attested <= hold.attested_at for hold in holds)
        if row.reason == "claim_dependency_stale":
            inputs = detail.get("stale_inputs")
            if not isinstance(inputs, list):
                return False
            upstream = [
                (item.get("source_claim_identity"), item.get("current_artifact_digest"))
                for item in inputs
                if isinstance(item, Mapping)
            ]
            if len(upstream) != len(inputs) or not all(
                isinstance(identity, str) and isinstance(digest, str)
                for identity, digest in upstream
            ):
                return False
            own = self._versions([row.subject_identity])
            if own is None:
                return False
            versions = tuple(sorted({*own, *upstream}))  # type: ignore[arg-type]
            return any(self._saw(hold, versions) for hold in holds)
        return False

    def _versions(self, identities: list[object]) -> tuple[tuple[str, str], ...] | None:
        versions = []
        for identity in identities:
            digest = self._current.get(identity) if isinstance(identity, str) else None
            if digest is None:
                return None
            versions.append((identity, digest))
        return tuple(sorted(versions))  # type: ignore[arg-type]

    def _saw(self, hold: _UnsureHold, versions: tuple[tuple[str, str], ...]) -> bool:
        """Whether every exact version was already accepted where the hold looked."""

        key = (hold.referent.git_oid, versions)
        if key not in self._seen:
            try:
                with self._instance.accepted_history_reader(at=hold.referent) as history:
                    self._seen[key] = all(
                        history.artifact(digest, identity=identity) is not None
                        for identity, digest in versions
                    )
            except PlaybillError:
                self._seen[key] = False
        return self._seen[key]

    def _lapses(self, identity: str, hold: _UnsureHold) -> datetime:
        if hold.valid_until is not None:
            return hold.valid_until
        claim = self._claims[identity]
        predicate = claim.statement.predicate
        if predicate not in self._hold_for:
            raw = self._instance.blob_at(self._coordinate.git_oid, claim_type_path(predicate))
            declared = (
                None
                if raw is None
                else parse_claim_type(raw, path=claim_type_path(predicate)).unsure_hold_for
            )
            self._hold_for[predicate] = (
                DEFAULT_UNSURE_HOLD
                if declared is None
                else timedelta(microseconds=declared.microseconds)
            )
        return hold.attested_at + self._hold_for[predicate]


def _apply_holds(
    items: tuple[PlaybillNextItemV1, ...], holds: _Holds
) -> tuple[tuple[PlaybillNextItemV1, ...], int]:
    """Park the rows an ``unsure`` hold covers; a finding it doesn't cover stays."""

    if not holds:
        return items, 0
    kept: list[PlaybillNextItemV1] = []
    held = 0
    for item in items:
        remaining = tuple(finding for finding in item.findings if not holds.covers(finding))
        held += len(item.findings) - len(remaining)
        if holds.covers(item):
            held += 1
            kept.extend(_row_of(finding) for finding in remaining)
        elif len(remaining) != len(item.findings):
            head = item.model_copy(update={"findings": ()})
            kept.append(_with_findings(head, map(_row_of, remaining)) if remaining else head)
        else:
            kept.append(item)
    return tuple(kept), held


def _row_of(finding: PlaybillNextFindingV1) -> PlaybillNextItemV1:
    return _item(
        severity=finding.severity,
        reason=finding.reason,
        subject_identity=finding.subject_identity,
        related_identities=finding.related_identities,
        detail=finding.detail,
        repair=finding.repair,
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

    accepted = _resolve_coordinate(instance, coordinate)
    tree = ClaimVerdictReadContext(instance, accepted).tree
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
        current = accepted_claim_attestations(
            instance,
            coordinate=accepted,
            tree=tree,
            claim=claim,
            historical=evidence.verified_attestations,
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
        superseded_accepted = frozenset(latest_door_by_principal)
        for rule in policy.rules:
            if rule.minimum_independent_control_components == 0:
                # A zero threshold escalates nothing; the rule is disabled.
                continue
            matching_accepted = tuple(
                item
                for item in current
                if item.current
                and (
                    not isinstance(item, AcceptedClaimAttestationEvidenceV1)
                    or item.envelope.statement.attestation_basis == "examined_existing"
                )
                and item.attestation_grade == "verified_principal"
                and item.statement.provider_or_principal.kind == "Principal"
                and item.statement.claim_statement_digest
                == claim_statement_digest(claim.statement).tagged
                and item.statement.stance == rule.stance
                and item.statement.provider_or_principal.name not in superseded_accepted
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
                    *(item.statement.provider_or_principal.name for item in matching_accepted),
                    *(item.attesting_principal_id for item in matching_door),
                )
            )
            if len(principal_identities) < rule.minimum_independent_control_components:
                continue
            attestation_digests = tuple(
                sorted(
                    (
                        *(item.attestation_digest for item in matching_accepted),
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
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    resolution_statuses: Mapping[str, str] | None = None,
    access_profile: CoverageAccessProfileV1 | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    # Claims are instance material: a caller not permitted to see it is told
    # nothing about them -- no identities, values, or verdicts -- exactly as the
    # citation and dependency folds already refuse.
    if access_profile is not None and not access_profile.permits("instance"):
        return ()
    if claims is None:
        listed = service_list_playbill_claims(instance, at=coordinate)
        claims = tuple(_claim_from_view(view) for view in listed.claims)
    claims = tuple(claim for claim in claims if claim.lifecycle.state == "live")
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
        # Two different values in one slot conflict only when the shared
        # semantic resolution -- the ClaimType's cardinality and resolution
        # policy at this evaluation time -- leaves them unresolved; a
        # many-valued predicate or a resolved slot is not a conflict.
        conflicted = slot.resolution == "unresolved" and (
            resolution_statuses is None
            or any(resolution_statuses.get(claim.identity.name) == "conflicted" for claim in group)
        )
        if conflicted:
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
            conflict_row: PlaybillNextItemV1 | None = _item(
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
        else:
            conflict_row = None
        # A conflicted slot's members still report their own evidence rows,
        # inside the conflict row rather than hidden behind it.
        member_rows: list[PlaybillNextItemV1] = []
        for claim in group:
            verdict = (
                None
                if verdicts_by_identity is None
                else verdicts_by_identity.get(claim.identity.qualified)
            )
            if verdict is None:
                verdict = service_evaluate_playbill_claim_verdict(
                    instance,
                    claim_identity=claim.identity.qualified,
                    evaluation_time=evaluation_time,
                    at=coordinate,
                ).verdict
                if verdicts_by_identity is not None:
                    verdicts_by_identity[claim.identity.qualified] = verdict
            if verdict.verdict == "stale_evidence":
                expirations = (
                    verdict.freshness_expirations
                    if isinstance(verdict, ClaimVerdictResultV2)
                    else ()
                )
                expired = tuple(item for item in expirations if evaluation_time >= item.expires_at)
                member_rows.append(
                    _item(
                        severity="repair",
                        reason="claim_stale_evidence",
                        subject_identity=claim.identity.qualified,
                        related_identities=(subject,),
                        detail={
                            "expired_capture_digests": [item.capture_digest for item in expired],
                            # A hold made after this instant saw every expiry here.
                            "last_expired_at": format_datetime(
                                max((item.expires_at for item in expired), default=None)
                            ),
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
                    member_rows.append(
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
            # A Claim not yet in effect is not uncovered: its evidence is judged
            # when its interval begins, not before.
            if verdict.verdict != "uncovered" or verdict.currency == "not_applicable":
                continue
            member_rows.append(
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
        if conflict_row is None:
            items.extend(member_rows)
        else:
            items.append(_with_findings(conflict_row, member_rows) if member_rows else conflict_row)
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
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> dict[str, _CitationCommitment]:
    population = (
        (
            _claim_from_view(view)
            for view in service_list_playbill_claims(instance, at=coordinate).claims
        )
        if claims is None
        else iter(claims)
    )
    internal_coordinate = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    store = instance.body_store()
    access = BodyAccessContext(principal_id="playbill-next", can_read_body=True)
    result: dict[str, _CitationCommitment] = {}
    facts = (
        build_accepted_query_facts(instance, coordinate=internal_coordinate)
        if facts_reader is None
        else facts_reader.build()
    )
    subjects = {subject.path: subject for subject in facts.subjects}
    providers = {provider.identity.qualified: provider for provider in facts.providers}
    visible_claims = {
        row.accepted.claim.identity.qualified
        for row in facts.claims
        if claim_row_visibility(
            row,
            subject=subjects.get(row.subject_path),
            providers=providers,
            policy=PROJECTION_VISIBILITY_POLICY,
            evaluation_time=evaluation_time,
        )
        is not None
    }
    try:
        for claim in population:
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
                historical = _historical_claim(
                    instance,
                    coordinate=internal_coordinate,
                    identity=claim.identity.qualified,
                    digest=predecessor_digest,
                )
                predecessor = None if historical is None else historical[0]
                if predecessor is None:
                    lineage_note = "predecessor_unresolved"
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


def _historical_claim(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    identity: str,
    digest: str,
) -> tuple[ClaimArtifactAny, AcceptedProjectionCoordinate] | None:
    """Read one exact retained version, regardless of unrelated generation count."""
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        location = history.artifact(digest, identity=identity)
        if location is None:
            return None
        generation = history.generation(location.occurrence_sequence)
    raw = instance.blob_at(generation.git_oid, location.path)
    if raw is None:
        return None
    claim = parse_claim(raw, path=location.path)
    if claim.identity.qualified != identity or claim_artifact_digest(claim).tagged != digest:
        raise ProposalIntegrityError("historical Claim differs from its indexed identity or digest")
    return claim, instance.coordinate_for_oid(generation.git_oid)


def _bounded_claim_lineages(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    current_claims: Mapping[str, ClaimArtifactAny],
    max_nodes: int = MAX_DEPENDENCY_LINEAGE_NODES,
) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """Follow indexed predecessors; unrelated generations consume no traversal budget."""
    lineages: dict[str, tuple[str, ...]] = {}
    incomplete: set[str] = set()
    remaining = max_nodes
    for path, current in sorted(current_claims.items()):
        found = {claim_artifact_digest(current).tagged}
        expected = current.lifecycle.predecessor_digest
        while expected is not None and remaining > 0 and expected not in found:
            remaining -= 1
            historical = _historical_claim(
                instance,
                coordinate=coordinate,
                identity=current.identity.qualified,
                digest=expected,
            )
            if historical is None:
                break
            found.add(expected)
            expected = historical[0].lifecycle.predecessor_digest
        if expected is not None:
            incomplete.add(path)
        lineages[path] = tuple(sorted(found))
    return lineages, frozenset(incomplete)


@dataclass(frozen=True)
class _AttestationLineageArtifact:
    claim: ClaimArtifactAny
    artifact_digest: str
    tree: Mapping[str, bytes]


def _attestation_claim_lineage(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim_identity: str,
) -> tuple[tuple[_AttestationLineageArtifact, ...], bool]:
    """Recover one Claim lineage at the request coordinate, oldest first."""

    path = claim_path(claim_identity)
    current_tree = ClaimVerdictReadContext(instance, coordinate).tree
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
    seen = {found[0].artifact_digest}
    while (
        expected is not None and len(found) < MAX_DEPENDENCY_LINEAGE_NODES and expected not in seen
    ):
        historical = _historical_claim(
            instance, coordinate=coordinate, identity=current.identity.qualified, digest=expected
        )
        if historical is None:
            break
        predecessor, predecessor_coordinate = historical
        found.append(
            _AttestationLineageArtifact(
                claim=predecessor,
                artifact_digest=expected,
                tree=ClaimVerdictReadContext(instance, predecessor_coordinate).tree,
            )
        )
        seen.add(expected)
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
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1 | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Fold new-capture memberships against immutable acceptance-time accounts.

    Only attestations current at the evaluation time count, exactly as for the
    attestation threshold: one not yet made, or past its ``valid_until``, asks
    nothing of anyone.
    """

    if access_profile is not None and not access_profile.permits("instance"):
        return ()

    from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest

    observations: list[tuple[ClaimAttestationV2, str | None, bool | None]] = [
        (payload.attestation, event.event_digest, payload.current_at_append)
        for event, payload in door_events
    ]
    seen = {claim_attestation_v2_envelope_digest(envelope) for envelope, _, _ in observations}
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted = projection.typed.claim_attestations(basis="new_capture")
    observations.extend(
        (envelope, None, None)
        for envelope in accepted
        if claim_attestation_v2_envelope_digest(envelope) not in seen
    )
    if not observations:
        return ()
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
    for envelope, event_digest, current_at_append in observations:
        statement = envelope.statement
        if statement.attestation_basis != "new_capture":
            continue
        if statement.attested_at > evaluation_time or (
            statement.valid_until is not None and evaluation_time >= statement.valid_until
        ):
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
                        "attestation_event_digest": event_digest,
                        "attestation_envelope_digest": claim_attestation_v2_envelope_digest(
                            envelope
                        ),
                        "attestation_basis": statement.attestation_basis,
                        "stance": statement.stance,
                        "attesting_principal": statement.attesting_principal_id,
                        "attested_at": format_datetime(statement.attested_at),
                        "current_at_append": current_at_append,
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
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Coalesce stale recorded backing-input edges through the existing impact walker."""

    if not access_profile.permits("instance"):
        return ()
    facts = (
        build_accepted_query_facts(instance, coordinate=coordinate, include_retired=True)
        if facts_reader is None
        else facts_reader.build(include_retired=True)
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
            policy=PROJECTION_VISIBILITY_POLICY,
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


def _source_scan_defect_notes(
    observed: PlaybillNextSourceObservationAny,
) -> tuple[str, ...] | None:
    """Report the notes evidencing a defect in the source's own scan.

    A defect here is whole-source: the evidence ``_source_citation_item``
    consults was never collected, so every citation to this source falls
    through the same gate no matter what the citation says. ``None`` means the
    scan is healthy and an unobserved citation there is a finding about that
    citation, to be repaired one citation at a time.

    Both observation versions are covered. V4 says so through its notes alone;
    V3 additionally carries ``scan_complete``, and its own validator forbids an
    incomplete scan from asserting any occurrence or scanned digest, so an
    incomplete V3 scan is whole-source by construction even when it names no
    note at all.
    """

    notes = tuple(
        note for note in observed.scan_notes if note in _COVERAGE_SOURCE_SCAN_DEFECT_NOTES
    )
    if notes:
        return notes
    if isinstance(observed, PlaybillNextSourceObservationV3) and not observed.scan_complete:
        return ()
    return None


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


def _citation_unobserved_item(
    commitment: _CitationCommitment,
    *,
    source_scan_notes: tuple[str, ...] = (),
    collapsed_citation_count: int = 0,
) -> PlaybillNextItemV1:
    source_id = commitment.source_id
    assert source_id is not None
    lineage_detail = (
        {} if commitment.lineage_note is None else {"lineage_note": commitment.lineage_note}
    )
    # A source whose own coverage scan came back defective observes none of its
    # citations, so every one of them reads unobserved for that one reason.
    # Report the cause once, naming what the scan reported and how many
    # citations stand behind it, instead of hundreds of look-alike rows an
    # agent cannot tell apart - and name the *cause*, not a co-symptom: a row
    # that says "the window cap was exceeded" sends a reader to raise a cap
    # that would clear nothing.
    collapsed_detail: dict[str, object] = (
        {}
        if not collapsed_citation_count
        else {
            "unobserved_cause": "source_scan_incomplete",
            "source_scan_notes": list(source_scan_notes),
            "collapsed_citation_count": collapsed_citation_count,
        }
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
            **collapsed_detail,
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
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> tuple[tuple[NextDomain, ...], tuple[PlaybillNextItemV1, ...]]:
    if observation is None:
        return (), ()
    domains: list[NextDomain] = []
    items: list[PlaybillNextItemV1] = []
    if observation.floor_status is not None or observation.installed_coordinate is not None:
        domains.append("workspace_floor")
        status = observation.floor_status
        # A broken floor is work; a missing or stale one is environment status.
        if status == "invalid":
            items.append(
                _item(
                    severity="blocking",
                    reason="floor_invalid",
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
            claims=claims,
            facts_reader=facts_reader,
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
            claims=claims,
            facts_reader=facts_reader,
        )
        defective_scan_notes = {
            source.source_id: notes
            for source in observation.source_observations
            for notes in (_source_scan_defect_notes(source),)
            if notes is not None
        }
        defective_commitments: dict[str, list[_CitationCommitment]] = defaultdict(list)
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
            if item is None:
                continue
            if (
                item.reason == "citation_source_unobserved"
                and commitment.source_id in defective_scan_notes
            ):
                defective_commitments[commitment.source_id].append(commitment)
                continue
            items.append(item)
        for source_id in sorted(defective_commitments, key=lambda item: item.encode("utf-8")):
            group = defective_commitments[source_id]
            items.append(
                _citation_unobserved_item(
                    group[0],
                    source_scan_notes=defective_scan_notes[source_id],
                    collapsed_citation_count=len(group),
                )
            )
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


def _ledger_mirror_health(instance: PlaybillInstance) -> PlaybillNextHealthV1:
    """Where this instance's published copy of its ledger stands against the head.

    Measured at the accepted head, never at a coordinate the caller asked to
    read at. A push still in flight is informational (`publishing`); a failed
    push (`behind`) or a mirror nothing was ever published to
    (`never_published`) calls for attention, because what repairs it is off
    this host.
    """

    url = instance.ledger_mirror_url()
    if url is None:
        return PlaybillNextHealthV1(state="not_configured")
    state = instance.ledger_mirror_state()
    head = instance.accepted_coordinate()
    restore = PlaybillNextRepairV1(
        operation="hand_edit",
        target=url,
        required_change="restore_the_ledger_mirror_remote_or_its_credential",
    )
    if state is None or state.url != url:
        return PlaybillNextHealthV1(
            state="never_published",
            detail={"mirror_url": url, "message": "nothing has been published to this remote"},
            repair=restore,
        )
    if state.status == "current" and state.published_main_oid == head.git_oid:
        return PlaybillNextHealthV1(state="current", detail={"mirror_url": url})
    if state.status == "behind":
        return PlaybillNextHealthV1(
            state="behind",
            detail={
                "mirror_url": url,
                "attempted_at": state.attempted_at,
                "requested_sequence": state.requested_sequence,
                "published_sequence": state.published_sequence,
                "message": state.detail or "ledger publication failed",
                "publication_command": "cruxible playbill ledger publish --json",
            },
            repair=restore,
        )
    return PlaybillNextHealthV1(
        state="publishing",
        detail={
            "mirror_url": url,
            "published_main_oid": state.published_main_oid,
            "accepted_git_oid": head.git_oid,
            "status": state.status,
        },
    )


def _procedure_catalog_health(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfileV1,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> PlaybillNextHealthV1:
    """Whether a workspace that asked for a complete Procedure catalog has one.

    The advisory is off unless a kit or workspace turns it on; a live Procedure
    missing from a catalog nobody asked to be complete is not a finding.
    """

    if (
        observation is None
        or observation.projection_coverage is None
        or observation.presentation_policy_notes
        or not access_profile.permits("instance")
    ):
        return PlaybillNextHealthV1(state="not_observed")
    coverage = observation.projection_coverage
    if coverage.coordinate.model_dump(mode="json") != PlaybillAcceptedCoordinate.from_internal(
        coordinate
    ).model_dump(mode="json"):
        return PlaybillNextHealthV1(state="not_observed")
    policy = upgrade_playbill_presentation_policy(
        observation.presentation_policy or PlaybillPresentationPolicyV1()
    )
    if not policy.projection_advisories.procedure or "Procedure" not in coverage.complete_kinds:
        return PlaybillNextHealthV1(state="not_required")
    covered = {
        item.artifact.qualified for item in coverage.bindings if item.artifact.kind == "Procedure"
    }
    missing: list[tuple[str, dict[str, object]]] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        procedures = projection.typed.procedure_inventory()
    for procedure in procedures:
        if procedure.lifecycle != "live" or procedure.identity in covered:
            continue
        identity = parse_artifact_identity(procedure.identity)
        catalog_entry: dict[str, object] = {
            "kind": "procedure",
            "procedure_identity": identity.model_dump(mode="json"),
            "locator": f"procedures/{identity.name}.md",
        }
        missing.append((identity.qualified, catalog_entry))
    if not missing:
        return PlaybillNextHealthV1(state="complete")
    missing.sort(key=lambda item: item[0].encode("utf-8"))
    identities = [item[0] for item in missing]
    entries = [item[1] for item in missing]
    return PlaybillNextHealthV1(
        state="missing",
        detail={
            "unprojected_procedure_ids": identities,
            "catalog_entries": entries,
            "message": "accepted Procedures have no configured workspace projection",
        },
        repair=PlaybillNextRepairV1(
            operation="hand_edit",
            target=".playbill/sources.yaml",
            required_change="add_procedure_projection_catalog_entries",
            arguments={"catalog_entries": entries},
        ),
    )


def _floor_health(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    observation: PlaybillNextWorkspaceObservationV1 | None,
) -> PlaybillNextHealthV1:
    """Whether the workspace's installed floor matches the accepted coordinate.

    A workspace that never configured a floor says nothing; a missing or
    stale one names the export that repairs it. An invalid floor is a
    blocking work row, not status.
    """

    if observation is None or (
        observation.floor_status is None and observation.installed_coordinate is None
    ):
        return PlaybillNextHealthV1(state="not_observed")
    status = observation.floor_status
    if status == "not_configured":
        return PlaybillNextHealthV1(state="not_configured")
    installed = observation.installed_coordinate
    detail = {
        "installed_coordinate": None if installed is None else installed.model_dump(mode="json"),
        "reported_status": status,
    }
    export = PlaybillNextRepairV1(
        operation="playbill.floor.export",
        target=instance.descriptor.instance_id,
        required_change="replace_installed_floor",
        arguments={},
    )
    export = export.model_copy(
        update={"command": _repair_command(export.operation, arguments=export.arguments)}
    )
    if status == "missing":
        return PlaybillNextHealthV1(state="missing", detail=detail, repair=export)
    if status == "invalid":
        # The blocking floor_invalid row carries the repair.
        return PlaybillNextHealthV1(state="invalid", detail=detail)
    stale = (
        installed is not None
        and installed != AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json"))
    ) or (status == "stale" and installed is None)
    if stale:
        return PlaybillNextHealthV1(state="stale", detail=detail, repair=export)
    return PlaybillNextHealthV1(state="current", detail=detail)


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
    tree = ClaimVerdictReadContext(instance, coordinate).tree
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


def _proposal_items(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    access_profile: CoverageAccessProfileV1,
) -> tuple[PlaybillNextItemV1, ...]:
    """Stale proposals: admitted work that can no longer activate where it stands.

    Activation settles a candidate only onto the state it was evaluated
    against, so a proposal whose parent head has moved past waits on its
    author: readmit rebases the same tree onto the current head, and withdraw
    says it will never be settled. Either one closes the row.
    """

    if not access_profile.permits("instance"):
        return ()
    return tuple(
        _item(
            severity="repair",
            reason="proposal_stale",
            subject_identity=proposal.proposal_id,
            related_identities=(proposal.target_ref,),
            detail={
                "actor_id": proposal.actor_id,
                "admitted_at": proposal.admitted_at,
                "candidate_parent_semantic_root": proposal.candidate_parent_semantic_root,
                "accepted_semantic_root": coordinate.semantic_root,
                "target_ref": proposal.target_ref,
            },
            repair=PlaybillNextRepairV1(
                operation="playbill.proposal.readmit",
                target=proposal.proposal_id,
                required_change="readmit_as_its_author_or_withdraw_the_stale_proposal",
                arguments={"proposal_id": proposal.proposal_id},
            ),
        )
        for proposal in stale_unreadmitted_proposals(instance, coordinate)
    )


def _registered_publication_blocks(
    instance: PlaybillInstance,
) -> dict[tuple[str, str], ProjectionBlockRegistration] | None:
    """Fold every block this instance registers, whichever road declared it.

    Both roads, one identity: the pair the page itself names. Before this the
    question "is this marker sanctioned?" was asked only of block ids beginning
    `pub-`, which is a spelling the retired publication road minted, so a block
    an agent declared with `block repin` was never checked against anything.
    """

    return registered_projection_blocks(instance)


def _registrations_released_by_retirement(
    registrations: Mapping[tuple[str, str], ProjectionBlockRegistration],
    *,
    tree: Mapping[str, bytes],
) -> frozenset[tuple[str, str]]:
    """The registered blocks whose backing Claim has been retired.

    The fold reads protocol state and never opens the Claim tree, so a ruling
    that retired a block's backing Claim left its registration standing and
    `next` went on demanding the frame -- for a block that same ruling had told
    the author to delete, with the repair "restore it". A registration whose
    Claim is retired registers nothing: the world has moved past that page.

    Only a publication registration has a backing Claim to retire. A block an
    agent declared holds a LIST, and a retirement inside that list is reported
    as a stale backing on the block, not as the block ceasing to exist.

    It takes the already-folded registrations rather than folding again: the
    fold walks every durable intent event, and one `next` used to reach it from
    three places plus once per block.
    """

    released: set[tuple[str, str]] = set()
    for key, registration in registrations.items():
        publication = registration.publication
        if publication is None:
            continue
        path = claim_path(publication.claim_identity)
        raw = tree.get(path)
        if raw is not None and parse_claim(raw, path=path).lifecycle.state == "retired":
            released.add(key)
    return frozenset(released)


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
    # A registered block whose marker is not in the page is the author having
    # removed it. The instance is the half that still expects it, so the repair
    # is to release the registration, not to put back a block a ruling may have
    # told the author to delete -- which was the only named repair before
    # `block depublish` existed, and the reason a re-modelled page had none.
    return _item(
        severity="blocking",
        reason="projection_marker_invalid",
        subject_identity=target,
        related_identities=(),
        detail=detail,
        repair=PlaybillNextRepairV1(
            operation=(
                "playbill.block.depublish"
                if marker_status == "registered_marker_missing"
                else "playbill.block.repin"
            ),
            target=target,
            required_change=(
                "depublish_or_restore_the_registered_block"
                if marker_status == "registered_marker_missing"
                else "restore_projection_frame_then_repin"
            ),
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
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
    resolution_statuses: Mapping[str, str] | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Report shared currency assessments without suppressing sibling findings."""

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

    tree = ClaimVerdictReadContext(instance, coordinate).tree
    # One fold per `next`. The registration fold parses every durable intent
    # event, and the queue used to reach it from three places and once more per
    # syncable block; the retirement release now reads the same folded result.
    folded = _registered_publication_blocks(instance)
    registrations: frozenset[tuple[str, str]] | None = None
    if folded is not None:
        registrations = frozenset(folded) - _registrations_released_by_retirement(folded, tree=tree)
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

    checks = ProjectionCheckContext(
        instance,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        stamps=tuple(marker.stamp for source in sources for marker in source.marker_summaries),
        facts_reader=facts_reader,
        verdicts_by_identity=verdicts_by_identity,
        resolution_statuses=resolution_statuses,
    )
    for source in sources:
        for marker in source.marker_summaries:
            registration = (source.source_id, marker.stamp.block_id)
            if registrations is not None and registration not in registrations:
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
                        # A repin declares the block to the instance, so the
                        # verb that registers a marker is the verb that wrote
                        # it. Removing the marker is the other road, and the
                        # required change still names both.
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="remove_or_register_projection_block",
                            arguments={
                                "source_id": source.source_id,
                                "block_id": marker.stamp.block_id,
                            },
                        ),
                    )
                )
            assessment = checks.read(PlaybillBlockSyncReadRequestV1(stamp=marker.stamp))
            severity: NextSeverity = (
                "blocking" if marker.stamp.currency_policy == "require_current" else "warning"
            )
            stale = [b.identity.qualified for b in assessment.moved_backings]
            retired = [
                i.identity.qualified
                for i in assessment.issues
                if i.reason == "block_backing_retired"
            ]
            overturned = [
                i.identity.qualified
                for i in assessment.issues
                if i.reason == "block_backing_overturned"
            ]
            target = f"{source.source_id}#{marker.stamp.block_id}"
            identities = tuple(
                sorted(
                    (backing.identity.qualified for backing in marker.stamp.backing),
                    key=lambda value: value.encode("utf-8"),
                )
            )
            arguments = {"source_id": source.source_id, "block_id": marker.stamp.block_id}
            unresolved = tuple(
                i for i in assessment.issues if i.identity.qualified not in retired + overturned
            )
            if unresolved or (assessment.status == "refused" and not assessment.issues):
                items.append(
                    _item(
                        severity="blocking" if assessment.status == "refused" else severity,
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=tuple(i.identity.qualified for i in unresolved),
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "backing_state": "unchecked"
                            if assessment.status == "unchecked"
                            else "invalid"
                            if assessment.status == "refused"
                            else "missing",
                            "issues": [i.model_dump(mode="json") for i in unresolved],
                            "message": assessment.detail,
                            "currency_policy": marker.stamp.currency_policy,
                        },
                        repair=PlaybillNextRepairV1(
                            operation="playbill.block.repin",
                            target=target,
                            required_change="restore_dependency_check_then_review_and_repin",
                            arguments=arguments,
                        ),
                    )
                )
            if marker.observed_body_digest != marker.stamp.body_digest:
                items.append(
                    _item(
                        severity=severity,
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
            # A block holds a LIST. One member of it going away is not the
            # block going away: the proportionate repair is a repin that drops
            # that member and re-authors the prose around the rest. Releasing
            # the whole registration is right only when there is nothing left
            # to hold -- when every held member has retired or been overturned.
            held_members = frozenset(backing.identity.qualified for backing in marker.stamp.backing)
            gone = frozenset(retired) | frozenset(overturned)
            surviving = tuple(sorted(held_members - gone, key=lambda value: value.encode("utf-8")))
            exhausted = bool(held_members) and not surviving
            for moved, detail_key, change in (
                (retired, "retired_backings", "retired"),
                (overturned, "overturned_backings", "overturned"),
            ):
                if not moved:
                    continue
                related = tuple(sorted(moved, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity=severity,
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            detail_key: list(related),
                            # The discriminator the repair follows from: the
                            # row's operation is a function of whether the held
                            # list still has a member to hold.
                            "backing_state": "exhausted" if exhausted else change,
                            "surviving_backings": list(surviving),
                        },
                        # No verb republishes a retired backing. When something
                        # survives, the block is repinned onto exactly that;
                        # when nothing does, the registration is released and
                        # the marker leaves the page.
                        repair=(
                            PlaybillNextRepairV1(
                                operation="playbill.block.depublish",
                                target=target,
                                required_change=f"depublish_{change}_backing_block",
                                arguments=arguments,
                            )
                            if exhausted
                            else PlaybillNextRepairV1(
                                operation="playbill.block.repin",
                                target=target,
                                required_change=f"drop_the_{change}_backing_then_repin",
                                arguments={
                                    **arguments,
                                    "claim": [
                                        x.removeprefix("Claim:")
                                        for x in surviving
                                        if x.startswith("Claim:")
                                    ],
                                    "clear_claims": not any(
                                        x.startswith("Claim:") for x in surviving
                                    ),
                                },
                            )
                        ),
                    )
                )
            if stale:
                related = tuple(sorted(stale, key=lambda value: value.encode("utf-8")))
                items.append(
                    _item(
                        severity=severity,
                        reason="projection_backing_stale",
                        subject_identity=target,
                        related_identities=related,
                        detail={
                            "source_id": source.source_id,
                            "block_id": marker.stamp.block_id,
                            "stale_backings": list(related),
                            "backing_state": "revised",
                        },
                        # Nothing renders a block, so nothing converges one: a
                        # member that moved is answered by reading the prose
                        # against the new state and re-stamping the list.
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
    # One request evaluates a Claim's verdict at exactly one coordinate and one
    # evaluation time, so the folds that need it share the result instead of
    # each walking all 740 Claims. The map dies with the request.
    verdicts_by_identity: dict[str, ClaimVerdictResultAny] = {}
    # Derive the whole population ONCE, here, through the same fold `orient`
    # uses. `_claim_items` used to walk every live Claim itself and evaluate
    # whatever the map did not already hold, so a `next` paid for the entire
    # derivation even when an `orient` at the same state had just done it: the
    # memo could not serve a fold that never asked it. Asking it first shares
    # one answer with every fold in this request and with every later read of
    # the same state.
    # These inputs belong to this evaluation only. A later request constructs a
    # fresh reader so body availability and other mutable evidence are observed.
    facts_reader = _AcceptedQueryFactsRead(instance, coordinate=coordinate)
    parsed_claims: tuple[ClaimArtifactAny, ...] | None = None
    resolution_statuses: Mapping[str, str] | None = None
    try:
        parsed_claims = tuple(
            _claim_from_view(view)
            for view in service_list_playbill_claims(
                instance, at=public_coordinate, include_retired=True
            ).claims
        )
        resolution_statuses = claim_resolution_statuses(
            instance,
            claims=parsed_claims,
            at=public_coordinate,
            evaluation_time=request.evaluation_time,
            verdicts_by_identity=verdicts_by_identity,
        )
    except PlaybillError:
        # A queue is a read of whatever resolves. If the population cannot be
        # resolved as a whole -- a missing ClaimType, an unreadable store --
        # each fold falls back to deriving what it needs and reporting on it.
        verdicts_by_identity.clear()
    workspace_domains, workspace_items = _workspace_items(
        instance,
        coordinate=public_coordinate,
        evaluation_time=request.evaluation_time,
        access_profile=request.access_profile,
        observation=request.workspace_observation,
        claims=parsed_claims,
        facts_reader=facts_reader,
    )
    observed = tuple(
        domain
        for domain in _ALL_DOMAINS
        if domain == "accepted_state" or domain in workspace_domains
    )
    unobserved = tuple(domain for domain in _ALL_DOMAINS if domain not in observed)
    found = (
        *_claim_items(
            instance,
            coordinate=public_coordinate,
            evaluation_time=request.evaluation_time,
            expiring_within=request.expiring_within,
            door_events=door_events,
            verdicts_by_identity=verdicts_by_identity,
            claims=parsed_claims,
            resolution_statuses=resolution_statuses,
            access_profile=request.access_profile,
        ),
        *_claim_attestation_door_items(
            instance,
            coordinate=coordinate,
            door_events=door_events,
            evaluation_time=request.evaluation_time,
            access_profile=request.access_profile,
        ),
        *workspace_items,
        *_projection_items(
            instance,
            coordinate=coordinate,
            evaluation_time=request.evaluation_time,
            access_profile=request.access_profile,
            observation=request.workspace_observation,
            verdicts_by_identity=verdicts_by_identity,
            facts_reader=facts_reader,
            resolution_statuses=resolution_statuses,
        ),
        *_claim_dependency_items(
            instance,
            coordinate=coordinate,
            evaluation_time=request.evaluation_time,
            access_profile=request.access_profile,
            facts_reader=facts_reader,
        ),
        *_document_items(
            instance,
            coordinate=coordinate,
            access_profile=request.access_profile,
            observation=request.workspace_observation,
        ),
        *_proposal_items(
            instance,
            coordinate=public_coordinate,
            access_profile=request.access_profile,
        ),
    )
    held = 0
    if parsed_claims is not None and request.access_profile.permits("instance"):
        found, held = _apply_holds(
            found,
            _Holds(
                instance,
                coordinate=coordinate,
                claims=parsed_claims,
                door_events=door_events,
                evaluation_time=request.evaluation_time,
            ),
        )
    items = tuple(sorted(_group_items(found), key=_item_sort_key))
    terminal = instance.descriptor.decommissioned
    status = PlaybillNextStatusV1(
        blocking=terminal is not None,
        held=held,
        instance=(
            PlaybillNextHealthV1(state="active")
            if terminal is None
            else PlaybillNextHealthV1(
                state="decommissioned",
                detail={
                    "reason": terminal.reason,
                    "decommissioned_at": terminal.decommissioned_at,
                    "decommissioned_by": terminal.decommissioned_by,
                },
                repair=PlaybillNextRepairV1(
                    operation="hand_edit",
                    target="instance.json",
                    required_change=(
                        "allocate_a_new_instance_with_playbill_host_create_or_"
                        "archive_this_directory_yourself"
                    ),
                ),
            )
        ),
        floor=_floor_health(
            instance, coordinate=public_coordinate, observation=request.workspace_observation
        ),
        ledger_mirror=_ledger_mirror_health(instance),
        provider_lane=(
            PlaybillNextHealthV1(state="not_reported")
            if provider_lane is None
            else PlaybillNextHealthV1(state="available")
            if provider_lane.state != "unavailable"
            else PlaybillNextHealthV1(
                state="unavailable",
                detail={"code": provider_lane.code, "detail": provider_lane.detail},
                repair=PlaybillNextRepairV1(
                    operation="hand_edit",
                    target="daemon/provider-runtime.json",
                    required_change=(
                        "repair_provider_runtime_configuration_or_use_a_shorter_"
                        "state_root_then_retry"
                    ),
                ),
            )
        ),
        procedure_catalog=_procedure_catalog_health(
            instance,
            coordinate=coordinate,
            access_profile=request.access_profile,
            observation=request.workspace_observation,
        ),
    )
    values = {
        "coordinate": public_coordinate,
        "evaluation_time": request.evaluation_time,
        "observed_domains": observed,
        "unobserved_domains": unobserved,
        "status": status,
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
