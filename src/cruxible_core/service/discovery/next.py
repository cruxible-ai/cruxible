"""Deterministic repair queue derived from one accepted Cruxible coordinate."""

from __future__ import annotations

import base64
import json
import shlex
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, cast

if TYPE_CHECKING:
    from cruxible_core.consumers.next.facts import CitationRequest, ReadHeads, ServedRows

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts import (
    NEXT_DEFAULT_LIMIT,
    NEXT_MAX_LIMIT,
    NextReason,
    NextRepairGate,
    NextRepairOperand,
    NextRepairOperation,
    NextSeverity,
    ProviderLaneStatus,
)
from cruxible_client.contracts.accepted_attestations import AcceptedClaimAttestationEvidence
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.authoring.inputs import LineInput
from cruxible_client.contracts.authoring.models import BlockSyncReadRequest
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    DIRECT_SELF_ASSERTED_CONTRACT_ID,
    FOREIGN_SOURCE_COORDINATE_TYPE,
    CanonicalDuration,
    observed_content,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEvent,
    ClaimAttestationEventPayload,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationStatement,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    effective_evidence_requirement,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimVerdictResult,
    ClaimVerdictResultAny,
    EvidenceFreshnessExpiration,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimLawEvidenceAny,
    LiteralClaimObject,
    SubjectClaimObject,
    _is_claim_type_rederivation,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.codes import CurrentCode, normalize_code
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_CARDS_PER_SOURCE,
    PresentationPolicyAny,
    PresentationPolicyNote,
    PresentationPolicyV1,
    ProjectionClaimBacking,
    ProjectionCoverageObservation,
    ProjectionMarkerSummary,
    upgrade_presentation_policy,
)
from cruxible_client.contracts.documents import document_path, parse_document
from cruxible_client.contracts.errors import CruxibleError, ProposalIntegrityError
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionIdentityRule
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.procedure_mandates import ProcedureMandate, ProcedureMandateV1
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.public_names import public_name
from cruxible_client.contracts.refusal_receipts import PriorRefusalCoverage
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, parse_datetime
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_core.claims.claim_slots import classify_claim_slot
from cruxible_core.compiler.compiler import (
    COMPILER_REVISION_LABELS,
    LIGHT_ARTIFACTS_COMPILER,
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.compiler.upgrades import upgrade_law
from cruxible_core.consumers.protocol import ConsumerHealth
from cruxible_core.coverage.contracts import (
    CitationWindowObservation,
    CoverageAccessProfile,
    CoverageCommitmentScanProof,
    LogicalSourceIdentity,
)
from cruxible_core.coverage.indexes import (
    WorkingOccurrence,
)
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.typed_state import LIVE_SELECTED_SLOT_CLAIMS_SQL, utc_microseconds
from cruxible_core.query.backends import claim_row_visibility
from cruxible_core.query.impact import (
    SOURCE_CONTRADICTED,
    SOURCE_SUPERSEDED,
    DependencyImpactRequestV1,
    DependentImpactV1,
    build_dependency_impact,
)
from cruxible_core.runtime.instance import PlaybillInstance
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
    service_list_playbill_claims,
)
from cruxible_core.service.discovery.claim_status import claim_resolution_statuses
from cruxible_core.service.discovery.query import (
    _AcceptedQueryFactsRead,
    build_accepted_query_facts,
    verify_retired_claim_adjudication,
)
from cruxible_core.service.evidence.evidence import (
    ClaimVerdictReadContext,
    accepted_claim_attestations,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.proposals.proposals import (
    proposals_awaiting_activation,
    proposals_awaiting_approval,
    stale_proposals_awaiting_approval,
    stale_unreadmitted_proposals,
)
from cruxible_core.service.proposals.publications import (
    ProjectionBlockRegistration,
    registered_projection_blocks,
)
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.read_positions import read_body

NEXT_ITEM_ID_DOMAIN = "playbill-next-item-v1"
NEXT_RESULT_V2_DIGEST_DOMAIN = "playbill-next-result-v2"
DEFAULT_EXPIRING_WITHIN_MICROSECONDS = 604_800_000_000
MAX_DEPENDENCY_LINEAGE_NODES = 4096

NextDomain = Literal[
    "accepted_state",
    "workspace_floor",
    "workspace_sources",
    "workspace_projections",
]
CitationLineageNote = Literal[
    "predecessor_lineage_limit_exceeded",
    "predecessor_unresolved",
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
# source-wide and is deliberately absent, because collapsing on it would put a
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


class NextError(CruxibleError):
    code = "cruxible.next.refused"

    @property
    def error_code(self) -> str:
        return self.code


class NextAccessProfileInvalid(NextError):
    code = "cruxible.next.access_profile_invalid"


class NextWorkspaceObservationInvalid(NextError):
    code = "cruxible.next.workspace_observation_invalid"


class NextCoordinateNotAccepted(NextError):
    code = "cruxible.next.coordinate_not_accepted"


class NextAcceptedStateInvalid(NextError):
    code = "cruxible.next.accepted_state_invalid"


class NextCursorMismatch(NextError):
    """A page cursor that does not continue the queue this request reads."""

    code = "cruxible.next.cursor_mismatch"


class NextDriftObservation(_StrictNextModel):
    citation_id: str
    expected_commitment_digest: str
    observed_commitment_digest: str

    @field_validator("citation_id", "expected_commitment_digest", "observed_commitment_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class NextSourceObservation(_StrictNextModel):
    tag: Literal["playbill-next-source-observation-v4"] = "playbill-next-source-observation-v4"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    document_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    observed_source_digest: str
    byte_length: int = Field(ge=0)
    marker_summaries: tuple[ProjectionMarkerSummary, ...] = Field()
    occurrences: tuple[WorkingOccurrence, ...] = Field(max_length=MAX_PROJECTION_CARDS_PER_SOURCE)
    commitment_scan_proofs: tuple[CoverageCommitmentScanProof, ...] = Field(
        max_length=MAX_PROJECTION_CARDS_PER_SOURCE
    )
    citation_window_observations: tuple[CitationWindowObservation, ...] = Field(
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
    def _source_shape(self) -> "NextSourceObservation":
        expected_source = LogicalSourceIdentity(plane="external", identity=self.source_id)
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


class NextMissingBinding(_StrictNextModel):
    """A catalog entry binding a workspace file that does not exist."""

    tag: Literal["playbill-next-missing-binding-v1"] = "playbill-next-missing-binding-v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    document_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    locator: str = Field(min_length=1, max_length=4096)


class NextWorkspaceObservation(_StrictNextModel):
    tag: Literal["playbill-next-workspace-observation-v1"] = (
        "playbill-next-workspace-observation-v1"
    )
    floor_status: Literal["not_configured", "missing", "current", "stale", "invalid"] | None = None
    installed_coordinate: AcceptedCoordinate | None = None
    drift_observations: tuple[NextDriftObservation, ...] | None = None
    source_observations: tuple[NextSourceObservation, ...] | None = None
    presentation_policy: PresentationPolicyAny | None = None
    presentation_policy_notes: tuple[PresentationPolicyNote, ...] = ()
    projection_coverage: ProjectionCoverageObservation | None = None
    # Catalog entries whose bound workspace file is missing; each is a repair row.
    missing_bindings: tuple[NextMissingBinding, ...] = ()

    @field_validator("missing_bindings")
    @classmethod
    def _missing(cls, value: tuple[NextMissingBinding, ...]) -> tuple[NextMissingBinding, ...]:
        ids = tuple(item.source_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next missing bindings must be sorted and unique by source_id")
        return value

    @field_validator("drift_observations")
    @classmethod
    def _drift(
        cls,
        value: tuple[NextDriftObservation, ...] | None,
    ) -> tuple[NextDriftObservation, ...] | None:
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
        value: tuple[NextSourceObservation, ...] | None,
    ) -> tuple[NextSourceObservation, ...] | None:
        if value is None:
            return None
        ids = tuple(item.source_id for item in value)
        if ids != tuple(sorted(set(ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("next source observations must be sorted and unique by source_id")
        return value

    @model_validator(mode="after")
    def _floor_shape(self) -> "NextWorkspaceObservation":
        if self.floor_status == "current" and self.installed_coordinate is None:
            raise ValueError("a current floor observation requires its installed coordinate")
        return self


class NextRequest(_StrictNextModel):
    tag: Literal["playbill-next-request-v2"] = "playbill-next-request-v2"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: CoverageAccessProfile
    expiring_within: CanonicalDuration = CanonicalDuration(
        microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS
    )
    workspace_observation: NextWorkspaceObservation | None = None
    # The result_digest of a queue this caller has already seen. A digest this
    # process still remembers yields only the rows that are new since it; one it
    # does not -- a restart, an eviction, a digest from elsewhere -- yields the
    # whole queue, which is always a correct answer to "what is outstanding".
    since_result_digest: str | None = None
    # One page of the answer. A cursor continues the answer its first page
    # began: it carries that page's evaluation instant, coordinate, attestation
    # head and delta base, which supersede the request's own, so a caller whose
    # clock or head has moved still reads the same queue -- or is refused.
    limit: int = Field(default=NEXT_DEFAULT_LIMIT, ge=1, le=NEXT_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=2048)
    # Who reads the queue, so each repair is one this caller can run: the
    # surface picks the syntax a repair renders in (a CLI command, or an MCP
    # tool call), and an MCP caller lists the tools its session advertises, so
    # a row whose repair is a tool it lacks keeps its place with the repair
    # withheld and `repair_requires` naming what running it needs.
    # Absent, rows render as CLI commands.
    caller_surface: Literal["cli", "mcp", "sdk"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    caller_tools: tuple[str, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    at_attestation_head_digest: str | None = None

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("at_attestation_head_digest")
    @classmethod
    def _attestation_head(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


def validate_playbill_next_request(
    value: NextRequest | Mapping[str, object],
) -> NextRequest:
    if isinstance(value, NextRequest):
        return value
    try:
        return NextRequest.model_validate(value)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        if "access_profile" in roots:
            error: type[NextError] = NextAccessProfileInvalid
        elif "workspace_observation" in roots:
            error = NextWorkspaceObservationInvalid
        elif "cursor" in roots:
            error = NextCursorMismatch
        else:
            error = NextAcceptedStateInvalid
        raise error(f"{error.code}: {validation_summary(exc)}") from exc


class PlaybillNextRepairV1(_StrictNextModel):
    operation: Annotated[NextRepairOperation, CurrentCode]
    target: str
    required_change: str
    arguments: object = Field(default_factory=dict)
    # Runnable operations are dotted CLI paths whose arguments are their options.
    # Their command is composed from the digested fields beside it, so it stays
    # deterministic inside the item_id and result_digest preimages. Hand edits
    # instead carry a target and required change and never claim a command.
    command: str | None = None
    # Other repairs for the same row when the agent judges which applies; absent
    # (and out of the item_id preimage) on a row with one repair.
    alternatives: tuple[PlaybillNextRepairV1, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    # Operands only the caller holds -- why (``because``), the new text
    # (``file``), a fresh observation (``capture``) -- that the command shows as
    # placeholders (TEXT, FILE#ANCHOR, CAP) to replace. Absent (and out of the
    # item_id preimage) when the command names every operand.
    operands_to_add: tuple[NextRepairOperand, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @field_validator("alternatives")
    @classmethod
    def _flat_alternatives(
        cls, value: tuple[PlaybillNextRepairV1, ...]
    ) -> tuple[PlaybillNextRepairV1, ...]:
        if any(item.alternatives for item in value):
            raise ValueError("a repair alternative carries no alternatives of its own")
        return value

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


#: The permission tiers a repair can need, by name (``PermissionMode`` lowercased).
NextRepairTier: TypeAlias = Literal["read_only", "governed_write", "graph_write", "admin"]
_TIER_BY_RUNG: tuple[NextRepairTier, ...] = ("read_only", "governed_write", "graph_write", "admin")


class PlaybillNextRepairRequirementV1(_StrictNextModel):
    """What running a withheld repair needs that this caller does not have.

    A row whose repair this caller cannot run stays in the queue: the work is
    real whoever reads it. Only the repair is withheld, and this says why --
    the permission ``tier`` the repair's ``tool`` runs at, and/or the MCP tool
    ``profile`` that advertises it (``full`` advertises every tool; the CLI
    runs every repair a tier allows). ``authoring`` is a caller that cannot
    author on this instance at all -- an unbound credential, a principal that
    is not configured, registered or active, or a decommissioned instance --
    and ``authoring_refusal`` carries whoami's code, detail and repair.
    """

    tag: Literal["playbill-next-repair-requirement-v1"] = "playbill-next-repair-requirement-v1"
    operation: Annotated[NextRepairOperation, CurrentCode]
    tool: str
    tier: NextRepairTier
    profile: Literal["full"] | None = Field(default=None, exclude_if=lambda value: value is None)
    because: tuple[NextRepairGate, ...] = Field(min_length=1)
    authoring_refusal: AuthoringRefusal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _profile(self) -> "PlaybillNextRepairRequirementV1":
        if ("profile" in self.because) != (self.profile is not None):
            raise ValueError("a next repair requirement names a profile exactly when it needs one")
        if ("authoring" in self.because) != (self.authoring_refusal is not None):
            raise ValueError(
                "a next repair requirement names the authoring refusal exactly when it needs one"
            )
        return self


def _repair_or_requirement(
    repair: "PlaybillNextRepairV1 | None",
    requires: PlaybillNextRepairRequirementV1 | None,
) -> None:
    if (repair is None) == (requires is None):
        raise ValueError("a next row carries its repair or what running it requires, not both")


class PlaybillNextFindingV1(_StrictNextModel):
    """One more finding about the same underlying fact as the row that carries it."""

    tag: Literal["playbill-next-finding-v1"] = "playbill-next-finding-v1"
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: tuple[str, ...] = ()
    detail: object = Field(default_factory=dict)
    #: ``None`` when this caller cannot run it; ``repair_requires`` then says why.
    repair: PlaybillNextRepairV1 | None
    repair_requires: PlaybillNextRepairRequirementV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _withheld(self) -> "PlaybillNextFindingV1":
        _repair_or_requirement(self.repair, self.repair_requires)
        return self


class PlaybillNextItemV1(_StrictNextModel):
    tag: Literal["playbill-next-item-v1"] = "playbill-next-item-v1"
    item_id: str
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: tuple[str, ...] = ()
    detail: object = Field(default_factory=dict)
    #: ``None`` when this caller's surface, tool profile or permission tier cannot
    #: run it: the row stays, and ``repair_requires`` says what running it needs.
    repair: PlaybillNextRepairV1 | None
    # The row's other findings about the same block, source, document,
    # evidence or conflicted slot, each keeping its own reason, detail and
    # repair. Absent on a row that stands alone, so its bytes do not change.
    findings: tuple[PlaybillNextFindingV1, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    repair_requires: PlaybillNextRepairRequirementV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
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
        _repair_or_requirement(self.repair, self.repair_requires)
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
    "compiler": frozenset(
        {"current", "upgrade_available", "no_upgrade_path", "claim_type_upgrade_available"}
    ),
    "line_dispatch": frozenset({"not_observed", "idle", "waiting", "due"}),
    "consumers": frozenset({"not_observed", "not_running", "current", "lagging", "stalled"}),
    "triggers": frozenset({"not_observed", "scheduled", "unscheduled"}),
}


class PlaybillNextHealthV1(_StrictNextModel):
    """One environment facet: its state, what it saw, and the repair if it needs one."""

    tag: Literal["playbill-next-health-v1"] = "playbill-next-health-v1"
    state: str
    detail: object = Field(default_factory=dict)
    repair: PlaybillNextRepairV1 | None = None
    #: The facet needs a repair this caller's surface, tool profile or
    #: permission tier cannot perform, so its repair was dropped. The facet
    #: itself is kept.
    repair_hidden: bool = Field(default=False, exclude_if=lambda value: not value)
    #: What running the withheld repair needs; set exactly when ``repair_hidden``.
    repair_requires: PlaybillNextRepairRequirementV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _hidden_repair(self) -> "PlaybillNextHealthV1":
        if self.repair_hidden and self.repair is not None:
            raise ValueError("a next status facet hides its repair or carries it, not both")
        if self.repair_hidden != (self.repair_requires is not None):
            raise ValueError("a next status facet says what its hidden repair requires")
        return self


class PlaybillNextStatusV1(_StrictNextModel):
    """The environment the queue was read in, beside the work rather than in it.

    These are conditions of the instance and its workspace -- a decommissioned
    instance, an unexported floor, a lagging ledger mirror, an unavailable
    provider lane, an incomplete Procedure catalog, a compiler behind the
    running one, Line occurrences waiting on dispatch, built-in workers behind
    on what they report, an internal action no Trigger schedules -- not work
    items about accepted state. `blocking` is set only when no write can succeed.
    """

    tag: Literal["playbill-next-status-v1"] = "playbill-next-status-v1"
    blocking: bool
    instance: PlaybillNextHealthV1
    floor: PlaybillNextHealthV1
    ledger_mirror: PlaybillNextHealthV1
    provider_lane: PlaybillNextHealthV1
    procedure_catalog: PlaybillNextHealthV1
    compiler: PlaybillNextHealthV1
    line_dispatch: PlaybillNextHealthV1
    consumers: PlaybillNextHealthV1
    triggers: PlaybillNextHealthV1
    #: Rows parked by a current ``unsure`` attestation whose basis is unchanged.
    #: No row is left out for the caller: one whose repair this caller's
    #: surface, tool profile or permission tier cannot run is kept with its
    #: repair withheld and ``repair_requires`` set.
    held: int = Field(default=0, ge=0)
    #: Present only while the earlier-refusal index lags the accepted head, read
    #: after this answer's own maintenance: receipt counts on proposal rows and
    #: settled-item counts on Line rows cover retirements through that generation.
    prior_refusal_coverage: PriorRefusalCoverage | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _states(self) -> "PlaybillNextStatusV1":
        for facet, states in _HEALTH_STATES.items():
            if getattr(self, facet).state not in states:
                raise ValueError(f"next {facet} status has an unknown state")
        if self.blocking != (self.instance.state == "decommissioned"):
            raise ValueError("next status blocks exactly a decommissioned instance")
        return self


class PlaybillNextResultV2(_StrictNextModel):
    tag: Literal["playbill-next-result-v2"] = "playbill-next-result-v2"
    coordinate: AcceptedCoordinate
    evaluation_time: datetime
    observed_domains: tuple[NextDomain, ...]
    unobserved_domains: tuple[NextDomain, ...]
    status: PlaybillNextStatusV1
    items: tuple[PlaybillNextItemV1, ...]
    # Every row the whole answer carries -- the queue, or on a delta its
    # changed rows -- of which `items` is one page. Neither this count nor the
    # cursor is in the digest preimage: `result_digest` names the whole queue
    # on every page, which is what binds a cursor to it.
    total_items: int = Field(ge=0)
    next_cursor: str | None = None
    result_digest: str
    # Set only on a delta. The carried items are the deterministic symmetric
    # difference from that earlier queue while `result_digest` remains the
    # current whole-queue cursor. The server remembers that full queue so the
    # digest remains usable on the next call.
    delta_since: str | None = None
    attestation_head_digest: str
    removed_item_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        exclude_if=lambda value: not value,
    )

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("result_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

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
    def _shape(self) -> "PlaybillNextResultV2":
        if set(self.observed_domains).intersection(self.unobserved_domains):
            raise ValueError("next observed and unobserved domains overlap")
        if set((*self.observed_domains, *self.unobserved_domains)) != set(_ALL_DOMAINS):
            raise ValueError("next result must account for every observation domain")
        if self.items != tuple(sorted(self.items, key=_item_sort_key)):
            raise ValueError("next items do not follow the deterministic order")
        if len(self.items) > self.total_items:
            raise ValueError("a next page cannot carry more rows than its answer")
        if self.next_cursor is not None and len(self.items) == self.total_items:
            raise ValueError("a next answer carried whole has no further page")
        if self.whole_queue and self.result_digest != playbill_next_result_digest(self):
            raise ValueError("next result digest does not reproduce")
        if self.removed_item_ids and self.delta_since is None:
            raise ValueError("removed next item IDs are valid only on a delta")
        carried_ids = frozenset(item.item_id for item in self.items)
        if not set(self.removed_item_ids).issubset(carried_ids):
            raise ValueError("removed next item IDs must name carried delta rows")
        return self

    @property
    def whole_queue(self) -> bool:
        """Whether `items` is the entire queue the digest names: no delta, no paging."""

        return self.delta_since is None and len(self.items) == self.total_items


_REPAIR_COMMAND_PATHS: Mapping[str, str] = {
    "cruxible.authoring.example": "authoring example",
    "cruxible.authoring.submit": "authoring submit",
    "cruxible.authoring.bind": "authoring bind",
    "cruxible.claim.retire": "retire",
    "cruxible.rebind": "rebind",
    "cruxible.get": "get",
    "cruxible.set": "set",
    "cruxible.write": "write",
    "cruxible.floor.export": "floor export",
    "cruxible.block.depublish": "block depublish",
    "cruxible.block.detach": "block detach",
    "cruxible.block.repin": "block repin",
    "cruxible.block.sync": "block sync",
    "cruxible.files.propose": "files propose",
    "cruxible.proposal.readmit": "proposal readmit",
    "cruxible.proposal.approve": "proposal approve",
    "cruxible.proposal.activate": "proposal activate",
    "cruxible.compiler.upgrade": "compiler upgrade",
    "cruxible.claim-type.upgrade": "claim-type upgrade",
    "cruxible.line.enable": "line enable",
    "cruxible.line.evaluate": "line evaluate",
    "cruxible.line.dispatch": "line dispatch",
    "cruxible.inbox.list": "inbox list",
    "cruxible.prediction.settle": "prediction settle",
    "cruxible.kit.add": "kit add",
}

# Each of these needs a local file. The queue knows the path only if the row
# carried it, so the placeholder is filled from the arguments when they name it
# and dropped -- with the flag that introduces it -- when they do not. A bare
# `PAYLOAD_FILE` left in the line is not a hint, it is an unrunnable command
# presented as a runnable one, which is the one thing `command` must never be.
_REPAIR_COMMAND_OPERANDS: Mapping[str, tuple[str, ...]] = {
    "cruxible.authoring.bind": ("--payload-file", "PAYLOAD_FILE"),
}
_REPAIR_COMMAND_PLACEHOLDERS: Mapping[str, str] = {
    "PAYLOAD_FILE": "payload_file",
}
_ATTESTATION_REPAIR_EXAMPLES: Mapping[str, str] = {
    "cite_supporting_evidence": "claim-cite-supporting-evidence",
    "adjudicate_unreviewed_evidence": "claim-adjudicate-unreviewed-evidence",
}


def _revision_name(source: str) -> str:
    """The proposal name a modified-Document repair suggests for its source."""

    return f"revise-{source}"


def _repair_paths(values: Mapping[str, object]) -> list[str]:
    """The workspace pages a page-scoped repair names, or none."""

    paths = values.get("paths")
    if not isinstance(paths, (list, tuple)):
        return []
    return [path for path in paths if isinstance(path, str) and path]


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


NextCallerSurface: TypeAlias = Literal["cli", "mcp", "sdk"]

#: Every repair operation's served door: the tool that performs it. The tool's
#: entry in ``TOOL_PERMISSIONS`` is the tier the repair needs on every surface,
#: and an MCP caller must also advertise the tool. ``None`` is a repair with no
#: served door to gate. Block repin and sync run in the client-side adapter on
#: every surface (MCP included): repin declares the block at the instance, and
#: sync reads its backings.
_REPAIR_TOOLS: Mapping[str, str | None] = {
    # The template starts the repair; compile is the door that performs it.
    "cruxible.authoring.example": "cruxible_authoring_compile",
    # A payload the queue filled in full: submitting it is the repair.
    "cruxible.authoring.submit": "cruxible_authoring_submit",
    "cruxible.authoring.bind": "cruxible_authoring_bind",
    "cruxible.claim.retire": "cruxible_retire",
    # A re-bind is one change kind of the typed write; it has no tool of its own.
    "cruxible.rebind": "cruxible_write",
    "cruxible.get": "cruxible_get",
    "cruxible.set": "cruxible_set",
    "cruxible.write": "cruxible_write",
    "cruxible.floor.export": "cruxible_floor_export",
    "cruxible.block.depublish": "cruxible_block_depublish",
    "cruxible.block.detach": "cruxible_block_detach",
    "cruxible.block.repin": "cruxible_block_repin",
    "cruxible.block.sync": "cruxible_block_sync",
    "cruxible.files.propose": "cruxible_files_propose",
    "cruxible.proposal.readmit": "cruxible_proposal_readmit",
    "cruxible.proposal.approve": "cruxible_proposal_approve",
    "cruxible.proposal.activate": "cruxible_proposal_activate",
    "cruxible.compiler.upgrade": "cruxible_compiler_upgrade",
    "cruxible.claim-type.upgrade": "cruxible_claim_type_upgrade",
    "cruxible.line.enable": "cruxible_line_enable",
    "cruxible.line.evaluate": "cruxible_line_evaluate",
    "cruxible.line.dispatch": "cruxible_line_dispatch",
    "cruxible.inbox.list": "cruxible_inbox_list",
    "cruxible.prediction.settle": "cruxible_prediction_settle",
    "cruxible.kit.add": "cruxible_kit_add",
    "hand_edit": None,
}
_GOVERNED_WRITE_RUNG = 1
#: Repairs whose served door writes nothing and so never meets the principal
#: boundary; every other served repair is refused to a caller that cannot author.
_AUTHORING_FREE_REPAIRS = frozenset(
    {"cruxible.floor.export", "cruxible.get", "cruxible.inbox.list"}
)


def _tool_rung(tool: str) -> int:
    """The least caller rung (``PermissionMode.value - 1``) a tool runs at."""

    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS

    return int(TOOL_PERMISSIONS[tool]) - 1


#: The most contenders a contest row offers a keep-one option for.
_MAX_CONTEST_OPTIONS = 8


def _restated_set(values: Mapping[str, object]) -> dict[str, object] | None:
    """The set call a `cruxible.set` repair names, from its arguments, or None."""

    subject, field, value = values.get("subject"), values.get("field"), values.get("value")
    if not isinstance(subject, str) or not isinstance(field, str):
        return None
    if not isinstance(value, str | int | bool):
        return None
    restated: dict[str, object] = {"subject": subject, "field": field, "value": value}
    role = values.get("role")
    if isinstance(role, str):
        restated["role"] = role
    capture = values.get("capture_digest")
    contract = values.get("evidence_contract")
    if isinstance(capture, str):
        restated["evidence"] = {"kind": "capture", "capture": capture}
    elif isinstance(contract, str):
        restated["evidence"] = {"kind": "contract", "contract": contract}
    return restated


def _write_changes(values: Mapping[str, object]) -> list[dict[str, object]]:
    changes = values.get("changes")
    if not isinstance(changes, list | tuple):
        return []
    return [dict(item) for item in changes if isinstance(item, Mapping)]


def _submitted_payload(values: Mapping[str, object]) -> Mapping[str, object] | None:
    """The authoring input a `cruxible.authoring.submit` repair carries in full, or None."""

    payload = values.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("kind"), str):
        return None
    return payload


#: SDK `ChangeSetDraft.line` keywords for a Line input's fields, with each default.
_SDK_LINE_KEYWORDS: tuple[tuple[str, str, object], ...] = (
    ("name", "name", None),
    ("procedure_name", "procedure", None),
    ("acquisition_policy_name", "acquisition_policy", None),
    ("max_authority", "max_authority", None),
    ("trigger_input", "trigger_input", None),
    ("parameters", "parameters", {}),
    ("budgets", "budgets", None),
    ("occurrence_epoch", "occurrence_epoch", 1),
    ("retire", "retire", False),
)


def _rebind_section(values: Mapping[str, object]) -> str | None:
    """A section citation's ``FILE#Heading/Path``, when the repair names it."""

    file, section = values.get("section_file"), values.get("section")
    if not isinstance(file, str) or not file or not isinstance(section, list | tuple):
        return None
    if not all(isinstance(item, str) for item in section):
        return None
    return f"{file}#{'/'.join(section)}"


def _rebind_change(values: Mapping[str, object]) -> dict[str, object] | None:
    """The typed ``rebind`` change a re-bind repair names, as far as the row knows it."""

    claim_id = values.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        return None
    change: dict[str, object] = {"op": "rebind", "target": claim_id}
    citation = values.get("citation")
    if isinstance(citation, str) and citation:
        change["citation"] = citation
    file, section = values.get("section_file"), values.get("section")
    if isinstance(file, str) and isinstance(section, list | tuple):
        change["evidence"] = {"kind": "section", "file": file, "section": list(section)}
    return change


def _cli_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _claim_set_arguments(
    claim: ClaimArtifactAny, claim_type: ClaimType | None
) -> dict[str, object] | None:
    """The `set` that restates one live Claim, or None where `set` cannot say it.

    A repair that asks for a Claim to be stated again -- on new evidence, or as
    an acknowledgement -- is the `set` verb on its Subject and field, which the
    default profile serves. It cannot restate a qualified Claim, a derivation
    (only its Procedure produces one), a many-valued field (`set` replaces one
    value), or an exact-content or structured value (no scalar to pass); those
    keep their authoring repair.
    """

    statement = claim.statement
    if claim_type is None or claim_type.cardinality != "one":
        return None
    if statement.qualifier is not None or statement.role == "derivation":
        return None
    obj = statement.object
    value: object
    if isinstance(obj, LiteralClaimObject) and isinstance(obj.value, str | int | bool):
        value = obj.value
    elif isinstance(obj, SubjectClaimObject):
        value = obj.address.artifact_path.removeprefix("subjects/").removesuffix(".json")
    else:
        return None
    return {
        "subject": statement.subject.artifact_path.removeprefix("subjects/").removesuffix(".json"),
        "field": statement.predicate,
        "value": value,
        "role": statement.role,
    }


def _claim_type_at(
    instance: PlaybillInstance, git_oid: str, predicate: str, memo: dict[str, ClaimType | None]
) -> ClaimType | None:
    if predicate not in memo:
        path = claim_type_path(predicate)
        content = instance.blob_at(git_oid, path)
        memo[predicate] = None if content is None else parse_claim_type(content, path=path)
    return memo[predicate]


def _live_claim_set_arguments(
    instance: PlaybillInstance, git_oid: str, claim_id: str, memo: dict[str, ClaimType | None]
) -> dict[str, object] | None:
    """`_claim_set_arguments` for a Claim named by ID, when it is live at ``git_oid``."""

    path = claim_path(claim_id)
    content = instance.blob_at(git_oid, path)
    if content is None:
        return None
    claim = parse_claim(content, path=path)
    if claim.lifecycle.state != "live":
        return None
    return _claim_set_arguments(
        claim, _claim_type_at(instance, git_oid, claim.statement.predicate, memo)
    )


def _restating_repair(
    fallback: NextRepairOperation,
    *,
    target: str,
    required_change: str,
    arguments: Mapping[str, object],
    set_arguments: Mapping[str, object] | None,
) -> PlaybillNextRepairV1:
    """A repair through `set` when it can restate the Claim, else its authoring door."""

    if set_arguments is None:
        return PlaybillNextRepairV1(
            operation=fallback,
            target=target,
            required_change=required_change,
            arguments=dict(arguments),
        )
    return PlaybillNextRepairV1(
        operation="cruxible.set",
        target=target,
        required_change=required_change,
        arguments={**arguments, **set_arguments},
    )


#: The contracts a Claim's own authoring produces; they never back a `captured` field.
_OWN_WORDS_CONTRACTS = frozenset(
    {DIRECT_SELF_ASSERTED_CONTRACT_ID, COORDINATOR_SELF_SOURCE_CONTRACT_ID}
)


def _role_contracts(claim_type: ClaimType, role: str) -> tuple[str, ...]:
    """The CaptureContracts (public names) the rules admitting ``role`` name, in byte order.

    Read off the ClaimType alone (rules that name contracts by identity), so a
    row built from it depends only on that Claim's own definition.
    """

    names = {
        item.target.name
        for rule in claim_type.evidence_admission_policy.rules
        if isinstance(rule, ClaimEvidenceAdmissionIdentityRule) and role in rule.claim_roles
        for item in rule.capture_contracts
    }
    return tuple(
        sorted(
            (public_name(name) for name in names if name not in _OWN_WORDS_CONTRACTS),
            key=lambda item: item.encode("utf-8"),
        )
    )


def _role_unadmitted(claim_type: ClaimType | None, role: str) -> tuple[str, ...] | None:
    """The roles a ClaimType's evidence rules admit, when none of them is ``role``."""

    if claim_type is None:
        return None
    roles: list[str] = []
    for rule in claim_type.evidence_admission_policy.rules:
        roles.extend(item for item in rule.claim_roles if item not in roles)
    return tuple(roles) if roles and role not in roles else None


def _cited_first(admitted: tuple[str, ...], cited: tuple[str, ...]) -> tuple[str, ...]:
    """The admitted contracts with the cited Captures' own first, each group in its order."""

    first = tuple(name for name in cited if name in admitted)
    return (*dict.fromkeys(first), *(name for name in admitted if name not in first))


def _capture_contract_names(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    capture_digests: Iterable[str],
) -> tuple[str, ...]:
    """The public names of the CaptureContracts these Captures were observed under.

    One keyed projection lookup and one keyed contract-version lookup per
    Capture; byte order. A Capture the projection does not hold names nothing.
    """

    digests = tuple(dict.fromkeys(capture_digests))
    if not digests:
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        contract_digests = {
            str(row[0])
            for digest in digests
            if (
                row := projection.typed.connection.execute(
                    "SELECT contract_digest FROM captures WHERE capture_digest=?", (digest,)
                ).fetchone()
            )
            is not None
        }
    public = AcceptedCoordinate.from_internal(coordinate)
    names = {
        public_name(found.contract.identity.name)
        for digest in contract_digests
        if (found := instance.accepted_capture_contract_version(public, digest)) is not None
    }
    return tuple(sorted(names, key=lambda item: item.encode("utf-8")))


def _expiring_lead(expiration: EvidenceFreshnessExpiration, within: CanonicalDuration) -> timedelta:
    """How long before ``expiration`` next warns: the request's lead, at most horizon/4.

    The horizon runs from the Capture's observation (or the re-observation that
    set its expiry) to its expiry, so a short-lived Capture is not "expiring"
    for most of its life under a long default lead.
    """

    observed = expiration.reobserved_at or expiration.observed_at
    return min(timedelta(microseconds=within.microseconds), (expiration.expires_at - observed) / 4)


def _retire_alternative(target: str, claim_id: str) -> PlaybillNextRepairV1:
    """Retiring the Claim, offered beside a repair that keeps it: the agent judges."""

    return PlaybillNextRepairV1(
        operation="cruxible.claim.retire",
        operands_to_add=("because",),
        target=target,
        required_change="retire_claim_with_attribution",
        arguments={"claim_id": claim_id},
    )


def _evidence_repair(
    fallback: NextRepairOperation,
    *,
    target: str,
    required_change: str,
    claim_id: str,
    claim_type: ClaimType | None,
    set_arguments: Mapping[str, object] | None,
    cited_contracts: tuple[str, ...] = (),
) -> PlaybillNextRepairV1:
    """A repair that restates a Claim on evidence its field admits, or retires it.

    A field that requires captured evidence refuses a restatement in the
    writer's own words, so the restating ``set`` cites a Capture: one per
    contract the Claim's role admits (``--evidence-contract``), with retire as
    the last alternative. ``cited_contracts`` (the expiring or expired
    Captures' own contracts) come first, then the other admitted ones. Any
    other field keeps the restating ``set`` (or the authoring door), with
    retire beside it.
    """

    retire = _retire_alternative(target, claim_id)
    if (
        set_arguments is not None
        and claim_type is not None
        and effective_evidence_requirement(claim_type) == "captured"
    ):
        role = str(set_arguments.get("role", ""))
        options = [
            PlaybillNextRepairV1(
                operation="cruxible.set",
                target=target,
                required_change=required_change,
                arguments={"claim_id": claim_id, **set_arguments, "evidence_contract": contract},
            )
            for contract in _cited_first(_role_contracts(claim_type, role), cited_contracts)
        ]
        if options:
            return options[0].model_copy(update={"alternatives": (*options[1:], retire)})
        set_arguments = None
    primary = _restating_repair(
        fallback,
        target=target,
        required_change=required_change,
        arguments={"claim_id": claim_id},
        set_arguments=set_arguments,
    )
    return primary.model_copy(update={"alternatives": (retire,)})


def _contradicting_capture_repair(
    target: str,
    *,
    claim_id: str,
    capture_digest: str,
    required_change: str,
    set_arguments: Mapping[str, object] | None,
) -> PlaybillNextRepairV1:
    """Read the contradicting Capture; then restate the Claim on its own evidence, or retire it.

    The restatement never cites the contradicting Capture: citing it would back
    the Claim with the evidence that contradicts it.
    """

    alternatives: list[PlaybillNextRepairV1] = []
    if set_arguments is not None:
        alternatives.append(
            PlaybillNextRepairV1(
                operation="cruxible.set",
                target=target,
                required_change="restate_claim",
                arguments={"claim_id": claim_id, **set_arguments},
            )
        )
    alternatives.append(_retire_alternative(target, claim_id))
    return PlaybillNextRepairV1(
        operation="cruxible.get",
        target=target,
        required_change=required_change,
        arguments={"claim_id": claim_id, "ref": f"Capture:{capture_digest}"},
        alternatives=tuple(alternatives),
    )


#: What each operand the caller adds reads as in a command, for it to replace.
_OPERAND_PLACEHOLDERS: Mapping[NextRepairOperand, str] = {
    "because": "TEXT",
    "file": "FILE#ANCHOR",
    "capture": "CAP",
}


def _because(operands: tuple[NextRepairOperand, ...]) -> dict[str, object]:
    return {"because": _OPERAND_PLACEHOLDERS["because"]} if "because" in operands else {}


def _mcp_call(tool: str, **arguments: object) -> str:
    rendered = ", ".join(f"{key}={json.dumps(value)}" for key, value in arguments.items())
    return f"{tool}({rendered})"


def _mcp_repair_call(
    operation: NextRepairOperation,
    *,
    arguments: object,
    operands: tuple[NextRepairOperand, ...] = (),
) -> str | None:
    """Render one repair as the MCP tool call that performs it, or None.

    A repair whose operands are local files or structured bases the queue does
    not hold renders no call rather than a call that cannot be made.
    """

    values = arguments if isinstance(arguments, Mapping) else {}

    def text(key: str) -> str | None:
        value = values.get(key)
        return value if isinstance(value, str) and value else None

    if operation == "cruxible.compiler.upgrade":
        return _mcp_call("cruxible_compiler_upgrade")
    if operation == "cruxible.claim-type.upgrade":
        # The bare request previews every ClaimType the upgrade would change.
        return _mcp_call("cruxible_claim_type_upgrade", request={})
    if operation == "cruxible.line.enable" and text("line"):
        return _mcp_call("cruxible_line_enable", line=text("line"))
    if operation == "cruxible.line.evaluate" and text("line") and text("since") and text("until"):
        return _mcp_call(
            "cruxible_line_evaluate",
            line=text("line"),
            request={"since": text("since"), "until": text("until")},
        )
    if operation == "cruxible.line.dispatch" and text("line"):
        limit = values.get("limit")
        request = {"limit": limit} if isinstance(limit, int) and limit > 1 else {}
        return _mcp_call("cruxible_line_dispatch", line=text("line"), request=request)
    if operation == "cruxible.inbox.list" and text("line"):
        return _mcp_call("cruxible_inbox_list", line=text("line"), status="unread")
    if operation == "cruxible.kit.add" and text("reference"):
        return _mcp_call("cruxible_kit_add", reference=text("reference"))
    if operation == "cruxible.prediction.settle" and text("prediction_id"):
        # The observation is the settler's to choose: its Claim ID is the one
        # argument left to add.
        return _mcp_call("cruxible_prediction_settle", prediction_id=text("prediction_id"))
    if operation == "cruxible.authoring.example" and text("example"):
        return _mcp_call("cruxible_authoring_example", name=text("example"))
    if operation == "cruxible.authoring.submit" and (payload := _submitted_payload(values)):
        return _mcp_call("cruxible_authoring_submit", payload=dict(payload))
    if operation == "cruxible.proposal.readmit" and text("proposal_id"):
        return _mcp_call("cruxible_proposal_readmit", proposal_id=text("proposal_id"))
    if operation == "cruxible.proposal.activate" and text("proposal_id"):
        return _mcp_call("cruxible_proposal_activate", proposal_id=text("proposal_id"))
    if operation == "cruxible.proposal.approve" and text("proposal_id") and text("signer_id"):
        return _mcp_call(
            "cruxible_proposal_approve",
            proposal_id=text("proposal_id"),
            signer_id=text("signer_id"),
        )
    if operation == "cruxible.block.depublish" and text("source_id") and text("block_id"):
        return _mcp_call(
            "cruxible_block_depublish",
            file_id=text("source_id"),
            block_id=text("block_id"),
        )
    if operation == "cruxible.block.repin" and text("source_id") and text("block_id"):
        return _mcp_call("cruxible_block_repin", file=text("source_id"), block=text("block_id"))
    if operation == "cruxible.block.sync" and values.get("all") is True:
        return _mcp_call("cruxible_block_sync", all_files=True)
    if operation == "cruxible.block.detach" and _repair_paths(values):
        return _mcp_call("cruxible_block_detach", files=_repair_paths(values))
    if operation == "cruxible.floor.export":
        return _mcp_call("cruxible_floor_export", mode="write")
    if operation == "cruxible.files.propose" and (source := text("source_id")):
        return _mcp_call(
            "cruxible_files_propose", file=source, proposal_name=_revision_name(source)
        )
    if operation == "cruxible.claim.retire" and text("claim_id"):
        # Why it ends is the retirer's to say: `because` is the argument left to add.
        return _mcp_call("cruxible_retire", target=text("claim_id"), **_because(operands))
    if operation == "cruxible.set" and (restated := _restated_set(values)) is not None:
        # `because` (and the file evidence a recapture needs) is the caller's to add.
        return _mcp_call("cruxible_set", **restated)
    if operation == "cruxible.write" and (changes := _write_changes(values)):
        return _mcp_call("cruxible_write", changes=changes)
    if operation == "cruxible.get" and text("ref"):
        return _mcp_call("cruxible_get", ref=text("ref"))
    if operation == "cruxible.rebind" and (change := _rebind_change(values)) is not None:
        # A re-bind is one change of the typed write; `because` (and a file
        # citation's new text, or a fresh Capture) is the caller's to add.
        if "file" in operands:
            change["evidence"] = {"kind": "file", "file": _OPERAND_PLACEHOLDERS["file"]}
        elif "capture" in operands:
            change["evidence"] = {"kind": "capture", "capture": _OPERAND_PLACEHOLDERS["capture"]}
        return _mcp_call("cruxible_write", changes=[change], **_because(operands))
    return None


def _python_literal(value: object) -> str:
    """Render a str, bool, None, int, or list/dict of those as Python source.

    A JSON string literal is a valid Python one (every JSON escape is a Python
    escape), so strings keep their double quotes; JSON's `true`, `false` and
    `null` are not Python, so they are rendered here.
    """

    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{_python_literal(key)}: {_python_literal(item)}" for key, item in value.items()
            )
            + "}"
        )
    raise TypeError(f"no Python literal for an SDK repair operand of type {type(value).__name__}")


def _sdk_call(target: str, *positional: object, **keywords: object) -> str:
    rendered = [_python_literal(value) for value in positional]
    rendered.extend(f"{key}={_python_literal(value)}" for key, value in keywords.items())
    return f"{target}({', '.join(rendered)})"


def _sdk_repair_call(
    operation: NextRepairOperation,
    *,
    arguments: object,
    operands: tuple[NextRepairOperand, ...] = (),
) -> str | None:
    """Render one repair as the `Cruxible` SDK call that performs it, or None.

    Like the CLI and MCP renderings, an operand only the caller holds -- the
    signer, the observation, the retirement reason -- is left for it to add. A
    repair the SDK facade has no method for, or whose operands are local files,
    renders no call rather than another surface's command.
    """

    values = arguments if isinstance(arguments, Mapping) else {}

    def text(key: str) -> str | None:
        value = values.get(key)
        return value if isinstance(value, str) and value else None

    if operation == "cruxible.line.enable" and (line := text("line")):
        return _sdk_call("cx.line", line) + ".enable()"
    if operation == "cruxible.line.evaluate" and (line := text("line")):
        since, until = text("since"), text("until")
        if since is None or until is None:
            return None
        return _sdk_call("cx.line", line) + _sdk_call(".evaluate", since=since, until=until)
    if operation == "cruxible.line.dispatch" and (line := text("line")):
        limit = values.get("limit")
        if isinstance(limit, int) and limit > 1:
            return _sdk_call("cx.line", line) + _sdk_call(".dispatch", limit=limit)
        return _sdk_call("cx.line", line) + ".dispatch()"
    if operation == "cruxible.inbox.list" and (line := text("line")):
        return "cx.inbox()" + _sdk_call(".list", line=line, status="unread")
    if operation == "cruxible.prediction.settle" and (prediction := text("prediction_id")):
        return _sdk_call("cx.settle", prediction)
    if operation == "cruxible.claim-type.upgrade":
        return _sdk_call("cx.upgrade_claim_types")
    if operation == "cruxible.authoring.example":
        example = text("example")
        return None if example is None else _sdk_call("authoring_example", example)
    if operation == "cruxible.authoring.submit":
        # The facade authors a Line as a changeset member; other kinds have
        # their own builders, which no repair names yet.
        payload = _submitted_payload(values)
        if payload is None or payload.get("kind") != "line":
            return None
        keywords = {
            keyword: payload[field]
            for field, keyword, default in _SDK_LINE_KEYWORDS
            if field in payload and payload[field] != default
        }
        return _sdk_call("cx.changes().line", **keywords) + ".submit()"
    if operation == "cruxible.proposal.activate" and (proposal := text("proposal_id")):
        return _sdk_call("cx.activate", proposal)
    if operation == "cruxible.proposal.approve" and (proposal := text("proposal_id")):
        handle = _sdk_call("cx.proposal", proposal)
        return f"{handle}.approve(reviewed={handle}.review())"
    if operation == "cruxible.claim.retire" and (claim := text("claim_id")):
        return _sdk_call("cx.retire", claim, **_because(operands))
    if operation == "cruxible.set" and (restated := _restated_set(values)) is not None:
        subject, field, value = (restated.pop(key) for key in ("subject", "field", "value"))
        return _sdk_call("cx.set", subject, field, value, **restated)
    if operation == "cruxible.write":
        changes = _write_changes(values)
        if len(changes) == 1 and changes[0].get("op") == "retire":
            return _sdk_call("cx.retire", changes[0]["target"])
        return None
    if operation == "cruxible.get" and (ref := text("ref")):
        return _sdk_call("cx.get", ref)
    if operation == "cruxible.rebind" and (claim := text("claim_id")):
        options: dict[str, object] = {}
        if citation := text("citation"):
            options["citation"] = citation
        if (section := _rebind_section(values)) is not None:
            options["section"] = section
        for operand in ("file", "capture"):
            if operand in operands:
                options[operand] = _OPERAND_PLACEHOLDERS[operand]
        return _sdk_call("cx.rebind", claim, **options, **_because(operands))
    if operation == "cruxible.block.repin" and (source := text("source_id")):
        block = text("block_id")
        if block is None:
            return None
        listed = values.get("claim")
        named = [values.get("claim_id"), *(listed if isinstance(listed, (list, tuple)) else ())]
        claims = [
            value.removeprefix("Claim:") for value in named if isinstance(value, str) and value
        ]
        if claims:
            return _sdk_call("cx.block.repin", source, block, claims=claims)
        return _sdk_call("cx.block.repin", source, block)
    if operation == "cruxible.block.sync" and values.get("all") is True:
        return _sdk_call("cx.block.sync", all=True)
    if operation == "cruxible.block.detach" and _repair_paths(values):
        return _sdk_call("cx.block.detach", *_repair_paths(values))
    if operation == "cruxible.block.depublish" and (source := text("source_id")):
        block = text("block_id")
        return (
            None
            if block is None
            else _sdk_call("cx.block.depublish", file_id=source, block_id=block)
        )
    return None


def _repair_command(
    operation: NextRepairOperation,
    *,
    arguments: object,
    surface: NextCallerSurface | None = None,
    operands: tuple[NextRepairOperand, ...] = (),
) -> str | None:
    """Compose the invocation for one repair operation on this surface.

    ``operands`` the caller must add render as their placeholders
    (``_OPERAND_PLACEHOLDERS``), so the command shows every required operand.
    """

    if operation == "hand_edit":
        return None
    if surface == "mcp":
        return _mcp_repair_call(operation, arguments=arguments, operands=operands)
    if surface == "sdk":
        return _sdk_repair_call(operation, arguments=arguments, operands=operands)
    parts = ["cruxible", _REPAIR_COMMAND_PATHS[operation]]
    values = arguments if isinstance(arguments, Mapping) else {}
    if operation == "cruxible.block.repin":
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
    elif operation == "cruxible.block.depublish":
        source_id = values.get("source_id")
        block_id = values.get("block_id")
        if isinstance(source_id, str) and isinstance(block_id, str):
            parts.extend([shlex.quote(source_id), shlex.quote(block_id)])
        else:
            return None
    elif operation == "cruxible.block.sync":
        if values.get("all") is True:
            parts.append("--all")
        else:
            return None
    elif operation == "cruxible.block.detach":
        paths = _repair_paths(values)
        if not paths:
            return None
        parts.extend(shlex.quote(path) for path in paths)
    elif operation == "cruxible.authoring.example":
        # The runnable step is the template that starts the payload; with no
        # example named, a bare `authoring example` lists every name.
        example = values.get("example")
        if isinstance(example, str) and example:
            parts.append(shlex.quote(example))
    elif operation == "cruxible.authoring.submit":
        # The payload is complete, so the command carries it on stdin rather
        # than naming a file the caller would have to write first.
        payload = _submitted_payload(values)
        if payload is None:
            return None
        parts.extend(["-", "<<<", shlex.quote(canonical_json(payload))])
    elif operation == "cruxible.compiler.upgrade":
        # No operands: the command targets the compiler the daemon runs, which
        # is the one this repair names in its facet's detail.
        pass
    elif operation == "cruxible.claim-type.upgrade":
        # No operands: the bare command previews every ClaimType it would change.
        pass
    elif operation == "cruxible.line.evaluate":
        line, since, until = values.get("line"), values.get("since"), values.get("until")
        if not all(isinstance(value, str) and value for value in (line, since, until)):
            return None
        parts.extend(
            [
                shlex.quote(str(line)),
                "--since",
                shlex.quote(str(since)),
                "--until",
                shlex.quote(str(until)),
                # One command: evaluate the gap, then dispatch what it enqueued.
                "--dispatch",
            ]
        )
    elif operation in {"cruxible.line.enable", "cruxible.line.dispatch"}:
        line = values.get("line")
        limit = values.get("limit")
        if not isinstance(line, str) or not line:
            return None
        parts.append(shlex.quote(line))
        if operation == "cruxible.line.dispatch" and isinstance(limit, int) and limit > 1:
            parts.extend(["--limit", str(limit)])
    elif operation == "cruxible.inbox.list":
        line = values.get("line")
        if not isinstance(line, str) or not line:
            return None
        parts.extend(["--line", shlex.quote(line), "--status", "unread"])
    elif operation == "cruxible.kit.add":
        # A preview by default: it shows what the add would do at the head now.
        reference = values.get("reference")
        if not isinstance(reference, str) or not reference:
            return None
        parts.append(shlex.quote(reference))
    elif operation == "cruxible.prediction.settle":
        # The daemon resolves the exact contract and window from the bound
        # window id; the observation is the settler's to choose, so its Claim
        # ID is the one operand left to add (`--observation CLM-...`).
        prediction_id = values.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id:
            return None
        parts.append(shlex.quote(prediction_id))
    elif operation in {"cruxible.proposal.readmit", "cruxible.proposal.activate"}:
        proposal_id = values.get("proposal_id")
        if not isinstance(proposal_id, str):
            return None
        parts.append(shlex.quote(proposal_id))
    elif operation == "cruxible.proposal.approve":
        # The signing key stays in the signer's client custody; the daemon
        # never learns its path, so `--key` is the one operand left to add.
        proposal_id = values.get("proposal_id")
        signer_id = values.get("signer_id")
        if not isinstance(proposal_id, str) or not isinstance(signer_id, str):
            return None
        parts.extend([shlex.quote(proposal_id), "--signer-id", shlex.quote(signer_id)])
    elif operation == "cruxible.set":
        restated = _restated_set(values)
        if restated is None:
            return None
        parts.extend(
            shlex.quote(_cli_scalar(restated[key])) for key in ("subject", "field", "value")
        )
        role = restated.get("role")
        if isinstance(role, str):
            parts.extend(["--role", shlex.quote(role)])
        evidence = restated.get("evidence")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("capture"), str):
            parts.extend(["--capture", shlex.quote(str(evidence["capture"]))])
        elif isinstance(evidence, Mapping) and isinstance(evidence.get("contract"), str):
            parts.extend(["--evidence-contract", shlex.quote(str(evidence["contract"]))])
    elif operation == "cruxible.write":
        changes = _write_changes(values)
        if not changes or any(item.get("op") != "retire" for item in changes):
            return None
        if len(changes) == 1:
            return f"cruxible retire {shlex.quote(str(changes[0]['target']))}"
        # Several retirements are one change set; `--because` is the operand left.
        return "cruxible write - <<< " + shlex.quote(canonical_json({"changes": changes}))
    elif operation == "cruxible.get":
        ref = values.get("ref")
        if not isinstance(ref, str) or not ref:
            return None
        parts.append(shlex.quote(ref))
    elif operation == "cruxible.rebind":
        # The new text is the re-binder's to choose: a file citation leaves
        # `--file FILE#ANCHOR` to add (as retire leaves `--because`); a section
        # citation names its section, which locates the text on its own.
        claim_id = values.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return None
        parts.append(shlex.quote(claim_id))
        citation = values.get("citation")
        if isinstance(citation, str) and citation:
            parts.extend(["--citation", shlex.quote(citation)])
        section = _rebind_section(values)
        if section is not None:
            parts.extend(["--section", shlex.quote(section)])
        if "file" in operands:
            parts.extend(["--file", _OPERAND_PLACEHOLDERS["file"]])
        if "capture" in operands:
            parts.extend(["--capture", _OPERAND_PLACEHOLDERS["capture"]])
        if "because" in operands:
            parts.extend(["--because", _OPERAND_PLACEHOLDERS["because"]])
    elif operation == "cruxible.files.propose":
        # Without a named file the operator adds --file and --name.
        source = values.get("source_id")
        if isinstance(source, str) and source:
            parts.extend(
                ["--file", shlex.quote(source), "--name", shlex.quote(_revision_name(source))]
            )
    elif operation == "cruxible.claim.retire":
        # Why it ends is the retirer's to say: `--because` is the operand left to add.
        claim_id = values.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return None
        parts.append(shlex.quote(claim_id))
        if "because" in operands:
            parts.extend(["--because", _OPERAND_PLACEHOLDERS["because"]])
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
    repair: PlaybillNextRepairV1 | None,
    surface: NextCallerSurface | None = None,
    repair_requires: PlaybillNextRepairRequirementV1 | None = None,
) -> PlaybillNextItemV1:
    if repair is None:
        return _withheld_item(
            severity=severity,
            reason=reason,
            subject_identity=subject_identity,
            related_identities=related_identities,
            detail=detail,
            repair_requires=repair_requires,
        )
    # Composed here rather than at each emitting site: a row whose command was
    # forgotten would be indistinguishable from one that has no command.
    command = _repair_command(
        repair.operation,
        arguments=repair.arguments,
        surface=surface,
        operands=repair.operands_to_add,
    )
    example = (
        _ATTESTATION_REPAIR_EXAMPLES.get(repair.required_change)
        if repair.operation == "cruxible.authoring.example"
        else None
    )
    if example is not None and isinstance(repair.arguments, Mapping):
        claim_id = repair.arguments.get("claim_id")
        capture_digest = repair.arguments.get("capture_digest")
        if isinstance(claim_id, str) and isinstance(capture_digest, str) and surface == "mcp":
            command = _mcp_call(
                "cruxible_authoring_example",
                name=example,
                claim_id=claim_id,
                capture_digest=capture_digest,
            )
        elif isinstance(claim_id, str) and isinstance(capture_digest, str) and surface == "sdk":
            command = _sdk_call(
                "authoring_example", example, claim_id=claim_id, capture_digest=capture_digest
            )
        elif isinstance(claim_id, str) and isinstance(capture_digest, str):
            command = " ".join(
                (
                    "cruxible authoring example",
                    shlex.quote(example),
                    "--attestation-claim-id",
                    shlex.quote(claim_id),
                    "--capture-digest",
                    shlex.quote(capture_digest),
                )
            )
    repair = repair.model_copy(
        update={
            "command": command,
            # Each alternative is composed for this surface like the primary.
            "alternatives": tuple(
                alternative.model_copy(
                    update={
                        "command": _repair_command(
                            alternative.operation,
                            arguments=alternative.arguments,
                            surface=surface,
                            operands=alternative.operands_to_add,
                        )
                    }
                )
                for alternative in repair.alternatives
            ),
        }
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


def _withheld_item(
    *,
    severity: NextSeverity,
    reason: NextReason,
    subject_identity: str,
    related_identities: tuple[str, ...],
    detail: object,
    repair_requires: PlaybillNextRepairRequirementV1 | None,
) -> PlaybillNextItemV1:
    """A row this caller cannot repair: kept, its repair withheld, its requirement named."""

    values = {
        "severity": severity,
        "reason": reason,
        "subject_identity": subject_identity,
        "related_identities": related_identities,
        "detail": detail,
        "repair": None,
        "repair_requires": repair_requires,
    }
    provisional = PlaybillNextItemV1.model_construct(
        _fields_set=None, item_id="sha256:" + "0" * 64, **values
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


def _evidence_content(item: PlaybillNextItemV1) -> str | None:
    """What a new-evidence row's Capture observed, by content.

    Two Captures of the same content -- the same file bytes, the same fetched
    body -- are one piece of evidence: the row names the observed content when
    it differs from the Capture's own commitment, and its Capture otherwise.
    """

    return _detail_value(item, "observed_content_digest") or _detail_value(item, "capture_digest")


def _group_key(
    item: PlaybillNextItemV1, *, edited_sources: frozenset[str]
) -> tuple[str, ...] | None:
    if item.reason in _BLOCK_REASONS:
        return ("block", item.subject_identity)
    if item.reason in _EVIDENCE_REASON_ORDER:
        content = _evidence_content(item)
        return None if content is None else ("evidence", item.subject_identity, content)
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
        nested = [
            finding.subject_identity
            for finding in item.findings
            if finding.reason in _SUPPORT_RESOLVES and finding.subject_identity in supporting
        ]
        if nested:
            singles.append(
                _with_findings(
                    item.model_copy(update={"findings": ()}),
                    [
                        *map(_row_of, item.findings),
                        *(
                            row
                            for subject in dict.fromkeys(nested)
                            for row in supporting.pop(subject)
                        ),
                    ],
                )
            )
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
        (item.subject_identity, _evidence_content(item))
        for item in items
        if item.reason in _EVIDENCE_REASON_ORDER and item.reason != "claim_new_evidence_supporting"
    }
    # A resolvable row may already sit inside another -- a conflict carries its
    # members' rows -- and still counts as the work the capture resolves.
    resolvable = {item.subject_identity for item in items if item.reason in _SUPPORT_RESOLVES} | {
        finding.subject_identity
        for item in items
        for finding in item.findings
        if finding.reason in _SUPPORT_RESOLVES
    }
    kept: list[PlaybillNextItemV1] = []
    folded: dict[str, list[PlaybillNextItemV1]] = defaultdict(list)
    # Resolvable rows first, in carrier order, so the first one reached carries.
    carrier_order = {reason: rank for rank, reason in enumerate(_SUPPORT_RESOLVES)}
    for item in sorted(items, key=lambda item: carrier_order.get(item.reason, len(carrier_order))):
        if item.reason != "claim_new_evidence_supporting":
            kept.append(item)
        elif (item.subject_identity, _evidence_content(item)) in contested:
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
            repair_requires=item.repair_requires,
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
        "repair_requires": head.repair_requires,
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
        door_events: tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...],
        door_history: Callable[
            [], tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...]
        ]
        | None = None,
        evaluation_time: datetime,
        accepted: tuple[ClaimAttestation, ...] | None = None,
    ) -> None:
        """``accepted`` replaces reading every current Claim's accepted examined attestations.

        A caller that holds the rows of a few Claims (the stored rows) passes
        those Claims' own, with every door examined event of their current
        versions instead of the folded events.
        """

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

        # The latest examined stance per Claim and principal as of the evaluation
        # time; a later support or contradict by the same principal ends that
        # principal's hold, and one made after the evaluation time does not.
        def eligible(statement: ClaimAttestationStatement) -> tuple[str, str] | None:
            identity = statement.claim_identity.qualified
            if (
                statement.attestation_basis != "examined_existing"
                or statement.attested_at > evaluation_time
                or self._current.get(identity) != statement.claim_artifact_digest
            ):
                return None
            return identity, statement.attesting_principal_id

        accepted_latest: dict[tuple[str, str], ClaimAttestationStatement] = {}
        if self._current:
            if accepted is None:
                with instance.bind_accepted_projection(coordinate) as projection:
                    accepted = projection.typed.claim_attestations(
                        basis="examined_existing", current_claims_only=True
                    )
            for envelope in accepted:
                key = eligible(envelope.statement)
                if key is not None and (
                    key not in accepted_latest
                    or accepted_latest[key].attested_at < envelope.statement.attested_at
                ):
                    accepted_latest[key] = envelope.statement
        # The folded door keeps only each principal's latest examined statement.
        # When that one postdates the evaluation time, the statement in force
        # then was superseded out of the fold, so read the whole chain.
        if door_history is not None and any(
            payload.attestation.statement.attestation_basis == "examined_existing"
            and payload.attestation.statement.attested_at > evaluation_time
            for _event, payload in door_events
        ):
            door_events = door_history()
        # Within the door, the latest append wins, exactly as the fold chooses,
        # so reading the whole chain never picks a different statement than the
        # fold would have at the same evaluation time.
        door_latest: dict[tuple[str, str], tuple[int, ClaimAttestationEventPayload]] = {}
        for event, payload in door_events:
            key = eligible(payload.attestation.statement)
            if key is not None and (key not in door_latest or door_latest[key][0] < event.sequence):
                door_latest[key] = (event.sequence, payload)
        latest: dict[tuple[str, str], ClaimAttestationStatement] = dict(accepted_latest)
        for key, (_sequence, payload) in door_latest.items():
            statement = payload.attestation.statement
            if key in latest and statement.attested_at < latest[key].attested_at:
                continue
            if payload.current_at_append is False:
                # Appended against a stale head: it ends a hold but never makes one.
                latest.pop(key, None)
                continue
            latest[key] = statement
        holds: dict[str, list[_UnsureHold]] = defaultdict(list)
        for (identity, _principal), statement in latest.items():
            if statement.stance == "unsure" and (
                statement.valid_until is None or evaluation_time < statement.valid_until
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
            contenders = list(_contenders(row))
            if not contenders:
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

    def _versions(self, identities: Sequence[object]) -> tuple[tuple[str, str], ...] | None:
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
            except CruxibleError:
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


def _hold_members(
    item: PlaybillNextItemV1,
) -> tuple[PlaybillNextItemV1 | PlaybillNextFindingV1, ...]:
    return (item, *item.findings)


def _held_claims(
    rows: tuple[PlaybillNextItemV1, ...], holds: _Holds, identities: set[str]
) -> frozenset[str]:
    held: set[str] = set()
    for item in rows:
        for row in _hold_members(item):
            if not holds.covers(row):
                continue
            # The Claims ``covers`` decided over: a conflict's contenders, else the
            # row's subject. A stale dependency's upstream Claims are not held.
            named = {row.subject_identity, *_contenders(row)}
            held.update(named & identities)
    return frozenset(held)


def _contenders(row: PlaybillNextItemV1 | PlaybillNextFindingV1) -> tuple[str, ...]:
    """A contest row's contenders: the Claims it names, whichever option its repair offers."""

    return tuple(row.related_identities) if row.reason == "claim_conflicted" else ()


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
            # Rebuilt, never copied: the item id digests the findings it carries.
            kept.append(
                _with_findings(item.model_copy(update={"findings": ()}), map(_row_of, remaining))
            )
        else:
            kept.append(item)
    return tuple(kept), held


def _oracle_claim_rows(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    door_events: tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...],
    claims: tuple[ClaimArtifactAny, ...] | None,
    resolution_statuses: Mapping[str, str] | None,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    access_profile: CoverageAccessProfile | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """The full fold of every row family ``next`` builds about Claims (the rows a hold can park).

    The stored rows (``consumers.next.facts``) serve reads at the head; this
    fold serves what they cannot -- a historical coordinate, a pinned door
    head, a disabled or cold worker -- and is the oracle the stored rows are
    tested against. ``next`` and ``claim_unsure_holds`` both assemble a
    Claim's rows here, so a family added for one reaches the other.
    """

    return (
        *_claim_items(
            instance,
            coordinate=AcceptedCoordinate.from_internal(coordinate),
            evaluation_time=evaluation_time,
            expiring_within=expiring_within,
            door_events=door_events,
            verdicts_by_identity=verdicts_by_identity,
            claims=claims,
            resolution_statuses=resolution_statuses,
            access_profile=access_profile,
        ),
        *_claim_attestation_door_items(
            instance,
            coordinate=coordinate,
            door_events=door_events,
            evaluation_time=evaluation_time,
            access_profile=access_profile,
        ),
        *_claim_dependency_items(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            access_profile=access_profile,
            claims=claims,
            facts_reader=facts_reader,
        ),
    )


def claim_unsure_holds(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    evaluation_time: datetime,
    resolution_statuses: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """The Claims an ``unsure`` hold parks right now, decided exactly as ``next`` decides.

    The rows are ``next``'s own Claim rows for these Claims and every live
    contender in their slots, so a conflict is decided over all its contenders
    whichever of them the caller asked about; a Claim is held when
    ``_Holds.covers`` parks one of them: a new contender, a revised input,
    newer evidence or a further expiry the attester never saw ends the hold here
    exactly as it returns the row to the queue. With no hold on any of them,
    nothing is folded.
    """

    live = tuple(claim for claim in claims if claim.lifecycle.state == "live")
    if not live:
        return frozenset()
    from cruxible_core.consumers.next.facts import read_heads, served_claim_rows
    from cruxible_core.storage.read_positions import reading_at

    identities = {claim.identity.qualified for claim in live}
    # The caller's coordinate is the one its own verdicts are read at: the holds
    # are read there too (folded once the worker has passed it), at the door head now.
    served = served_claim_rows(
        instance,
        coordinate=coordinate,
        door_head=None,
        evaluation_time=evaluation_time,
        expiring_within=CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS),
        claims=sorted(identities),
    )
    if served is not None:
        return _held_claims(served.rows, served.holds, identities) if served.holds else frozenset()
    heads = read_heads(instance, coordinate=coordinate)
    with reading_at(heads.positions):
        return _folded_holds(
            instance,
            coordinate=coordinate,
            live=live,
            identities=identities,
            door_head=heads.door_root,
            evaluation_time=evaluation_time,
            resolution_statuses=resolution_statuses,
        )


def _folded_holds(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    live: tuple[ClaimArtifactAny, ...],
    identities: set[str],
    door_head: str,
    evaluation_time: datetime,
    resolution_statuses: Mapping[str, str] | None,
) -> frozenset[str]:
    """``claim_unsure_holds`` folded at one read's recorded heads."""

    store = instance.claim_attestation_evidence_store()
    population = _with_slot_contenders(instance, coordinate, live)
    if resolution_statuses is None:
        # A conflict row stands on the slot's resolution, exactly as next decides it.
        try:
            resolution_statuses = claim_resolution_statuses(
                instance,
                claims=population,
                at=AcceptedCoordinate.from_internal(coordinate),
                evaluation_time=evaluation_time,
            )
        except CruxibleError:
            resolution_statuses = None
    door_events = store.fold_events(at_head=door_head)
    holds = _Holds(
        instance,
        coordinate=coordinate,
        claims=population,
        door_events=door_events,
        door_history=lambda: store.events(at_head=door_head),
        evaluation_time=evaluation_time,
    )
    if not holds:
        return frozenset()
    rows = _oracle_claim_rows(
        instance,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        expiring_within=CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS),
        door_events=door_events,
        claims=population,
        resolution_statuses=resolution_statuses,
    )
    return _held_claims(rows, holds, identities)


def _with_slot_contenders(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
) -> tuple[ClaimArtifactAny, ...]:
    """These live Claims and every live Claim sharing a (predicate, Subject) slot: one seek each."""

    context = ClaimVerdictReadContext(instance, coordinate)
    found: dict[str, ClaimArtifactAny] = {claim.identity.qualified: claim for claim in claims}
    with instance.bind_accepted_projection(coordinate) as projection:
        for claim in claims:
            subject = claim.statement.subject
            for (identity,) in projection.typed.connection.execute(
                LIVE_SELECTED_SLOT_CLAIMS_SQL,
                (
                    subject.artifact_path,
                    claim.statement.predicate,
                    subject.selector.scheme,
                    subject.selector.value,
                ),
            ):
                if str(identity) not in found:
                    found[str(identity)] = context.claim(str(identity))
    return tuple(found[identity] for identity in sorted(found))


def _row_of(finding: PlaybillNextFindingV1) -> PlaybillNextItemV1:
    return _item(
        severity=finding.severity,
        reason=finding.reason,
        subject_identity=finding.subject_identity,
        related_identities=finding.related_identities,
        detail=finding.detail,
        repair=finding.repair,
        repair_requires=finding.repair_requires,
    )


def _item_sort_key(item: PlaybillNextItemV1) -> tuple[int, bytes, bytes, bytes]:
    return (
        _SEVERITY_RANK[item.severity],
        item.subject_identity.encode("utf-8"),
        item.reason.encode("utf-8"),
        item.item_id.encode("ascii"),
    )


def playbill_next_result_digest(result: PlaybillNextResultV2) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    payload.pop("total_items")
    payload.pop("next_cursor")
    payload.pop("delta_since")
    payload.pop("removed_item_ids", None)
    return typed_digest(Sha256Value, NEXT_RESULT_V2_DIGEST_DOMAIN, payload).tagged


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
        raise NextCoordinateNotAccepted(
            f"{NextCoordinateNotAccepted.code}: coordinate is not accepted"
        ) from exc


def _claim_threshold_evidence(
    instance: PlaybillInstance, *, at: AcceptedProjectionCoordinate
) -> Mapping[str, ClaimLawEvidenceAny]:
    """A lazy law-evidence map; only threshold-bearing Claims need these records."""

    return ClaimVerdictReadContext(instance, at).history().law_evidence


def _claim_attestation_threshold_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    claims: tuple[ClaimArtifactAny, ...],
    law_evidence: Mapping[str, ClaimLawEvidenceAny],
    door_events: tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...] = (),
) -> tuple[PlaybillNextItemV1, ...]:
    """Emit v4 queue consequences from current independent attestation components."""

    accepted = _resolve_coordinate(instance, coordinate)
    tree = ClaimVerdictReadContext(instance, accepted).tree
    claim_types: dict[str, ClaimType] = {}
    # Each Claim reads only its own events: one pass over the door, not one per Claim.
    door_by_claim: dict[str, list[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload]]] = (
        defaultdict(list)
    )
    for event, payload in door_events:
        door_by_claim[payload.attestation.statement.claim_identity.qualified].append(
            (event, payload)
        )
    items: list[PlaybillNextItemV1] = []
    for claim in sorted(claims, key=lambda item: item.identity.qualified.encode("utf-8")):
        items.extend(
            _claim_threshold_rows(
                instance,
                accepted=accepted,
                tree=tree,
                claim=claim,
                law_evidence=law_evidence,
                claim_types=claim_types,
                evaluation_time=evaluation_time,
                door_events=door_by_claim.get(claim.identity.qualified, ()),
                time_boundaries=None,
            )
        )
    return tuple(items)


def _claim_threshold_rows(
    instance: PlaybillInstance,
    *,
    accepted: AcceptedProjectionCoordinate,
    tree: Mapping[str, bytes],
    claim: ClaimArtifactAny,
    law_evidence: Mapping[str, ClaimLawEvidenceAny],
    claim_types: dict[str, ClaimType],
    evaluation_time: datetime,
    door_events: Iterable[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload]],
    time_boundaries: set[datetime] | None,
) -> list[PlaybillNextItemV1]:
    """One live Claim's threshold rows, rule by rule: the threshold fold's per-Claim body.

    ``door_events`` may hold any events: only this exact version's examined ones count.
    """

    items: list[PlaybillNextItemV1] = []
    predicate = claim.statement.predicate
    claim_type = claim_types.get(predicate)
    if claim_type is None:
        path = claim_type_path(predicate)
        claim_type = parse_claim_type(tree[path], path=path)
        claim_types[predicate] = claim_type
    policy = claim_type.attestation_consequence_policy
    exact_door = tuple(
        (event, payload)
        for event, payload in door_events
        if payload.attestation.statement.claim_identity == claim.identity
        and payload.attestation.statement.claim_artifact_digest
        == claim_artifact_digest(claim).tagged
        and payload.attestation.statement.attestation_basis == "examined_existing"
    )
    latest_door_by_principal: dict[
        str, tuple[ClaimAttestationEvent, ClaimAttestationEventPayload]
    ] = {}
    for event, payload in exact_door:
        principal_id = payload.attesting_principal_id
        previous = latest_door_by_principal.get(principal_id)
        if previous is None or event.sequence > previous[0].sequence:
            latest_door_by_principal[principal_id] = (event, payload)
    if policy is None:
        return _contradicting_attestation_rows(
            claim,
            claim_type,
            latest=latest_door_by_principal,
            evaluation_time=evaluation_time,
            time_boundaries=time_boundaries,
        )
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
    if time_boundaries is not None:
        for item in current:
            time_boundaries.add(item.statement.observed_at)
            if item.statement.valid_until is not None:
                time_boundaries.add(item.statement.valid_until)
        for _event, payload in exact_door:
            time_boundaries.add(payload.attestation.statement.attested_at)
            if payload.attestation.statement.valid_until is not None:
                time_boundaries.add(payload.attestation.statement.valid_until)
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
                not isinstance(item, AcceptedClaimAttestationEvidence)
                or item.envelope.statement.attestation_basis == "examined_existing"
            )
            and item.attestation_grade == "verified_principal"
            and item.statement.provider_or_principal.kind == "Principal"
            and item.statement.claim_statement_digest
            == claim_statement_digest(claim.statement).tagged
            and item.statement.stance == rule.stance
            and item.statement.provider_or_principal.name not in superseded_accepted
            and item.statement.observed_at <= evaluation_time
            and (item.statement.valid_until is None or evaluation_time < item.statement.valid_until)
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
                # Enough independent contradiction retires the Claim; any
                # other threshold is resolved by stating it again.
                repair=(
                    PlaybillNextRepairV1(
                        operation="cruxible.claim.retire",
                        operands_to_add=("because",),
                        target=claim.identity.qualified,
                        required_change="resolve_attestation_threshold",
                        arguments={
                            "claim_id": claim.identity.name,
                            "rule_id": rule.rule_id,
                        },
                    )
                    if rule.stance == "contradict"
                    else _restating_repair(
                        "cruxible.authoring.example",
                        target=claim.identity.qualified,
                        required_change="resolve_attestation_threshold",
                        arguments={
                            "claim_id": claim.identity.name,
                            "rule_id": rule.rule_id,
                        },
                        set_arguments=_claim_set_arguments(claim, claim_type),
                    )
                ),
            )
        )
    return items


def _contradicting_attestation_rows(
    claim: ClaimArtifactAny,
    claim_type: ClaimType,
    *,
    latest: Mapping[str, tuple[ClaimAttestationEvent, ClaimAttestationEventPayload]],
    evaluation_time: datetime,
    time_boundaries: set[datetime] | None,
) -> list[PlaybillNextItemV1]:
    """A live Claim's current contradicting examined attestations, with no policy to weigh them.

    Maintainer ruling (2026-10-21): core resurfaces, agents judge. Each
    principal's latest word on this exact version that contradicts it, current
    at the evaluation time, raises one neutral row reusing the contradicting-
    evidence adjudication; the verdict does not move. ``latest`` is this
    version's own events, so the row costs what the Claim's partition holds.
    """

    reason, severity, required_change = _DOOR_REASONS["contradict"]
    set_arguments = _claim_set_arguments(claim, claim_type)
    items: list[PlaybillNextItemV1] = []
    for principal_id in sorted(latest, key=lambda item: item.encode("utf-8")):
        event, payload = latest[principal_id]
        statement = payload.attestation.statement
        if statement.stance != "contradict" or not payload.current_at_append:
            continue
        if time_boundaries is not None:
            time_boundaries.add(statement.attested_at)
            if statement.valid_until is not None:
                time_boundaries.add(statement.valid_until)
        if statement.attested_at > evaluation_time or (
            statement.valid_until is not None and evaluation_time >= statement.valid_until
        ):
            continue
        items.append(
            _item(
                severity=severity,
                reason=reason,
                subject_identity=claim.identity.qualified,
                related_identities=(f"Principal:{principal_id}",),
                detail={
                    "claim_id": claim.identity.name,
                    "claim_artifact_digest": statement.claim_artifact_digest,
                    "attestation_event_digest": event.event_digest,
                    "attestation_envelope_digest": payload.envelope_digest,
                    "attestation_basis": statement.attestation_basis,
                    "stance": statement.stance,
                    "attesting_principal": principal_id,
                    "attested_at": format_datetime(statement.attested_at),
                    "current_at_append": payload.current_at_append,
                    **({"note": payload.note} if payload.note is not None else {}),
                },
                # Restate it if it stands, else state what holds or retire it:
                # no Capture to cite, so the authoring door is a plain revision.
                repair=_restating_repair(
                    "cruxible.authoring.example",
                    target=claim.identity.qualified,
                    required_change=required_change,
                    arguments=(
                        {"claim_id": claim.identity.name}
                        if set_arguments is not None
                        else {"claim_id": claim.identity.name, "example": "claim-revision"}
                    ),
                    set_arguments=set_arguments,
                ).model_copy(
                    update={
                        "alternatives": (
                            _retire_alternative(claim.identity.qualified, claim.identity.name),
                        )
                    }
                ),
            )
        )
    return items


def _claim_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    door_events: tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...] = (),
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    resolution_statuses: Mapping[str, str] | None = None,
    access_profile: CoverageAccessProfile | None = None,
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
    claim_types: dict[str, ClaimType | None] = {}
    # The threshold fold reads evidence only for types with a consequence policy.
    # Materializing the whole historical map here costs one read per Claim.
    law_evidence = _claim_threshold_evidence(instance, at=internal)
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
        items.extend(
            _claim_slot_rows(
                instance,
                coordinate=coordinate,
                internal=internal,
                group=group,
                resolution_statuses=resolution_statuses,
                verdicts_by_identity=verdicts_by_identity,
                evaluation_time=evaluation_time,
                expiring_within=expiring_within,
                time_boundaries=None,
                claim_types=claim_types,
            )
        )
    return tuple(items)


def _claim_slot_rows(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    internal: AcceptedProjectionCoordinate,
    group: list[ClaimArtifactAny],
    resolution_statuses: Mapping[str, str] | None,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    time_boundaries: set[datetime] | None,
    claim_types: dict[str, ClaimType | None],
) -> list[PlaybillNextItemV1]:
    """One (predicate, qualifier, Subject) slot's rows: the conflict row, else its members'.

    The slot fold's per-group body. A member verdict ``verdicts_by_identity``
    does not hold is evaluated here.
    """

    items: list[PlaybillNextItemV1] = []
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
            # No contender is picked here: that decision is the caller's. The
            # row offers one runnable option per contender -- keep it by
            # retiring the others -- and leaves `because` (and the retirement's
            # reason) to the caller. Splitting the slot by a qualifier is the
            # last option, where a field tells the contenders apart. A contest
            # wider than the row's bound keeps the authoring door.
            repair=(
                _contest_repair(subject, identities, arguments)
                if len(identities) <= _MAX_CONTEST_OPTIONS
                else PlaybillNextRepairV1(
                    operation="cruxible.authoring.example",
                    target=subject,
                    required_change="revise_claims_into_distinct_qualifiers",
                    arguments=arguments,
                )
            ),
        )
    else:
        conflict_row = None
    # A conflicted slot's members still report their own evidence rows,
    # inside the conflict row rather than hidden behind it.
    member_rows: list[PlaybillNextItemV1] = []
    for claim in group:
        claim_type = _claim_type_at(
            instance, internal.git_oid, claim.statement.predicate, claim_types
        )
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
                time_boundaries=time_boundaries,
                at=coordinate,
            ).verdict
            if verdicts_by_identity is not None:
                verdicts_by_identity[claim.identity.qualified] = verdict
        if time_boundaries is not None and isinstance(verdict, ClaimVerdictResult):
            for expiration in verdict.freshness_expirations:
                time_boundaries.add(expiration.expires_at)
                time_boundaries.add(
                    expiration.expires_at - _expiring_lead(expiration, expiring_within)
                )
        if verdict.verdict == "stale_evidence":
            expirations = (
                verdict.freshness_expirations if isinstance(verdict, ClaimVerdictResult) else ()
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
                    repair=_evidence_repair(
                        "cruxible.authoring.bind",
                        target=claim.identity.qualified,
                        required_change="recapture_expired_evidence",
                        claim_id=claim.identity.name,
                        claim_type=claim_type,
                        set_arguments=_claim_set_arguments(claim, claim_type),
                        cited_contracts=_capture_contract_names(
                            instance, internal, (item.capture_digest for item in expired)
                        ),
                    ),
                )
            )
            continue
        if isinstance(verdict, ClaimVerdictResult) and verdict.verdict in {
            "supported",
            "contradicted",
            "unresolved",
        }:
            supporting = set(verdict.supporting_evidence_digests)
            current_support_expirations = tuple(
                item
                for item in verdict.freshness_expirations
                if item.capture_digest in supporting and evaluation_time < item.expires_at
            )
            # Each Capture is expiring within its own lead (the request's, at
            # most a quarter of its horizon); the row stands only while every
            # current support is.
            within = tuple(
                item.expires_at - _expiring_lead(item, expiring_within) <= evaluation_time
                for item in current_support_expirations
            )
            expiring = current_support_expirations if within and all(within) else ()
            if expiring:
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
                        repair=_evidence_repair(
                            "cruxible.authoring.bind",
                            target=claim.identity.qualified,
                            required_change="recapture_expiring_evidence",
                            claim_id=claim.identity.name,
                            claim_type=claim_type,
                            set_arguments=_claim_set_arguments(claim, claim_type),
                            cited_contracts=_capture_contract_names(
                                instance, internal, (item.capture_digest for item in expiring)
                            ),
                        ),
                    )
                )
        # A Claim not yet in effect is not uncovered: its evidence is judged
        # when its interval begins, not before.
        if verdict.verdict != "uncovered" or verdict.currency == "not_applicable":
            continue
        admitted_roles = _role_unadmitted(claim_type, claim.statement.role)
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
                        if admitted_roles is None
                        else f"This is a {claim.statement.role} Claim, and the "
                        "ClaimType's evidence rules admit only "
                        f"{', '.join(admitted_roles)}: no evidence covers it in this role. "
                        "Restate it in an admitted role, or change the evidence rules."
                    ),
                },
                # A role no rule admits stays uncovered whatever it cites, so
                # the repair reads the ClaimType's rules instead of restating it.
                repair=(
                    _evidence_repair(
                        "cruxible.authoring.bind",
                        target=claim.identity.qualified,
                        required_change="add_admissible_evidence",
                        claim_id=claim.identity.name,
                        claim_type=claim_type,
                        set_arguments=_claim_set_arguments(claim, claim_type),
                    )
                    if admitted_roles is None
                    else PlaybillNextRepairV1(
                        operation="cruxible.get",
                        target=claim.identity.qualified,
                        required_change="admit_claim_role_or_restate",
                        arguments={"ref": f"ClaimType:{claim.statement.predicate}"},
                        alternatives=(
                            _retire_alternative(claim.identity.qualified, claim.identity.name),
                        ),
                    )
                ),
            )
        )
    if conflict_row is None:
        items.extend(member_rows)
    else:
        items.append(_with_findings(conflict_row, member_rows) if member_rows else conflict_row)
    return items


def _contest_repair(
    subject: str, identities: tuple[str, ...], arguments: Mapping[str, object]
) -> PlaybillNextRepairV1:
    """One option per contender: keep it by retiring the others; then the qualifier split.

    Two contenders retire one Claim (`retire`); more retire all but one in a
    single write. The primary is the first contender's option only by order:
    the row's ``required_change`` (``choose_contender``) says it is a choice.
    """

    options: list[PlaybillNextRepairV1] = []
    for keep in identities:
        others = [other.removeprefix("Claim:") for other in identities if other != keep]
        options.append(
            PlaybillNextRepairV1(
                operation="cruxible.claim.retire",
                operands_to_add=("because",),
                target=keep,
                required_change="choose_contender",
                arguments={"claim_id": others[0]},
            )
            if len(others) == 1
            else PlaybillNextRepairV1(
                operation="cruxible.write",
                target=keep,
                required_change="choose_contender",
                arguments={"changes": [{"op": "retire", "target": other} for other in others]},
            )
        )
    if "qualifier_field" in arguments:
        options.append(
            PlaybillNextRepairV1(
                operation="cruxible.authoring.example",
                target=subject,
                required_change="revise_claims_into_distinct_qualifiers",
                arguments=dict(arguments),
            )
        )
    return options[0].model_copy(update={"alternatives": tuple(options[1:])})


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
    lineage_note: CitationLineageNote | None = None
    # The `set` that restates the citing Claim, as sorted items, when it can.
    set_arguments: tuple[tuple[str, object], ...] | None = None
    # The heading path a section citation was taken at, so a re-bind names it.
    section: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _UnavailableCitation:
    """A live Claim's citation whose cited Capture envelope the store cannot
    produce right now (missing, corrupt, or refused).

    A direct byte read of the Capture still refuses; the citation inventory a
    workspace observation walks instead names it unavailable and answers the
    rest of the inventory normally, the same principle a damaged body's
    ``evidence_unavailable`` row already follows.
    """

    citation_id: str
    capture_digest: str
    claim_identity: str


@dataclass(frozen=True)
class CitationInventory:
    """Citation commitments a workspace observation reads, by citation id, and
    the ones whose cited Capture envelope could not be read."""

    by_id: Mapping[str, _CitationCommitment]
    unavailable: tuple[_UnavailableCitation, ...] = ()


def _source_selection_span(envelope: object) -> tuple[int, int] | None:
    """Read the accepted original byte window without inferring a locator."""

    source = getattr(envelope, "source", None)
    if not isinstance(source, ExternalSourceReference):
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
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> CitationInventory:
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
    result: dict[str, _CitationCommitment] = {}
    citation_claim_types: dict[str, ClaimType | None] = {}
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
    unavailable: list[_UnavailableCitation] = []
    try:
        for claim in population:
            if claim.lifecycle.state != "live" or claim.identity.qualified not in visible_claims:
                continue
            commitments, claim_unavailable = claim_citation_commitments(
                instance,
                claim=claim,
                coordinate=internal_coordinate,
                store=store,
                claim_types=citation_claim_types,
            )
            for commitment in commitments:
                result[commitment.citation_id] = commitment
            unavailable.extend(claim_unavailable)
    except Exception as exc:
        raise NextAcceptedStateInvalid(
            f"{NextAcceptedStateInvalid.code}: citation inventory is invalid"
        ) from exc
    return CitationInventory(by_id=result, unavailable=tuple(unavailable))


_CITATION_READER = BodyAccessContext(principal_id="playbill-next", can_read_body=True)


def claim_citation_commitments(
    instance: PlaybillInstance,
    *,
    claim: ClaimArtifactAny,
    coordinate: AcceptedProjectionCoordinate,
    store: Any,
    claim_types: dict[str, ClaimType | None],
) -> tuple[tuple[_CitationCommitment, ...], tuple[_UnavailableCitation, ...]]:
    """One live Claim's citation commitments: what each effective citation committed to.

    Reads the Claim's law record, its predecessor version when it has one, and
    the Capture envelope of each effective citation. ``claim_types`` is a
    per-caller ClaimType memo for the restating ``set`` arguments.

    A citation whose cited Capture envelope cannot be read -- missing,
    corrupt, or refused -- is named in the second tuple instead of raising:
    the rest of the Claim's citations are still read and returned in the
    first. Nothing extra is read to tell those apart; the read that already
    has to happen is the one that fails.
    """

    evidence = _claim_law_evidence(
        instance,
        path=claim_path(claim.identity.name),
        at=coordinate,
    )
    effective_captures = {item.capture_digest for item in evidence.verdict_captures}
    predecessor_digest = claim.lifecycle.predecessor_digest
    lineage_note: CitationLineageNote | None = None
    if predecessor_digest is not None:
        historical = _historical_claim(
            instance,
            coordinate=coordinate,
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
    commitments: list[_CitationCommitment] = []
    unavailable: list[_UnavailableCitation] = []
    for citation in claim_citation_references(claim):
        # A located section is a copy, so it does not have to contribute to
        # the verdict. Its independent location still commits to drift.
        location_copy = (
            getattr(citation, "role", None) == "copy"
            and getattr(citation, "origin", None) == "independent"
        )
        if citation.capture_digest not in effective_captures and not location_copy:
            continue
        # Present as of the read's landing position, or missing to it.
        try:
            envelope = parse_capture_envelope(
                read_body(store, citation.capture_digest, access=_CITATION_READER)
            )
        except CruxibleError:
            unavailable.append(
                _UnavailableCitation(
                    citation_id=citation.citation_id,
                    capture_digest=citation.capture_digest,
                    claim_identity=claim.identity.qualified,
                )
            )
            continue
        selector = (
            envelope.source.selector
            if isinstance(envelope.source, ExternalSourceReference)
            else None
        )
        window = (
            selector.get("working_selection", selector) if isinstance(selector, Mapping) else None
        )
        label = window.get("section") if isinstance(window, Mapping) else None
        if citation.capture_digest not in effective_captures and not isinstance(label, Mapping):
            continue
        heading_path = label.get("path") if isinstance(label, Mapping) else None
        source_id: str | None = None
        source_digest: str | None = None
        if (
            isinstance(envelope.source, ExternalSourceReference)
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
        commitments.append(
            _CitationCommitment(
                citation_id=citation.citation_id,
                commitment_digest=envelope.commitment.digest,
                byte_length=envelope.commitment.byte_length or 0,
                claim_identity=claim.identity.qualified,
                source_id=source_id,
                source_digest=source_digest,
                original_start=None if selection_span is None else selection_span[0],
                original_end=None if selection_span is None else selection_span[1],
                lineage_note=lineage_note,
                section=(
                    tuple(str(item) for item in heading_path)
                    if isinstance(heading_path, list | tuple)
                    else None
                ),
                set_arguments=(
                    None
                    if (
                        restated := _claim_set_arguments(
                            claim,
                            _claim_type_at(
                                instance,
                                coordinate.git_oid,
                                claim.statement.predicate,
                                claim_types,
                            ),
                        )
                    )
                    is None
                    else tuple(sorted(restated.items()))
                ),
            )
        )
    return tuple(commitments), tuple(unavailable)


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
    """Follow indexed predecessors, at most ``max_nodes`` for each lineage.

    Unrelated generations consume no traversal budget, and neither do other
    Claims' lineages: whether one lineage is complete depends on its own length
    alone, so it can be decided for one Claim without reading the others.
    """
    lineages: dict[str, tuple[str, ...]] = {}
    incomplete: set[str] = set()
    for path, current in sorted(current_claims.items()):
        remaining = max_nodes
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
                except (CruxibleError, ValueError):
                    accounts_by_authority[authority_digest] = ()
                    incomplete = True
        artifact_accounts[artifact.artifact_digest] = accounts_by_authority[authority_digest]
        predecessor = artifact
    return artifact_accounts, incomplete


class _ObservedContents:
    """Each Capture's observed-content identity, read once per request.

    An accepted (cited) Capture is answered from the evidence index; any other
    from its envelope. A Capture that cannot be read keys as itself.
    """

    def __init__(self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate):
        self._instance = instance
        self._coordinate = coordinate
        self._known: dict[str, tuple[str, str]] = {}

    def _read(self, capture_digest: str) -> tuple[str, str]:
        found = self._known.get(capture_digest)
        if found is not None:
            return found
        row = None
        with self._instance.bind_accepted_projection(self._coordinate) as projection:
            row = projection.typed.connection.execute(
                "SELECT observed_content_digest, evidence_commitment_digest FROM captures "
                "WHERE capture_digest=?",
                (capture_digest,),
            ).fetchone()
        if row is not None:
            found = (str(row[0]), str(row[1]))
        else:
            try:
                envelope = parse_capture_envelope(
                    read_body(
                        self._instance.body_store(),
                        capture_digest,
                        access=BodyAccessContext(principal_id="playbill-next", can_read_body=True),
                    )
                )
            except (OSError, CruxibleError, ValueError):
                found = (capture_digest, capture_digest)
            else:
                found = (observed_content(envelope).digest, envelope.commitment.digest)
        self._known[capture_digest] = found
        return found

    def digest(self, capture_digest: str) -> str:
        return self._read(capture_digest)[0]

    def commitment(self, capture_digest: str) -> str:
        return self._read(capture_digest)[1]


def _claim_attestation_door_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    door_events: tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...],
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Fold new-capture memberships against immutable acceptance-time accounts.

    Only attestations current at the evaluation time count, exactly as for the
    attestation threshold: one not yet made, or past its ``valid_until``, asks
    nothing of anyone.
    """

    if access_profile is not None and not access_profile.permits("instance"):
        return ()
    door_claim_types: dict[str, ClaimType | None] = {}

    from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest

    observations: list[tuple[ClaimAttestation, str | None, bool | None]] = [
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
    fold = _DoorFold(
        instance,
        coordinate,
        law_by_artifact=lambda: law_by_artifact,
        claim_types=door_claim_types,
    )
    items: list[PlaybillNextItemV1] = []
    for envelope, event_digest, current_at_append in observations:
        items.extend(
            fold.rows(
                envelope,
                event_digest=event_digest,
                current_at_append=current_at_append,
                evaluation_time=evaluation_time,
                time_boundaries=None,
            )
        )
    return tuple(items)


_DOOR_REASONS: Mapping[str, tuple[NextReason, NextSeverity, str]] = {
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


class _DoorFold:
    """Fold one new-capture observation at a time, sharing what its Claim's rows read.

    Each Claim's lineage and acceptance-time accounts are read once; the law
    records come from ``law_by_artifact``, read only when an observation needs them.
    """

    def __init__(
        self,
        instance: PlaybillInstance,
        coordinate: AcceptedProjectionCoordinate,
        *,
        law_by_artifact: Callable[[], Mapping[tuple[str, str], ClaimLawEvidenceAny]],
        claim_types: dict[str, ClaimType | None],
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._law_source = law_by_artifact
        self._law_index: Mapping[tuple[str, str], ClaimLawEvidenceAny] | None = None
        self._contents = _ObservedContents(instance, coordinate)
        self._lineages: dict[str, tuple[tuple[_AttestationLineageArtifact, ...], bool]] = {}
        self._accounts: dict[
            str, tuple[dict[str, tuple[CaptureAdmissionAccountV1, ...]], bool]
        ] = {}
        self._claim_types = claim_types

    def _law(self) -> Mapping[tuple[str, str], ClaimLawEvidenceAny]:
        if self._law_index is None:
            self._law_index = self._law_source()
        return self._law_index

    def read_captures(self, claim_id: str) -> tuple[str, ...]:
        """The Captures this Claim's acceptance-time accounts are read from (none until read).

        Every citation of its lineage, as Layer 1 counts the door's reads: a
        citation envelope that could not be read left its accounts incomplete,
        and it is a read all the same, so its repair reaches the door's rows.
        ``claim_id`` is the Claim's name or its qualified identity.
        """

        name = claim_id.removeprefix("Claim:")
        found = self._accounts.get(name)
        lineage = self._lineages.get(name)
        if found is None and lineage is None:
            return ()
        captures = (
            set()
            if found is None
            else {account.capture_digest for accounts in found[0].values() for account in accounts}
        )
        if lineage is not None:
            captures.update(
                citation.capture_digest
                for artifact in lineage[0]
                for citation in claim_citation_references(artifact.claim)
            )
        return tuple(sorted(captures))

    def rows(
        self,
        envelope: ClaimAttestation,
        *,
        event_digest: str | None,
        current_at_append: bool | None,
        evaluation_time: datetime,
        time_boundaries: set[datetime] | None,
    ) -> list[PlaybillNextItemV1]:
        """This observation's rows: one per cited Capture its Claim does not already admit."""

        from cruxible_client.contracts.claim_attestations import (
            claim_attestation_v2_envelope_digest,
        )

        items: list[PlaybillNextItemV1] = []
        statement = envelope.statement
        if statement.attestation_basis != "new_capture":
            return []
        if time_boundaries is not None:
            time_boundaries.add(statement.attested_at)
            if statement.valid_until is not None:
                time_boundaries.add(statement.valid_until)
        if statement.attested_at > evaluation_time or (
            statement.valid_until is not None and evaluation_time >= statement.valid_until
        ):
            return []
        claim_id = statement.claim_identity.name
        cached_lineage = self._lineages.get(claim_id)
        if cached_lineage is None:
            cached_lineage = _attestation_claim_lineage(
                self._instance,
                coordinate=self._coordinate,
                claim_identity=claim_id,
            )
            self._lineages[claim_id] = cached_lineage
        lineage, lineage_incomplete = cached_lineage
        cached_accounts = self._accounts.get(claim_id)
        if cached_accounts is None:
            cached_accounts = _attestation_resolving_accounts(
                self._instance,
                lineage=lineage,
                law_by_artifact=self._law(),
            )
            self._accounts[claim_id] = cached_accounts
        accounts_by_artifact, account_incomplete = cached_accounts
        lineage_digests = {item.artifact_digest for item in lineage}
        membership_proven = statement.claim_artifact_digest in lineage_digests
        terminal = bool(
            membership_proven
            and lineage
            and isinstance(lineage[-1].claim, ClaimArtifact)
            and lineage[-1].claim.lifecycle.state == "retired"
        )
        for capture_digest in statement.cited_capture_digests:
            # A Claim that already admits a Capture of the same observed content
            # has this evidence: re-observing it asks nothing new.
            content = self._contents.digest(capture_digest)
            resolved = terminal or any(
                any(
                    account.status == "admitted"
                    and self._contents.digest(account.capture_digest) == content
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
            reason, severity, required_change = _DOOR_REASONS[statement.stance]
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
                        **(
                            {"observed_content_digest": content}
                            if content != self._contents.commitment(capture_digest)
                            else {}
                        ),
                    },
                    # A supporting (or unsure) Capture is cited by restating the
                    # Claim on it. A contradicting one is read first: the agent
                    # judges whether the Claim stands (restated on its own
                    # evidence) or ends (retired).
                    repair=(
                        _contradicting_capture_repair(
                            statement.claim_identity.qualified,
                            claim_id=claim_id,
                            capture_digest=capture_digest,
                            required_change=required_change,
                            set_arguments=_live_claim_set_arguments(
                                self._instance,
                                self._coordinate.git_oid,
                                claim_id,
                                self._claim_types,
                            ),
                        )
                        if statement.stance == "contradict"
                        else _restating_repair(
                            "cruxible.authoring.example",
                            target=statement.claim_identity.qualified,
                            required_change=required_change,
                            arguments={
                                "claim_id": claim_id,
                                "capture_digest": capture_digest,
                            },
                            set_arguments=_live_claim_set_arguments(
                                self._instance,
                                self._coordinate.git_oid,
                                claim_id,
                                self._claim_types,
                            ),
                        )
                    ),
                )
            )
        return items


def _claim_dependency_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile | None = None,
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Coalesce stale recorded backing-input edges through the existing impact walker."""

    if access_profile is not None and not access_profile.permits("instance"):
        return ()
    # No consumed Claim inputs means there can be no stale dependency edge.
    # Check the accepted population before building replay/visibility facts.
    if claims is not None and not any(claim.backing.input_claim_digests for claim in claims):
        # The skipped retired build would have refused an accepted retired
        # Claim whose adjudication rule no longer reproduces; still refuse.
        verify_retired_claim_adjudication(instance, coordinate=coordinate)
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
        for identity, entry in stale_input_entries(
            source.accepted.claim.identity.qualified,
            impact.dependents,
            lineage_complete=source.accepted.path not in incomplete,
        ):
            by_dependent[identity].append(entry)

    return tuple(
        claim_dependency_row(identity, by_dependent[identity])
        for identity in sorted(by_dependent, key=lambda item: item.encode("utf-8"))
    )


def stale_input_entries(
    source_identity: str,
    dependents: Iterable[DependentImpactV1],
    *,
    lineage_complete: bool,
) -> list[tuple[str, dict[str, object]]]:
    """Each dependent one source's move leaves stale: a superseded or contradicted backing input."""

    entries: list[tuple[str, dict[str, object]]] = []
    for dependent in dependents:
        if (
            dependent.kind != "Claim"
            or dependent.dependency_kind != "backing_input"
            or not dependent.repair_candidate
            or not ({SOURCE_SUPERSEDED, SOURCE_CONTRADICTED} & set(dependent.impact_reasons))
        ):
            continue
        entries.append(
            (
                dependent.identity,
                {
                    "source_claim_identity": source_identity,
                    "used_artifact_digest": dependent.used_artifact_digest,
                    "current_artifact_digest": dependent.current_artifact_digest,
                    "impact_reasons": list(dependent.impact_reasons),
                    "lineage_complete": lineage_complete,
                },
            )
        )
    return entries


def claim_dependency_row(
    identity: str, entries: Iterable[Mapping[str, object]]
) -> PlaybillNextItemV1:
    """One dependent's stale-dependency row, from every source's stale-input entry."""

    stale_inputs = sorted(
        (dict(item) for item in entries),
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
    return _item(
        severity="repair",
        reason="claim_dependency_stale",
        subject_identity=identity,
        related_identities=related,
        detail={"stale_inputs": stale_inputs},
        repair=PlaybillNextRepairV1(
            operation="cruxible.authoring.example",
            target=identity,
            required_change="reauthor_claim_from_current_inputs",
            arguments={"claim_id": identity.removeprefix("Claim:")},
        ),
    )


def _source_scan_defect_notes(
    observed: NextSourceObservation,
) -> tuple[str, ...] | None:
    """Report the notes evidencing a defect in the source's own scan.

    A defect here is source-wide: the evidence ``_source_citation_item``
    consults was never collected, so every citation to this source falls
    through the same gate no matter what the citation says. ``None`` means the
    scan is healthy and an unobserved citation there is a finding about that
    citation, to be repaired one citation at a time.
    """

    notes = tuple(
        note for note in observed.scan_notes if note in _COVERAGE_SOURCE_SCAN_DEFECT_NOTES
    )
    return notes or None


def _source_citation_item(
    *,
    citation_id: str,
    commitment: _CitationCommitment,
    observed: NextSourceObservation | None,
    coordinate: AcceptedCoordinate,
) -> PlaybillNextItemV1 | None:
    source_id = commitment.source_id
    assert source_id is not None and commitment.source_digest is not None
    if observed is None:
        return _citation_unobserved_item(commitment)
    expected_source = LogicalSourceIdentity(plane="external", identity=source_id)
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
        raise NextWorkspaceObservationInvalid(
            f"{NextWorkspaceObservationInvalid.code}: "
            "a complete zero-occurrence proof contradicts its unchanged original window"
        )
    return _citation_drift_item(
        commitment,
        coordinate=coordinate,
        drift_state="changed",
        observed_window_digest=window.observed_window_digest,
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
        repair=_restating_repair(
            "cruxible.authoring.bind",
            target=commitment.claim_identity,
            required_change="observe_cited_source",
            arguments={
                "claim_id": commitment.claim_identity.removeprefix("Claim:"),
                "citation_id": commitment.citation_id,
                "source_id": source_id,
            },
            # A row standing for a whole defective source scan restates no one
            # Claim: the scan is what has to be observed again.
            set_arguments=(
                None
                if commitment.set_arguments is None or source_scan_notes
                else dict(commitment.set_arguments)
            ),
        ),
    )


def _citation_drift_item(
    commitment: _CitationCommitment,
    *,
    coordinate: AcceptedCoordinate,
    drift_state: Literal["changed", "gone", "ambiguous"],
    observed_window_digest: str | None = None,
    occurrences: tuple[WorkingOccurrence, ...] = (),
) -> PlaybillNextItemV1:
    source_id = commitment.source_id
    source = (
        None
        if source_id is None
        else LogicalSourceIdentity(plane="external", identity=source_id).model_dump(mode="json")
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
    claim_id = commitment.claim_identity.removeprefix("Claim:")
    rebind: dict[str, object] = {
        "claim_id": claim_id,
        "citation": commitment.citation_id.partition(":")[2][:12],
        **({} if source_id is None else {"source_id": source_id}),
    }
    if commitment.section is not None and source_id is not None:
        # A section citation names its heading, which locates the new text on
        # its own; a file citation's new anchor is the re-binder's to choose.
        rebind.update({"section_file": source_id, "section": list(commitment.section)})
    # Core resurfaces, the agent judges: re-bind the Claim to the text it rests
    # on now (the statement stays), or retire it -- whether the text changed,
    # moved or is gone.
    return _item(
        severity="repair",
        reason="citation_drifted",
        subject_identity=commitment.claim_identity,
        related_identities=(commitment.citation_id,),
        detail=detail,
        repair=PlaybillNextRepairV1(
            operation="cruxible.rebind",
            target=commitment.claim_identity,
            required_change="rebind_or_retire_drifted_claim",
            arguments=rebind,
            # A section citation locates its new text; a file citation's new
            # anchor is the re-binder's to choose.
            operands_to_add=("because",) if "section" in rebind else ("file", "because"),
            alternatives=(
                PlaybillNextRepairV1(
                    operation="cruxible.claim.retire",
                    operands_to_add=("because",),
                    target=commitment.claim_identity,
                    required_change="retire_claim_with_attribution",
                    arguments={
                        "claim_id": claim_id,
                        "expected_coordinate": coordinate.model_dump(mode="json"),
                    },
                ),
            ),
        ),
    )


def _citation_unavailable_item(item: _UnavailableCitation) -> PlaybillNextItemV1:
    """One citation whose cited Capture envelope cannot be read right now.

    Reuses ``evidence_unavailable``'s reason and row shape: the subject is the
    Capture, named by this one citing Claim, the same repair the evidence
    sweep's own finding carries. The sweep discovers a damaged body on its own
    schedule; this one is discovered where a workspace observation's citation
    inventory walk already has to read the envelope, and is not a duplicate --
    a single cited Capture appears here once per citing Claim.
    """

    subject = f"Capture:{item.capture_digest}"
    return _item(
        severity="repair",
        reason="evidence_unavailable",
        subject_identity=subject,
        related_identities=(item.claim_identity,),
        detail={
            "capture_digest": item.capture_digest,
            "citation_id": item.citation_id,
        },
        repair=PlaybillNextRepairV1(
            operation="hand_edit",
            target=subject,
            required_change="restore_the_capture_from_backup_or_recapture_and_recite_its_claims",
        ),
    )


def _workspace_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile,
    observation: NextWorkspaceObservation | None,
    claims: tuple[ClaimArtifactAny, ...] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
    served: ServedRows | None = None,
) -> tuple[tuple[NextDomain, ...], tuple[PlaybillNextItemV1, ...]]:
    """Workspace rows. ``served`` carries the maintained citation commitments a
    stored-rows read took; without it the inventory is folded from every Claim."""

    if observation is None:
        return (), ()

    def commitments() -> CitationInventory:
        if served is not None and served.citations is not None:
            if served.citation_error:
                raise NextAcceptedStateInvalid(
                    f"{NextAcceptedStateInvalid.code}: citation inventory is invalid"
                )
            return CitationInventory(
                by_id=served.citations,
                unavailable=tuple(
                    _UnavailableCitation(
                        citation_id=citation_id, capture_digest=capture_digest, claim_identity=claim
                    )
                    for citation_id, capture_digest, claim in served.citations_unavailable or ()
                ),
            )
        return _citation_commitments(
            instance,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            claims=claims,
            facts_reader=facts_reader,
        )

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
                        operation="cruxible.floor.export",
                        target=instance.descriptor.instance_id,
                        required_change="replace_installed_floor",
                        arguments={},
                    ),
                )
            )
    if observation.drift_observations is not None and access_profile.permits("instance"):
        domains.append("workspace_sources")
        requested_ids = {drift.citation_id for drift in observation.drift_observations}
        inventory = commitments()
        unavailable_ids = {
            item.citation_id for item in inventory.unavailable if item.citation_id in requested_ids
        }
        items.extend(
            _citation_unavailable_item(item)
            for item in inventory.unavailable
            if item.citation_id in requested_ids
        )
        for drift in observation.drift_observations:
            if drift.citation_id in unavailable_ids:
                # Its cited Capture is unreadable, named above; it is not a
                # mismatched or unknown citation and the rest still checks.
                continue
            expected = inventory.by_id.get(drift.citation_id)
            if expected is None or expected.commitment_digest != drift.expected_commitment_digest:
                raise NextWorkspaceObservationInvalid(
                    f"{NextWorkspaceObservationInvalid.code}: "
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
        inventory = commitments()
        items.extend(_citation_unavailable_item(item) for item in inventory.unavailable)
        defective_scan_notes = {
            source.source_id: notes
            for source in observation.source_observations
            for notes in (_source_scan_defect_notes(source),)
            if notes is not None
        }
        defective_commitments: dict[str, list[_CitationCommitment]] = defaultdict(list)
        for citation_id in sorted(inventory.by_id, key=lambda item: item.encode("ascii")):
            commitment = inventory.by_id[citation_id]
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


def _citation_request(observation: NextWorkspaceObservation | None) -> CitationRequest:
    """The citation commitments a workspace observation asks about: its drifted ids, or
    every commitment naming a source (an unobserved source's is a row too)."""

    if observation is None:
        return None
    if observation.drift_observations is not None:
        return tuple(drift.citation_id for drift in observation.drift_observations)
    if observation.source_observations is not None:
        return "sources"
    return None


def _backed_claims(observation: NextWorkspaceObservation | None) -> tuple[str, ...]:
    """The Claims the observed projection markers are backed by: what their checks read."""

    if observation is None or observation.source_observations is None:
        return ()
    return tuple(
        sorted(
            {
                backing.identity.qualified
                for source in observation.source_observations
                for marker in source.marker_summaries
                for backing in marker.stamp.backing
                if isinstance(backing, ProjectionClaimBacking)
            }
        )
    )


def _consumer_healths(
    instance: PlaybillInstance,
    *,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile,
) -> tuple[ConsumerHealth, ...]:
    """Every active consumer's health, read once per request for its rows and its facet."""

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.consumers.runner import consumer_health

    return consumer_health(instance, now=evaluation_time)


def _consumer_stalled_items(healths: tuple[ConsumerHealth, ...]) -> tuple[PlaybillNextItemV1, ...]:
    """A daemon consumer that stopped by itself, or stopped keeping up with its work.

    Consumers are operational state, measured now, like mirror health: an arm
    that stopped, or a worker that stopped advancing, is invisible anywhere
    else, and automation that quietly stops is the failure it otherwise hides.
    A deliberate disarm is not a finding. Each kind names its own repair.
    """

    return tuple(
        _item(
            severity="repair",
            reason="consumer_stalled",
            subject_identity=health.consumer_id,
            detail={"kind": health.kind, "state": health.state, **health.detail},
            repair=PlaybillNextRepairV1(
                operation=cast(NextRepairOperation, normalize_code(health.repair.operation)),
                target=health.consumer_id,
                required_change=health.repair.required_change,
                arguments=health.repair.arguments,
            ),
        )
        for health in healths
        if health.state in {"stopped", "stalled"} and health.repair is not None
    )


def _line_settled_refusal_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Live Lines whose current version settled items matching earlier refusals.

    Settled items skip review, so a receipt on one is easy to miss. One row per
    live Line, read from the count kept for the Line's exact spec digest -- one
    keyed probe per live Line, never a walk of its runs. Counts are computed in
    post from the run journal (the next consumer's tick, and a bounded catch-up
    here), never on settle delivery.

    The repair is the Line successor, because that is what closes the row and
    what the evidence asks for: demote the Line to `max_authority: propose` so
    its results are reviewed again, or point it at a fixed Procedure. Either is a
    new Line version, whose count starts empty. The runs that settled the items
    are named for `get` (the latest in related identities, which `run get` reads
    with each item's receipts) before deciding which.
    """

    if not access_profile.permits("instance"):
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        lines = tuple(
            (str(identity), str(digest))
            for identity, digest in projection.typed.connection.execute(
                "SELECT identity,artifact_digest FROM lines WHERE lifecycle='live' "
                "ORDER BY identity"
            )
        )
    if not lines:
        return ()
    from cruxible_core.service.procedures.settled_refusals import (
        NEXT_CATCH_UP_STEPS,
        catch_up_settled_refusals,
        count_settled_refusals_step,
    )

    # Settlements are counted in post; a bounded catch-up makes the row current
    # when it is looked at. The read builds the index if it must (as every read
    # does); a busy index skips the count, and the next read or the consumer's
    # next tick counts what is left, from the persisted cursor.
    index = instance.refusal_index()
    index.coverage(instance)  # a read allowed to wait builds the index if it must
    catch_up_settled_refusals(instance, steps=NEXT_CATCH_UP_STEPS, wait=False)
    # The counts and their coverage come from one final read, after this
    # request's own catch-up: they describe the same snapshot.
    counts, coverage = index.settled_refusals_with_coverage(instance, lines)
    if coverage is None:
        # That read may have completed the retirements the steps were spending
        # themselves on; count what was waiting on them before answering.
        step = count_settled_refusals_step(instance, wait=False)
        if step.records:
            counts, coverage = index.settled_refusals_with_coverage(instance, lines)
    flagged = [found for key in lines if (found := counts.get(key)) is not None]
    if not flagged:
        return ()
    # The repair payload restates each flagged Line from its record: one batched
    # read of the flagged Lines' specs at the coordinate, never of their runs.
    paths = {
        found.line_identity: line_spec_path(found.line_identity.removeprefix("Line:"))
        for found in flagged
    }
    specs = instance.blobs_at(coordinate.git_oid, tuple(sorted(set(paths.values()))))
    items: list[PlaybillNextItemV1] = []
    for found in flagged:
        path = paths[found.line_identity]
        content = specs.get(path)
        spec = None if content is None else parse_line_spec(content, path=path)
        items.append(
            _item(
                severity="warning",
                reason="line_settled_prior_refusals",
                subject_identity=found.line_identity,
                related_identities=(f"ProcedureRun:{found.latest_run_id}",),
                detail={
                    "line_spec_digest": found.line_spec_digest,
                    "settled_items_matching_refusals": found.items,
                    "settled_runs": found.runs,
                    "latest_run_id": found.latest_run_id,
                    "latest_settled_at": found.latest_settled_at,
                    **(
                        {}
                        if coverage is None
                        else {"counted_through_generation": coverage.indexed_through_generation}
                    ),
                    "detail": (
                        f"{found.line_identity} settled {found.items} item"
                        f"{'s' if found.items != 1 else ''} that match earlier refusals "
                        "of the same statement on the same evidence; settled items skip "
                        "review. Read the runs, then submit the repair payload: the same "
                        "Line capped at max_authority: propose, so its results are "
                        "reviewed again. Or bind it to a fixed Procedure: the same payload "
                        "with procedure_name naming the fixed version."
                    ),
                },
                repair=_line_demotion_repair(found.line_identity, spec),
            )
        )
    return tuple(items)


def _line_demotion_repair(line_identity: str, spec: LineSpec | None) -> PlaybillNextRepairV1:
    """The Line successor that closes a settled-prior-refusals row, filled from the Line.

    Same identity, the same Procedure, acquisition policy, parameters, budgets
    and trigger input, capped at `max_authority: propose`: lowering pins the
    Procedure and policy by name and makes it the Line's successor, whose count
    starts empty. A Line whose record cannot be read (never at an accepted
    coordinate) keeps the template that starts the payload.
    """

    if spec is None:
        return PlaybillNextRepairV1(
            operation="cruxible.authoring.example",
            target=line_identity,
            required_change="demote_the_line_to_propose_or_bind_a_fixed_procedure",
            arguments={"example": "line"},
        )
    budgets = spec.budgets
    payload = LineInput(
        kind="line",
        name=spec.identity.name,
        procedure_name=spec.procedure.target.name,
        acquisition_policy_name=(
            None if spec.acquisition_policy is None else spec.acquisition_policy.target.name
        ),
        max_authority="propose",
        trigger_input=spec.trigger_input,
        parameters=cast(dict[str, object], spec.model_dump(mode="json")["parameters"]),
        budgets=(
            {str(key): int(value) for key, value in budgets.items()}
            if isinstance(budgets, Mapping)
            else None
        ),
        occurrence_epoch=spec.occurrence_epoch,
    )
    return PlaybillNextRepairV1(
        operation="cruxible.authoring.submit",
        target=line_identity,
        required_change="demote_the_line_to_propose_or_bind_a_fixed_procedure",
        arguments={"line": spec.identity.name, "payload": payload.model_dump(mode="json")},
    )


def _line_upgrade_items(
    instance: PlaybillInstance, *, access_profile: CoverageAccessProfile
) -> tuple[PlaybillNextItemV1, ...]:
    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.consumers.next.line_upgrades import disabled_upgrades, unenabled_lines

    # A Line starts disabled: until its first enablement, name the enable.
    added = tuple(
        _item(
            severity="warning",
            reason="line_not_enabled",
            subject_identity=line,
            detail={"detail": "accepted but never enabled; enable it to run on its Triggers"},
            repair=PlaybillNextRepairV1(
                operation="cruxible.line.enable",
                target=line,
                required_change="enable_the_line_to_run_it_on_its_triggers",
                arguments={"line": line.removeprefix("Line:")},
            ),
        )
        for line in unenabled_lines(instance)
    )
    return added + tuple(
        _item(
            severity="repair",
            reason="line_disabled_by_upgrade",
            subject_identity=line,
            detail={
                "procedure_version": version,
                "detail": f"disabled by upgrade to {version}; enable to resume",
            },
            repair=PlaybillNextRepairV1(
                operation="cruxible.line.enable",
                target=line,
                required_change="enable_the_upgraded_line_to_resume",
                arguments={"line": line.removeprefix("Line:")},
            ),
        )
        for line, version in disabled_upgrades(instance)
    )


def _line_run_failure_items(
    instance: PlaybillInstance, *, access_profile: CoverageAccessProfile
) -> tuple[PlaybillNextItemV1, ...]:
    """Lines whose latest run of a Trigger's occurrence failed, with the failure's cause.

    A manual ``line run`` is never a row (its caller saw the failure); a later
    succeeded or halted run of the Line clears it. The repair is the declared
    change for the failure code when there is one: a transport error clears
    once the target is reachable, an HTTP status is the input's to revise.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_client.contracts.repairs import DECLARED_HAND_EDIT_CHANGES
    from cruxible_core.consumers.next.run_failures import line_run_failures

    items = []
    for failure in line_run_failures(instance):
        detail: dict[str, object] = {
            "run_id": failure.run_id,
            "finalized_at": failure.finalized_at,
            "failure_code": failure.failure_code,
            "detail": "a run of a Trigger's occurrence failed; the Line's next run that "
            "succeeds or halts clears this",
        }
        if failure.cause is not None:
            detail["cause"] = failure.cause
        items.append(
            _item(
                severity="repair",
                reason="line_run_failed",
                subject_identity=failure.line,
                detail=detail,
                repair=PlaybillNextRepairV1(
                    operation="hand_edit",
                    target=failure.line,
                    required_change=DECLARED_HAND_EDIT_CHANGES.get(
                        failure.failure_code or "",
                        "read_the_failed_run_fix_its_cause_then_let_the_line_run_again",
                    ),
                ),
            )
        )
    return tuple(items)


def _line_attention_items(
    instance: PlaybillInstance,
    *,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """What enabled Lines owe explicit work after a daemon restart.

    A restart rolls an enabled Line forward-only: one row per range its
    daemon never matched, naming the exact ``line evaluate`` that covers it,
    and one row per Line whose pending work waits for ``line dispatch``.
    Automatically dispatching work matched before a restart is not done here.
    Only enabled Lines are read; a deliberate disable owes nothing.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.service.procedures.line_dispatch import line_attention

    gaps, pending = line_attention(instance, now=evaluation_time)
    items: list[PlaybillNextItemV1] = []
    for gap in gaps:
        line = gap.line.removeprefix("Line:")
        since, until = format_datetime(gap.since), format_datetime(gap.until)
        items.append(
            _item(
                severity="repair",
                reason="line_coverage_gap",
                subject_identity=gap.line,
                detail={
                    "since": since,
                    "until": until,
                    "detail": "Daemon restarted; uncovered ranges require explicit evaluation.",
                },
                repair=PlaybillNextRepairV1(
                    operation="cruxible.line.evaluate",
                    target=gap.line,
                    required_change="evaluate_the_uncovered_range_then_dispatch_what_it_finds",
                    arguments={"line": line, "since": since, "until": until},
                ),
            )
        )
    for work in pending:
        items.append(
            _item(
                severity="repair",
                reason="line_work_pending",
                subject_identity=work.line,
                detail={
                    "due": work.due,
                    "oldest_eligible_at": format_datetime(work.oldest_eligible_at),
                    "detail": (
                        "Matched before a restart or evaluated explicitly; an enabled Line "
                        "admits only what it matched itself."
                    ),
                },
                repair=PlaybillNextRepairV1(
                    operation="cruxible.line.dispatch",
                    target=work.line,
                    required_change="dispatch_the_lines_pending_work",
                    arguments={"line": work.line.removeprefix("Line:")},
                ),
            )
        )
    return tuple(items)


def _line_inbox_items(
    instance: PlaybillInstance,
    *,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """One row per Line with unread inbox items: what it sent for judgment, and how many.

    The count is the Line's volume, read from a maintained per-Line tally, so
    the row costs the same however long the inbox's history is. An item that
    recurs after a disposition counts as unread again.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.exhaust.line_inbox import InboxUnavailable, LineInboxStore

    try:
        unread = LineInboxStore(instance).unread_by_line()
    except InboxUnavailable as exc:
        return (
            _item(
                severity="repair",
                reason="line_inbox_unread",
                subject_identity="inbox",
                detail={
                    "availability": "unavailable",
                    "payload_digest": exc.payload.digest,
                    "detail": "Inbox volume is unknown because its journal is incomplete.",
                },
                repair=PlaybillNextRepairV1(
                    operation="cruxible.inbox.list",
                    target="inbox",
                    required_change="restore_inbox_payload_and_rebuild_projection",
                ),
            ),
        )
    return tuple(
        _item(
            severity="repair",
            reason="line_inbox_unread",
            subject_identity=line,
            detail={
                "unread": count,
                "detail": f"{count} new inbox item{'s' if count != 1 else ''} from {line}.",
            },
            repair=PlaybillNextRepairV1(
                operation="cruxible.inbox.list",
                target=line,
                required_change="judge_the_lines_unread_inbox_items",
                arguments={"line": line.removeprefix("Line:"), "status": "unread"},
            ),
        )
        for line, count in unread
    )


def _kit_resume_items(
    instance: PlaybillInstance,
    *,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """One row per recorded kit add whose resume refused, with the refusal and its repair.

    Read from the pending kit-resume rows alone. A kit still waiting on a provider
    install, including one that was rejected or withdrawn, is in ``kit status``
    only.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.service.kit_resume import blocked_kit_resumes

    return tuple(
        _item(
            severity="repair",
            reason="kit_resume_blocked",
            subject_identity=f"kit:{resume.kit_id}",
            detail={
                "kit_id": resume.kit_id,
                "version": resume.version,
                "actor_id": resume.actor_id,
                "recorded_at": resume.recorded_at,
                "detail": resume.detail,
            },
            repair=PlaybillNextRepairV1(
                operation="cruxible.kit.add",
                target=f"kit:{resume.kit_id}",
                required_change="resolve_the_refusal_then_add_the_kit_again",
                arguments={} if resume.source is None else {"reference": resume.source},
            ),
        )
        for resume in blocked_kit_resumes(instance)
    )


def _evidence_unavailable_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Cited Captures the store can no longer produce, as the evidence worker last saw.

    One row per Capture that live Claims still cite at this coordinate: a
    missing or rotted envelope, a rotted body, or a missing body its contract
    requires. The worker's findings are read, never recomputed here.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.consumers.next.evidence import evidence_findings

    findings = evidence_findings(instance)
    if not findings:
        return ()
    items: list[PlaybillNextItemV1] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        for finding in findings:
            claims = tuple(
                row[0]
                for row in projection.typed.connection.execute(
                    "SELECT DISTINCT u.owner_key FROM citation_uses u "
                    "JOIN claims c ON c.identity=u.owner_key "
                    "WHERE u.owner_kind='Claim' AND c.lifecycle='live' AND u.capture_digest=? "
                    "ORDER BY u.owner_key",
                    (finding.capture_digest,),
                ).fetchall()
            )
            if not claims:
                continue
            subject = f"Capture:{finding.capture_digest}"
            items.append(
                _item(
                    severity="repair",
                    reason="evidence_unavailable",
                    subject_identity=subject,
                    related_identities=claims,
                    detail={
                        "capture_digest": finding.capture_digest,
                        "part": finding.part,
                        "object_digest": finding.object_digest,
                        "state": finding.state,
                        "checked_at": format_datetime(finding.checked_at),
                    },
                    repair=PlaybillNextRepairV1(
                        operation="hand_edit",
                        target=subject,
                        required_change=(
                            "restore_the_capture_from_backup_or_recapture_and_recite_its_claims"
                        ),
                    ),
                )
            )
    return tuple(items)


def _evidence_erased_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Live Claims left with no retained evidence: every cited Capture's body erased.

    The evidence worker records them as it follows the erasure journal and the
    generations; each row is confirmed here against this coordinate (still live,
    still citing, still erased), one indexed lookup per row. The verdict is not
    reinterpreted: the row resurfaces the Claim, and whoever reads it judges
    what fresh observation, if any, it should rest on.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.consumers.next.evidence import erased_claims
    from cruxible_core.evidence.capture_erasures import capture_erased

    found = erased_claims(instance)
    if not found:
        return ()
    items: list[PlaybillNextItemV1] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for row in found:
            cited = {
                str(item[0])
                for item in connection.execute(
                    "SELECT u.capture_digest FROM citation_uses u "
                    "JOIN claims c ON c.identity=u.owner_key "
                    "WHERE u.owner_kind='Claim' AND u.owner_key=? AND c.lifecycle='live'",
                    (row.claim_identity,),
                )
            }
            captures = tuple(digest for digest in row.capture_digests if digest in cited)
            if not captures or not all(capture_erased(instance, digest) for digest in captures):
                continue
            claim_id = row.claim_identity.removeprefix("Claim:")
            content = instance.blob_at(coordinate.git_oid, claim_path(claim_id))
            if content is None:
                continue
            claim = parse_claim(content, path=claim_path(claim_id))
            citation = next(
                (
                    item.citation_id
                    for item in claim_citation_references(claim)
                    if item.capture_digest in captures
                ),
                None,
            )
            items.append(
                _item(
                    severity="warning",
                    reason="claim_evidence_erased",
                    subject_identity=row.claim_identity,
                    related_identities=tuple(sorted(f"Capture:{digest}" for digest in captures)),
                    detail={
                        "erased_capture_digests": list(captures),
                        "predicate": claim.statement.predicate,
                        "checked_at": format_datetime(row.checked_at),
                    },
                    # Re-binding moves the citation onto a fresh Capture under
                    # the same contract. The Claim can also be revised or
                    # retired as it stands (an erased body verifies on the
                    # envelope plus the erasure record); re-binding is the
                    # repair that keeps it current.
                    repair=PlaybillNextRepairV1(
                        operation="cruxible.rebind",
                        target=row.claim_identity,
                        required_change="rebind_onto_a_fresh_capture_under_the_same_contract",
                        operands_to_add=("capture", "because"),
                        arguments={
                            "claim_id": claim_id,
                            **(
                                {}
                                if citation is None
                                else {"citation": citation.partition(":")[2][:12]}
                            ),
                        },
                    ),
                )
            )
    return tuple(items)


def _prediction_items(
    instance: PlaybillInstance,
    *,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Unanswered prediction windows closed at the read instant, and unbindable anchors.

    One row per closed bound window whose own resolution journal holds no
    current answer, and one per anchor Capture whose material no longer binds
    the window its contract asks for. The worker tracks journal answers; reads
    apply the requested evaluation time to each stored window's end.
    """

    if not access_profile.permits("instance"):
        return ()
    from cruxible_core.consumers.next.predictions import settleable_windows, unbindable_anchors

    items: list[PlaybillNextItemV1] = []
    for owed in settleable_windows(instance, evaluation_time=evaluation_time):
        subject = owed.contract.identity.qualified
        event = None if owed.window.event is None else owed.window.event.model_dump(mode="json")
        items.append(
            _item(
                severity="repair",
                reason="prediction_settleable",
                subject_identity=subject,
                related_identities=(owed.hypothesis,),
                detail={
                    "bound_contract_id": owed.bound_contract_id,
                    "window": {
                        "starts_at": format_datetime(owed.window.starts_at),
                        "ends_at": format_datetime(owed.window.ends_at),
                    },
                    "anchor_event": event,
                },
                repair=PlaybillNextRepairV1(
                    operation="cruxible.prediction.settle",
                    target=subject,
                    required_change=(
                        "settle_with_the_claim_id_of_an_accepted_observation_in_its_window"
                    ),
                    # The bound contract id names the exact window; the daemon
                    # resolves its contract and anchor from it.
                    arguments={"prediction_id": owed.bound_contract_id},
                ),
            )
        )
    for anchor in unbindable_anchors(instance):
        subject = anchor.contract.identity.qualified
        items.append(
            _item(
                severity="repair",
                reason="prediction_window_unbindable",
                subject_identity=subject,
                related_identities=(anchor.hypothesis,),
                detail={
                    "anchor_event": anchor.event.model_dump(mode="json"),
                    "code": anchor.code,
                },
                repair=PlaybillNextRepairV1(
                    operation="hand_edit",
                    target=subject,
                    required_change="restore_the_anchor_capture_material_or_retire_the_contract",
                ),
            )
        )
    return tuple(items)


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
                "publication_command": "cruxible ledger publish --json",
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


def _compiler_health(instance: PlaybillInstance) -> PlaybillNextHealthV1:
    """Whether accepted state runs an older compiler than this process installs.

    Measured at the accepted head, where an upgrade is proposed. Installing a
    newer compiler never switches accepted state onto it: only an approved
    upgrade proposal does, and only along an explicit forward edge. A compiler
    with no edge to the running one -- newer, or retired -- is reported and
    left alone.
    """

    accepted = instance.accepted_coordinate().compiler
    running = current_compiler_coordinate()
    detail = {
        "accepted_compiler_digest": accepted.rule_digest,
        "accepted_compiler_revision": COMPILER_REVISION_LABELS.get(accepted),
        "running_compiler_digest": running.rule_digest,
        "running_compiler_revision": COMPILER_REVISION_LABELS.get(running),
    }
    if accepted == running:
        return _claim_type_upgrade_health(instance, detail)
    try:
        upgrade_law(accepted, running)
    except ValueError:
        return PlaybillNextHealthV1(state="no_upgrade_path", detail=detail)
    # The bare command targets the compiler this daemon runs (the facet's
    # `running_compiler_digest`) and names the proposal after its revision.
    upgrade = PlaybillNextRepairV1(
        operation="cruxible.compiler.upgrade",
        target=instance.descriptor.instance_id,
        required_change="propose_approve_and_activate_the_compiler_upgrade",
    )
    return PlaybillNextHealthV1(
        state="upgrade_available",
        detail=detail,
        repair=upgrade.model_copy(
            update={"command": _repair_command(upgrade.operation, arguments=upgrade.arguments)}
        ),
    )


#: The pending ClaimTypes a compiler facet names; ``count`` says how many in all.
_PENDING_CLAIM_TYPES_SHOWN = 20


def _claim_type_upgrade_health(
    instance: PlaybillInstance, detail: Mapping[str, object]
) -> PlaybillNextHealthV1:
    """A current compiler, or the ClaimType upgrade it leaves to run.

    Compiler revision 36 writes only v4 identity evidence policies. A ClaimType
    accepted with a v3 policy keeps working as it is, but is edited only after
    the governed `claim-type upgrade` restates its policy. The genesis seeds a
    new instance starts with are not counted: nothing is pending on an instance
    the moment it is created.
    """

    head = instance.accepted_coordinate()
    if head.compiler != LIGHT_ARTIFACTS_COMPILER:
        return PlaybillNextHealthV1(state="current", detail=detail)
    pending = instance.pending_policy_restatements(head.git_oid)
    if not pending:
        return PlaybillNextHealthV1(state="current", detail=detail)
    upgrade = PlaybillNextRepairV1(
        operation="cruxible.claim-type.upgrade",
        target=instance.descriptor.instance_id,
        required_change="propose_approve_and_activate_the_claim_type_upgrade",
    )
    return PlaybillNextHealthV1(
        state="claim_type_upgrade_available",
        detail={
            **detail,
            "count": len(pending),
            "claim_types": list(pending[:_PENDING_CLAIM_TYPES_SHOWN]),
            "message": (
                "these ClaimTypes state a v3 evidence policy and keep working as they are; "
                "editing one waits for `cruxible claim-type upgrade` to restate it as v4"
            ),
        },
        repair=upgrade.model_copy(
            update={"command": _repair_command(upgrade.operation, arguments=upgrade.arguments)}
        ),
    )


#: One `line dispatch` admits at most this many occurrences.
_LINE_DISPATCH_LIMIT = 100


def _line_dispatch_health(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfile,
) -> PlaybillNextHealthV1:
    """Line occurrences evaluated into the dispatch set and not yet admitted.

    Read from the dispatch projection's unresolved index at the evaluation
    time, never at a coordinate: the set is operational, and nothing admits it
    implicitly. An occurrence whose window has not closed is `waiting`; one
    that could be admitted now makes the facet `due`, and the repair names the
    Line holding the oldest such occurrence. Only live Lines count: a retired
    Line's pending work can never be admitted, so it is neither due nor a
    repair anyone could run.
    """

    if not access_profile.permits("instance"):
        return PlaybillNextHealthV1(state="not_observed")
    # Opening the store creates its directory; an instance that never evaluated
    # a Line has nothing pending and keeps no dispatch state.
    if not dispatch_root(instance).exists():
        return PlaybillNextHealthV1(state="idle")
    with LineDispatchStore(instance).locked() as connection:
        rows = connection.execute(
            "SELECT line_id,SUM(eligible_at<=?),COUNT(*),"
            "MIN(CASE WHEN eligible_at<=? THEN eligible_at END),MIN(eligible_at) "
            "FROM pending WHERE disposition='pending' GROUP BY line_id ORDER BY line_id",
            (format_datetime(evaluation_time),) * 2,
        ).fetchall()
    if rows:
        with instance.bind_accepted_projection(coordinate) as projection:
            live = {
                row[0]
                for row in projection.typed.connection.execute(
                    "SELECT identity_digest FROM lines "
                    "WHERE lifecycle='live' AND identity_digest IN ("
                    + ",".join("?" for _ in rows)
                    + ")",
                    tuple(row[0] for row in rows),
                ).fetchall()
            }
        rows = [row for row in rows if row[0] in live]
    if not rows:
        return PlaybillNextHealthV1(state="idle")
    lines = [
        {
            "line_identity_digest": line_id,
            "due": due,
            "waiting": pending - due,
            "oldest_eligible_at": oldest,
        }
        for line_id, due, pending, _oldest_due, oldest in rows
    ]
    detail = {
        "due": sum(line["due"] for line in lines),
        "waiting": sum(line["waiting"] for line in lines),
        "lines": lines,
    }
    due_lines = [(oldest_due, line_id, due) for line_id, due, _, oldest_due, _ in rows if due]
    if not due_lines:
        return PlaybillNextHealthV1(state="waiting", detail=detail)
    _oldest, line_id, due = min(due_lines)
    dispatch = PlaybillNextRepairV1(
        operation="cruxible.line.dispatch",
        target=line_id,
        required_change="dispatch_the_due_line_occurrences",
        arguments={"line": line_id, "limit": min(due, _LINE_DISPATCH_LIMIT)},
    )
    return PlaybillNextHealthV1(
        state="due",
        detail=detail,
        repair=dispatch.model_copy(
            update={"command": _repair_command(dispatch.operation, arguments=dispatch.arguments)}
        ),
    )


def _triggers_health(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
) -> PlaybillNextHealthV1:
    """Which internal actions a live Trigger schedules, read at the coordinate.

    Informational, never blocking: an unscheduled action simply never fires,
    so the worker that follows it (evidence sweeps, anchor retries) stops
    advancing. The repair is authoring a Trigger aimed at it.
    """

    if not access_profile.permits("instance"):
        return PlaybillNextHealthV1(state="not_observed")
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.typed.connection.execute(
            "SELECT target,identity FROM triggers WHERE target_kind='action' "
            "AND lifecycle='live' ORDER BY target,identity"
        ).fetchall()
    scheduled: dict[str, list[str]] = {}
    for action, identity in rows:
        scheduled.setdefault(action, []).append(identity)
    from cruxible_core.server.registry import get_registry

    record = get_registry().get(instance.descriptor.instance_id)
    delivery_enabled = record is not None and record.floor_delivery
    # Workspace delivery defaults on when attached: an instance with it disabled
    # needs no floor schedule. Findings actions retain their existing advisory.
    # An opt-in action (curation detection, beta) is expected to be unscheduled.
    unscheduled = [
        action
        for action, spec in INTERNAL_ACTIONS.items()
        if action not in scheduled
        and not spec.opt_in
        and (spec.effect == "findings" or delivery_enabled)
    ]
    detail: dict[str, object] = {"scheduled": scheduled}
    if not unscheduled:
        return PlaybillNextHealthV1(state="scheduled", detail=detail)
    admits = any(
        entry.kind == "trigger"
        for entry in artifact_kinds_for_compiler(coordinate.compiler).entries()
    )
    detail.update(
        unscheduled=unscheduled,
        message="; ".join(f"no trigger schedules {action}" for action in unscheduled),
    )
    author = PlaybillNextRepairV1(
        operation="cruxible.authoring.example",
        target=unscheduled[0],
        required_change=(
            "author_a_trigger_aimed_at_the_unscheduled_action"
            if admits
            else "upgrade_the_compiler_then_author_a_trigger_aimed_at_the_unscheduled_action"
        ),
        arguments={"example": "trigger"},
    )
    return PlaybillNextHealthV1(
        state="unscheduled",
        detail=detail,
        repair=author.model_copy(
            update={"command": _repair_command(author.operation, arguments=author.arguments)}
        ),
    )


def _consumers_health(
    instance: PlaybillInstance,
    healths: tuple[ConsumerHealth, ...],
    *,
    access_profile: CoverageAccessProfile,
    running: bool,
) -> PlaybillNextHealthV1:
    """How current the built-in workers' findings are that some rows are read from.

    Rows such as `evidence_unavailable` are what a worker last observed, not a
    computation at read time, so this says how far behind that observation is.
    Without a running consumer loop -- a library read, a daemon shutting down --
    those rows are as of each worker's last pass and nothing is advancing them.
    """

    if not access_profile.permits("instance"):
        return PlaybillNextHealthV1(state="not_observed")
    from cruxible_core.consumers.runner import consumer_kinds

    workers: list[dict[str, object]] = []
    for kind in consumer_kinds():
        if kind.effect_class not in {"findings", "workspace_output"}:
            continue
        if not kind.active(instance):
            if kind.effect_class == "findings":
                workers.append({"kind": kind.name, "state": "disabled"})
            continue
        workers.extend(
            {"kind": health.kind, "state": health.state, **health.detail}
            for health in healths
            if health.kind == kind.name
        )
    # Enabled Lines are governed consumers: their health is not a finding the
    # facet's state is about, but an instance caller reads it here without
    # the daemon's registry.
    arms = Counter(health.state for health in healths if health.kind == "line")
    states = {str(worker["state"]) for worker in workers}
    state = (
        "not_running"
        if not running
        else "stalled"
        if "stalled" in states
        else "lagging"
        if "lagging" in states
        else "current"
    )
    detail: dict[str, object] = {"workers": workers}
    if arms:
        detail["line_enablements"] = {
            "running": arms["running"],
            "stalled": arms["stalled"],
            "stopped": arms["stopped"],
        }
    return PlaybillNextHealthV1(state=state, detail=detail)


def _procedure_catalog_health(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
    observation: NextWorkspaceObservation | None,
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
    if coverage.coordinate.model_dump(mode="json") != AcceptedCoordinate.from_internal(
        coordinate
    ).model_dump(mode="json"):
        return PlaybillNextHealthV1(state="not_observed")
    policy = upgrade_presentation_policy(observation.presentation_policy or PresentationPolicyV1())
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
            target=".cruxible/files.yaml",
            required_change="add_procedure_projection_catalog_entries",
            arguments={"catalog_entries": entries},
        ),
    )


def _floor_health(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    observation: NextWorkspaceObservation | None,
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
        operation="cruxible.floor.export",
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
    access_profile: CoverageAccessProfile,
    observation: NextWorkspaceObservation | None,
) -> tuple[PlaybillNextItemV1, ...]:
    if observation is None or not access_profile.permits("instance"):
        return ()
    items: list[PlaybillNextItemV1] = [
        _item(
            severity="repair",
            reason="workspace_binding_missing",
            subject_identity=binding.source_id,
            related_identities=(
                () if binding.document_id is None else (f"document:{binding.document_id}",)
            ),
            detail={
                "source_id": binding.source_id,
                "document_id": binding.document_id,
                "locator": binding.locator,
                "message": "the file catalog binds a workspace file that does not exist",
            },
            repair=PlaybillNextRepairV1(
                operation="hand_edit",
                target=binding.locator,
                required_change="restore_the_bound_file_or_fix_its_catalog_locator",
                arguments={"source_id": binding.source_id, "locator": binding.locator},
            ),
        )
        for binding in observation.missing_bindings
    ]
    if observation.source_observations is None:
        return tuple(items)
    tree = ClaimVerdictReadContext(instance, coordinate).tree
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
                # The catalogued file is the Document's next revision: propose
                # it through the source road, which compiles the file's exact
                # bytes against the accepted Document.
                repair=PlaybillNextRepairV1(
                    operation="cruxible.files.propose",
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


def _prior_refusal_detail(instance: PlaybillInstance, proposal_id: str) -> dict[str, object]:
    """How many earlier refusals a proposal's Claims match, when any do.

    A neutral count of receipts (`proposal review` shows each, as recorded); the
    row's reason and repair do not change.
    """

    receipts, coverage = instance.refusal_index().prior_refusals_with_coverage(
        instance, proposal_id
    )
    count = sum(len(items) for items in receipts.values())
    detail: dict[str, object] = {"prior_refusal_count": count} if count else {}
    if coverage is not None:
        # Neutral: the count covers retirements through this generation only.
        detail["prior_refusals_indexed_through"] = coverage.indexed_through_generation
    return detail


def _proposal_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    caller_principal_id: str | None,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Stale proposals: admitted work the head changed what it touches since its base.

    A proposal whose base merely moved is not here: nothing it touches changed,
    so approval or activation lands it at the head, and it shows as open. A
    stale one names its ``stale_paths``. Readmit recompiles retained
    authoring/write or Procedure inputs at the head, then follows ordinary
    approval policy; exact-byte origins retain byte readmission. Withdraw
    closes work that should never settle. The queue only explains the repair:
    it never recompiles while listing a row.

    Only the author may readmit, so a row shows only to its author: the
    daemon's authenticated caller, never a request field. A queue read
    without one -- an unattributed request -- has no rows here, as with
    approvals.
    """

    if caller_principal_id is None or not access_profile.permits("instance"):
        return ()
    evidence = instance.proposal_evidence()
    items: list[PlaybillNextItemV1] = []
    for proposal in stale_unreadmitted_proposals(
        instance, coordinate, actor_id=caller_principal_id
    ):
        detail: dict[str, object] = {
            "actor_id": proposal.actor_id,
            "admitted_at": proposal.admitted_at,
            "candidate_parent_semantic_root": proposal.candidate_parent_semantic_root,
            "accepted_semantic_root": coordinate.semantic_root,
            "target_ref": proposal.target_ref,
            "stale_paths": list(proposal.stale_paths),
            **_prior_refusal_detail(instance, proposal.proposal_id),
        }
        required_change = "readmit_as_its_author_or_withdraw_the_stale_proposal"
        detail["stale_repair"] = (
            "The head changed what it touches since its base (stale_paths): readmit "
            "recompiles at the head when the origin lowers; review any new candidate "
            "normally, or withdraw."
        )
        # A delegated settle re-lowers retained input and re-verifies its
        # mandate and condition at the head before publication. No authority
        # or human approval carries from the stale candidate.
        settle = evidence.read_admission(proposal.proposal_id).settle_submission
        if settle is not None:
            detail["settle_submission"] = {
                "mode": settle.mode,
                "mandate_digest": settle.mandate_digest,
            }
            if settle.mode == "delegated":
                required_change = (
                    "activate_or_readmit_to_recompile_and_reverify_the_settle_or_withdraw_it"
                )
        items.append(
            _item(
                severity="repair",
                reason="proposal_stale",
                subject_identity=proposal.proposal_id,
                related_identities=(proposal.target_ref,),
                detail=detail,
                repair=PlaybillNextRepairV1(
                    operation="cruxible.proposal.readmit",
                    target=proposal.proposal_id,
                    required_change=required_change,
                    arguments={"proposal_id": proposal.proposal_id},
                ),
            )
        )
    return tuple(items)


def _approval_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    caller_principal_id: str | None,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """Open proposals waiting on an approval the calling principal may give.

    The principal is the daemon's authenticated caller, never a request field,
    and must be active and ordinary in the coordinate's registry: only such a
    signer's approval counts toward a candidate's requirement. A queue read
    without one -- an unattributed request -- has no rows here. Approving
    closes the row; so does another signer meeting the requirement first,
    since the candidate then waits on activation instead.
    """

    if caller_principal_id is None or not access_profile.permits("instance"):
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        ordinary = frozenset(
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT principal_id FROM principals WHERE status='active' AND kind='ordinary'"
            )
        )
    return tuple(
        _item(
            severity="repair",
            reason="proposal_awaiting_approval",
            subject_identity=proposal.proposal_id,
            related_identities=(proposal.target_ref,),
            detail={
                "actor_id": proposal.actor_id,
                "admitted_at": proposal.admitted_at,
                "candidate_digest": proposal.candidate_digest,
                "signer_id": caller_principal_id,
                "eligible_approvals": proposal.eligible_approvals,
                "minimum_distinct_signers": proposal.minimum_distinct_signers,
                "target_ref": proposal.target_ref,
                **_prior_refusal_detail(instance, proposal.proposal_id),
            },
            repair=PlaybillNextRepairV1(
                operation="cruxible.proposal.approve",
                target=proposal.proposal_id,
                required_change="review_and_approve_the_candidate_with_your_signing_key",
                arguments={
                    "proposal_id": proposal.proposal_id,
                    "signer_id": caller_principal_id,
                },
            ),
        )
        for proposal in proposals_awaiting_approval(
            instance,
            AcceptedCoordinate.from_internal(coordinate),
            principal_id=caller_principal_id,
            ordinary_principal_ids=ordinary,
        )
    ) + _stale_approval_items(
        instance,
        coordinate=coordinate,
        caller_principal_id=caller_principal_id,
        ordinary=ordinary,
    )


def _stale_approval_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    caller_principal_id: str,
    ordinary: frozenset[str],
) -> tuple[PlaybillNextItemV1, ...]:
    """Stale proposals the caller would approve: stale, awaiting the author's readmit.

    Approvals are signed over the exact candidate, so a stale one is never
    approvable: the row offers no approve. Its repair, readmit, is withheld
    from the approver as the author's alone (``because: author``); once the
    author readmits, the successor shows here as an ordinary approval row.
    The author's own stale proposals are their ``proposal_stale`` rows.
    """

    tool = _REPAIR_TOOLS["cruxible.proposal.readmit"]
    assert tool is not None
    requires = PlaybillNextRepairRequirementV1(
        operation="cruxible.proposal.readmit",
        tool=tool,
        tier=_TIER_BY_RUNG[min(_tool_rung(tool), len(_TIER_BY_RUNG) - 1)],
        because=("author",),
    )
    return tuple(
        _item(
            severity="warning",
            reason="proposal_stale",
            subject_identity=proposal.proposal_id,
            related_identities=(proposal.target_ref,),
            detail={
                "actor_id": proposal.actor_id,
                "admitted_at": proposal.admitted_at,
                "awaiting": "author_readmit",
                "candidate_digest": proposal.candidate_digest,
                "candidate_parent_semantic_root": proposal.candidate_parent_semantic_root,
                "accepted_semantic_root": coordinate.semantic_root,
                "signer_id": caller_principal_id,
                "eligible_approvals": eligible,
                "minimum_distinct_signers": minimum,
                "target_ref": proposal.target_ref,
                "stale_paths": list(proposal.stale_paths),
                "detail": "stale: the head changed what it touches since its base "
                "(stale_paths); it awaits its author's readmit, then your review. A stale "
                "candidate is never approvable.",
                **_prior_refusal_detail(instance, proposal.proposal_id),
            },
            repair=None,
            repair_requires=requires,
        )
        for proposal, eligible, minimum in stale_proposals_awaiting_approval(
            instance,
            AcceptedCoordinate.from_internal(coordinate),
            principal_id=caller_principal_id,
            ordinary_principal_ids=ordinary,
        )
        if proposal.actor_id != caller_principal_id
    )


def _activation_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    caller_principal_id: str | None,
    access_profile: CoverageAccessProfile,
) -> tuple[PlaybillNextItemV1, ...]:
    """The calling principal's own open proposals that activation is next for.

    The caller is the daemon's authenticated principal, as for approval rows.
    A proposal it authored that needs no approval, or whose requirement other
    signers met, waits only on activation; activating it closes the row. A
    proposal still waiting on others' approval is theirs to approve and shows
    in their queues as ``proposal_awaiting_approval``.
    """

    if caller_principal_id is None or not access_profile.permits("instance"):
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        ordinary = frozenset(
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT principal_id FROM principals WHERE status='active' AND kind='ordinary'"
            )
        )
    return tuple(
        _item(
            severity="repair",
            reason="proposal_awaiting_activation",
            subject_identity=proposal.proposal_id,
            related_identities=(proposal.target_ref,),
            detail={
                "admitted_at": proposal.admitted_at,
                "candidate_digest": proposal.candidate_digest,
                "approvals": proposal.approvals,
                "target_ref": proposal.target_ref,
                **_prior_refusal_detail(instance, proposal.proposal_id),
            },
            repair=PlaybillNextRepairV1(
                operation="cruxible.proposal.activate",
                target=proposal.proposal_id,
                required_change=(
                    "activate_your_proposal_its_approvals_are_met"
                    if proposal.approval_required
                    else "activate_your_proposal_it_needs_no_approval"
                ),
                arguments={"proposal_id": proposal.proposal_id},
            ),
        )
        for proposal in proposals_awaiting_activation(
            instance,
            AcceptedCoordinate.from_internal(coordinate),
            principal_id=caller_principal_id,
            ordinary_principal_ids=ordinary,
        )
    )


def _mandate_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    expiring_within: CanonicalDuration,
    access_profile: CoverageAccessProfile,
    expiring: tuple[str, ...] | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Live ProcedureMandates whose validity window closes within the lead time.

    A mandate is the standing authority a Procedure's terminals run under, and
    nothing renews it: once `expires_at` passes every run it covers refuses as
    `procedure_mandate_expired`. A suspended mandate authorizes nothing already,
    so its lapse changes nothing and is not reported. ``expiring`` names them
    from the next worker's expiry index, at the head; without it the projection
    is read.
    """

    if not access_profile.permits("instance"):
        return ()
    start = utc_microseconds(evaluation_time)
    assert start is not None
    items: list[PlaybillNextItemV1] = []
    with instance.bind_accepted_projection(coordinate) as projection:
        if expiring is None:
            rows = projection.typed.connection.execute(
                "SELECT identity,artifact_digest,procedure_identity FROM procedure_mandates "
                "WHERE lifecycle='live' AND expires_at_us>? AND expires_at_us<=? ORDER BY identity",
                (start, start + expiring_within.microseconds),
            ).fetchall()
        else:
            rows = [
                row
                for identity in expiring
                for row in projection.typed.connection.execute(
                    "SELECT identity,artifact_digest,procedure_identity FROM procedure_mandates "
                    "WHERE identity=?",
                    (identity,),
                )
            ]
        for identity, digest, procedure_identity in rows:
            mandate = projection.typed.source(identity)
            if not isinstance(mandate, ProcedureMandateV1 | ProcedureMandate):
                raise NextAcceptedStateInvalid(
                    f"{NextAcceptedStateInvalid.code}: accepted ProcedureMandate "
                    f"{identity} has no valid source"
                )
            if isinstance(mandate, ProcedureMandate) and mandate.suspended:
                continue
            items.append(
                _item(
                    severity="warning",
                    reason="mandate_expiring",
                    subject_identity=identity,
                    related_identities=(procedure_identity,),
                    detail={
                        "mandate_digest": digest,
                        "procedure_identity": procedure_identity,
                        "grants": (
                            mandate.grants if isinstance(mandate, ProcedureMandate) else None
                        ),
                        "valid_from": format_datetime(mandate.valid_from),
                        "expires_at": format_datetime(mandate.expires_at),
                    },
                    repair=PlaybillNextRepairV1(
                        operation="cruxible.authoring.example",
                        target=identity,
                        required_change="author_a_successor_mandate_or_retire_it",
                        arguments={
                            "example": "procedure-mandate",
                            "mandate_name": mandate.identity.name,
                        },
                    ),
                )
            )
    return tuple(items)


def _undeclared_kind_items(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access_profile: CoverageAccessProfile,
    live_kinds: tuple[tuple[str, int, str], ...] | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Kinds with live Subjects that no SubjectKind has ever declared (identity D1).

    Such a kind's Subjects stay readable and claimable, but no new one can be
    created until a SubjectKind says how core names it. A kind whose SubjectKind
    was retired was closed on purpose and is not reported. ``live_kinds`` are the
    next worker's per-kind counts, at the head; without them the Subject rows
    are grouped once.
    """

    from cruxible_core.indexes.subject_identity import admits_subject_kinds

    if not access_profile.permits("instance") or not admits_subject_kinds(coordinate.compiler):
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        live = (
            connection.execute(
                "SELECT subject_kind, count(*), min(path) FROM subjects WHERE lifecycle='live' "
                "GROUP BY subject_kind ORDER BY subject_kind"
            ).fetchall()
            if live_kinds is None
            else live_kinds
        )
        declared = {
            str(row[0]).removeprefix("SubjectKind:")
            for row in connection.execute("SELECT identity FROM subject_kinds")
        }
    items: list[PlaybillNextItemV1] = []
    for kind, count, first in live:
        if str(kind) in declared:
            continue
        # The row is about the kind, which has no artifact yet: it names the
        # kind's first live Subject, which `get` reads.
        identity = "Subject:" + str(first).removeprefix("subjects/").removesuffix(".json")
        items.append(
            _item(
                severity="warning",
                reason="subject_kind_undeclared",
                subject_identity=identity,
                detail={
                    "kind": str(kind),
                    "live_subjects": int(count),
                    # The single-key unique shape keeps `kind/<slug>` working:
                    # author the slug ClaimType too if the kind has none.
                    "author": {
                        "kind": "subject_kind",
                        "name": str(kind),
                        "keys": [f"{kind}.slug"],
                        "uniqueness": "unique",
                    },
                },
                repair=PlaybillNextRepairV1(
                    operation="cruxible.authoring.example",
                    target=f"SubjectKind:{kind}",
                    required_change="author_a_subject_kind_declaring_how_new_subjects_are_named",
                    arguments={"example": "subject-kind"},
                ),
            )
        )
    return tuple(items)


def _registered_blocks(
    instance: PlaybillInstance,
) -> dict[tuple[str, str], ProjectionBlockRegistration] | None:
    """Fold every block this instance registers, keyed on the pair the page names."""

    return registered_projection_blocks(instance)


def _projection_marker_invalid_item(
    *,
    source_id: str,
    block_id: str | None,
    marker_status: Literal["invalid", "registered_marker_missing"],
) -> PlaybillNextItemV1:
    target = f"{source_id}#{block_id}" if block_id is not None else source_id
    detail: dict[str, str] = {
        "source_id": source_id,
        "error_code": "cruxible.projection.marker_invalid",
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
                "cruxible.block.depublish"
                if marker_status == "registered_marker_missing"
                else "cruxible.block.repin"
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
    access_profile: CoverageAccessProfile,
    observation: NextWorkspaceObservation | None,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    facts_reader: _AcceptedQueryFactsRead | None = None,
    resolution_statuses: Mapping[str, str] | None = None,
    served: ServedRows | None = None,
) -> tuple[PlaybillNextItemV1, ...]:
    """Report shared currency assessments without suppressing sibling findings.

    With ``served`` stored rows, the Claim backings' statuses and visibility are
    the maintained ones the same read took, and each query backing is evaluated
    over only its own predicates' facts, as the query verb serves it: query
    evaluation reads the verdict inputs of the Claims it selects (and their
    Subjects and Providers), which next's facts do not keep, so no maintained
    state can stand in for it. Folding, they share the request's facts.
    """

    if (
        observation is None
        or observation.source_observations is None
        or not access_profile.permits("instance")
    ):
        return ()
    observed_sources = tuple(observation.source_observations)
    if not observed_sources:
        return ()

    # One fold per `next`: the queue used to reach it from three places and once
    # more per syncable block.
    folded = _registered_blocks(instance)
    registrations: frozenset[tuple[str, str]] | None = None if folded is None else frozenset(folded)
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
        facts_reader=facts_reader if served is None else None,
        verdicts_by_identity=verdicts_by_identity,
        resolution_statuses=resolution_statuses,
        backed_states=None if served is None else served.backed,
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
                            operation="cruxible.block.repin",
                            target=target,
                            required_change="remove_or_register_projection_block",
                            arguments={
                                "source_id": source.source_id,
                                "block_id": marker.stamp.block_id,
                            },
                            # A block taken down by depublish is detached: its
                            # markers come off and its body stays as prose. The
                            # page path is the client's, so detach names it.
                            alternatives=(
                                PlaybillNextRepairV1(
                                    operation="cruxible.block.detach",
                                    target=target,
                                    required_change="detach_projection_block",
                                    arguments={
                                        "source_id": source.source_id,
                                        "block_id": marker.stamp.block_id,
                                    },
                                ),
                            ),
                        ),
                    )
                )
            assessment = checks.read(BlockSyncReadRequest(stamp=marker.stamp))
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
                            operation="cruxible.block.repin",
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
                            operation="cruxible.block.repin",
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
                                operation="cruxible.block.depublish",
                                target=target,
                                required_change=f"depublish_{change}_backing_block",
                                arguments=arguments,
                            )
                            if exhausted
                            else PlaybillNextRepairV1(
                                operation="cruxible.block.repin",
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
                            operation="cruxible.block.repin",
                            target=target,
                            required_change="review_block_supersede_prose_then_repin",
                            arguments=arguments,
                        ),
                    )
                )
    return tuple(items)


class _CallerView:
    """What this caller can run, decided once per request and cached per Line."""

    def __init__(
        self,
        instance: PlaybillInstance,
        *,
        surface: NextCallerSurface | None,
        tools: tuple[str, ...] | None,
        caller_rung: int | None,
        authoring_refusal: AuthoringRefusal | None = None,
    ) -> None:
        self.instance = instance
        self.surface = surface
        self.tools = None if tools is None else frozenset(tools)
        self.caller_rung = caller_rung
        self.authoring_refusal = authoring_refusal
        self._line_rungs: dict[str, int] = {}

    def _required_rung(self, repair: PlaybillNextRepairV1, tool: str) -> int:
        static = _tool_rung(tool)
        if repair.operation == "cruxible.line.evaluate":
            # The tool's own tier is a dry-run read; enqueueing the range writes.
            return _GOVERNED_WRITE_RUNG
        if repair.operation == "cruxible.block.sync" and self.surface not in ("mcp", "sdk"):
            # The tool's own tier is a pure check. The CLI's rendering of
            # this repair runs `block sync --all` with no `--check`, which
            # converges stale rendered blocks and records their pages'
            # observations (a GOVERNED_WRITE write) by default; MCP and the
            # SDK facade never converge or record on their own, so they stay
            # at the tool's own read tier.
            return _GOVERNED_WRITE_RUNG
        if repair.operation != "cruxible.line.dispatch":
            return static
        # A Line dispatch's tier is the Line's own: an observe-only Line
        # dispatches at read, one whose runs propose or settle at governed write.
        line = repair.arguments.get("line") if isinstance(repair.arguments, Mapping) else None
        if not isinstance(line, str):
            return _GOVERNED_WRITE_RUNG
        if line not in self._line_rungs:
            from cruxible_core.service.procedures.procedure_runs import (
                line_run_target_rung,
                run_permission_rung,
            )

            try:
                self._line_rungs[line] = run_permission_rung(
                    line_run_target_rung(self.instance, line)
                )
            except CruxibleError:
                self._line_rungs[line] = _GOVERNED_WRITE_RUNG
        return max(static, self._line_rungs[line])

    def requirement(self, repair: PlaybillNextRepairV1) -> PlaybillNextRepairRequirementV1 | None:
        """What this caller lacks to run ``repair``, or ``None`` when it can run it."""

        tool = _REPAIR_TOOLS[repair.operation]
        if tool is None:
            return None
        unadvertised = self.surface == "mcp" and self.tools is not None and tool not in self.tools
        unauthored = (
            self.authoring_refusal is not None and repair.operation not in _AUTHORING_FREE_REPAIRS
        )
        if self.caller_rung is None and not unadvertised and not unauthored:
            return None
        rung = self._required_rung(repair, tool)
        because: list[NextRepairGate] = []
        if self.caller_rung is not None and self.caller_rung < rung:
            because.append("tier")
        if unadvertised:
            because.append("profile")
        if unauthored:
            because.append("authoring")
        if not because:
            return None
        return PlaybillNextRepairRequirementV1(
            operation=repair.operation,
            tool=tool,
            tier=_TIER_BY_RUNG[min(rung, len(_TIER_BY_RUNG) - 1)],
            profile="full" if unadvertised else None,
            because=tuple(because),
            authoring_refusal=self.authoring_refusal if unauthored else None,
        )

    def can_run(self, repair: PlaybillNextRepairV1) -> bool:
        return self.requirement(repair) is None

    def _shown(self, row: PlaybillNextItemV1 | PlaybillNextFindingV1) -> PlaybillNextItemV1:
        """One row or nested finding as a standalone row, as this caller sees its repair.

        A repair the caller cannot run is withheld and named by what it needs;
        one it can run is rendered for its surface.
        """

        requires = row.repair_requires if row.repair is None else self.requirement(row.repair)
        if row.repair is None or requires is not None:
            return _withheld_item(
                severity=row.severity,
                reason=row.reason,
                subject_identity=row.subject_identity,
                related_identities=row.related_identities,
                detail=row.detail,
                repair_requires=requires,
            )
        # An alternative this caller cannot run is left out; the primary's
        # requirement already speaks for the row.
        runnable = tuple(item for item in row.repair.alternatives if self.can_run(item))
        if self.surface in {None, "cli"} and len(runnable) == len(row.repair.alternatives):
            return row if isinstance(row, PlaybillNextItemV1) else _row_of(row)
        return _item(
            severity=row.severity,
            reason=row.reason,
            subject_identity=row.subject_identity,
            related_identities=row.related_identities,
            detail=row.detail,
            repair=row.repair.model_copy(
                update={
                    "command": None if self.surface not in {None, "cli"} else row.repair.command,
                    "alternatives": runnable,
                }
            ),
            surface=self.surface,
        )

    def _changes(self, row: PlaybillNextItemV1 | PlaybillNextFindingV1) -> bool:
        if self.surface not in {None, "cli"}:
            return True
        return row.repair is not None and any(
            self.requirement(item) is not None for item in (row.repair, *row.repair.alternatives)
        )

    def render(self, items: Iterable[PlaybillNextItemV1]) -> tuple[PlaybillNextItemV1, ...]:
        """Render each row's repair, and each nested finding's, for this caller.

        The one render point: it runs after holds and grouping, which read and
        rebuild rows from their findings with every repair in its default (CLI)
        rendering. No row is left out here. A repair this caller's surface, tool
        profile or tier cannot run is withheld, and the row says what running it
        requires (``repair_requires``); a CLI row whose repairs all run keeps
        its bytes.
        """

        rendered: list[PlaybillNextItemV1] = []
        for item in items:
            if not self._changes(item) and not any(map(self._changes, item.findings)):
                rendered.append(item)
                continue
            head = self._shown(item.model_copy(update={"findings": ()}))
            rendered.append(
                _with_findings(head, map(self._shown, item.findings)) if item.findings else head
            )
        return tuple(rendered)

    def health(self, health: PlaybillNextHealthV1) -> PlaybillNextHealthV1:
        repair = health.repair
        if repair is None or repair.operation == "hand_edit":
            return health
        requires = self.requirement(repair)
        if requires is not None:
            return health.model_copy(
                update={"repair": None, "repair_hidden": True, "repair_requires": requires}
            )
        if self.surface in {None, "cli"}:
            return health
        command = _repair_command(
            repair.operation,
            arguments=repair.arguments,
            surface=self.surface,
            operands=repair.operands_to_add,
        )
        return health.model_copy(update={"repair": repair.model_copy(update={"command": command})})


def _caller_queue(
    found: Iterable[PlaybillNextItemV1], caller: _CallerView, holds: _Holds | None
) -> tuple[tuple[PlaybillNextItemV1, ...], int]:
    """The caller's queue from every row found: held, grouped and rendered.

    Returns the sorted rows with the held count. Nothing is left out for the
    caller: a repair it cannot run is withheld at render, and the row stays.
    """

    kept = tuple(found)
    held = 0
    if holds is not None:
        kept, held = _apply_holds(kept, holds)
    return tuple(sorted(caller.render(_group_items(kept)), key=_item_sort_key)), held


@dataclass(frozen=True)
class _NextQueue:
    coordinate: AcceptedProjectionCoordinate
    attestation_head: str
    observed: tuple[NextDomain, ...]
    unobserved: tuple[NextDomain, ...]
    consumer_healths: tuple[ConsumerHealth, ...]
    caller: _CallerView
    items: tuple[PlaybillNextItemV1, ...]
    held: int


def _next_queue(
    instance: PlaybillInstance,
    *,
    request: NextRequest,
    caller_principal_id: str | None,
    caller_rung: int | None,
    read_context: ClaimVerdictReadContext | None = None,
    caller_authoring_refusal: AuthoringRefusal | None = None,
    heads: ReadHeads | None = None,
) -> _NextQueue:
    """The single fold for full next and its bounded attention summary: a point-in-time read.

    The read records once where every log stands -- at the coordinate and door
    head the request names (its pins, R8), else where they stand now -- and
    answers as of exactly those heads: the stored rows served at them, or the
    fold at them when the worker cannot serve (cold, disabled, already past a
    pin). Every family read after the Claim rows (workspace, projection, holds)
    reads at the same heads; body presence and re-observations count through
    the recorded positions. Writes that land meanwhile are not in this answer.
    ``heads`` an encompassing read (orient) already recorded are read at instead
    of recording them here.
    """

    from cruxible_core.consumers.next.facts import read_heads, served_claim_rows
    from cruxible_core.storage.read_positions import reading_at

    named = None if request.at is None else _resolve_coordinate(instance, request.at)
    # The stored rows serve the read; the fold serves the rest at the same heads.
    stored = (
        served_claim_rows(
            instance,
            coordinate=named,
            door_head=request.at_attestation_head_digest,
            evaluation_time=request.evaluation_time,
            expiring_within=request.expiring_within,
            citations=_citation_request(request.workspace_observation),
            backed=_backed_claims(request.workspace_observation),
            heads=heads,
        )
        if request.access_profile.permits("instance")
        else None
    )
    if stored is not None and stored.heads is not None:
        heads = stored.heads
    elif heads is None:
        heads = read_heads(instance, coordinate=named, door_head=request.at_attestation_head_digest)
    with reading_at(heads.positions):
        return _queue_at(
            instance,
            request=request,
            stored=stored,
            coordinate=heads.coordinate,
            attestation_head=heads.door_root,
            caller_principal_id=caller_principal_id,
            caller_rung=caller_rung,
            read_context=read_context,
            caller_authoring_refusal=caller_authoring_refusal,
        )


def _queue_at(
    instance: PlaybillInstance,
    *,
    request: NextRequest,
    stored: ServedRows | None,
    coordinate: AcceptedProjectionCoordinate,
    attestation_head: str,
    caller_principal_id: str | None,
    caller_rung: int | None,
    read_context: ClaimVerdictReadContext | None,
    caller_authoring_refusal: AuthoringRefusal | None,
) -> _NextQueue:
    """The queue at one read's recorded heads (``_next_queue``)."""

    store = instance.claim_attestation_evidence_store()
    public_coordinate = AcceptedCoordinate.from_internal(coordinate)
    door_events = () if stored is not None else store.fold_events(at_head=attestation_head)
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
    if stored is None:
        try:
            parsed_claims = (
                read_context.claims()
                if read_context is not None
                else tuple(
                    _claim_from_view(view)
                    for view in service_list_playbill_claims(
                        instance, at=public_coordinate, include_retired=True
                    ).claims
                )
            )
            resolution_statuses = claim_resolution_statuses(
                instance,
                claims=parsed_claims,
                at=public_coordinate,
                evaluation_time=request.evaluation_time,
                verdicts_by_identity=verdicts_by_identity,
                read_context=read_context,
            )
        except CruxibleError:
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
        served=stored,
    )
    observed = tuple(
        domain
        for domain in _ALL_DOMAINS
        if domain == "accepted_state" or domain in workspace_domains
    )
    unobserved = tuple(domain for domain in _ALL_DOMAINS if domain not in observed)
    # Upgrade findings catch up their projection on a read. Capture health
    # afterward so the first response and its continuation see the same cursor.
    line_upgrade_items = _line_upgrade_items(instance, access_profile=request.access_profile)
    consumer_healths = _consumer_healths(
        instance, evaluation_time=request.evaluation_time, access_profile=request.access_profile
    )
    found = (
        *(
            stored.rows
            if stored is not None
            else _oracle_claim_rows(
                instance,
                coordinate=coordinate,
                evaluation_time=request.evaluation_time,
                expiring_within=request.expiring_within,
                door_events=door_events,
                verdicts_by_identity=verdicts_by_identity,
                claims=parsed_claims,
                resolution_statuses=resolution_statuses,
                access_profile=request.access_profile,
                facts_reader=facts_reader,
            )
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
            served=stored,
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
            caller_principal_id=caller_principal_id,
            access_profile=request.access_profile,
        ),
        *_approval_items(
            instance,
            coordinate=coordinate,
            caller_principal_id=caller_principal_id,
            access_profile=request.access_profile,
        ),
        *_activation_items(
            instance,
            coordinate=coordinate,
            caller_principal_id=caller_principal_id,
            access_profile=request.access_profile,
        ),
        *_mandate_items(
            instance,
            coordinate=coordinate,
            evaluation_time=request.evaluation_time,
            expiring_within=request.expiring_within,
            access_profile=request.access_profile,
            expiring=None if stored is None else stored.expiring_mandates,
        ),
        *_evidence_unavailable_items(
            instance,
            coordinate=coordinate,
            access_profile=request.access_profile,
        ),
        *_evidence_erased_items(
            instance,
            coordinate=coordinate,
            access_profile=request.access_profile,
        ),
        *_undeclared_kind_items(
            instance,
            coordinate=coordinate,
            access_profile=request.access_profile,
            live_kinds=None if stored is None else stored.subject_kinds,
        ),
        *_prediction_items(
            instance, evaluation_time=request.evaluation_time, access_profile=request.access_profile
        ),
        *_consumer_stalled_items(consumer_healths),
        *_line_attention_items(
            instance, evaluation_time=request.evaluation_time, access_profile=request.access_profile
        ),
        *_line_settled_refusal_items(
            instance, coordinate=coordinate, access_profile=request.access_profile
        ),
        *line_upgrade_items,
        *_line_run_failure_items(instance, access_profile=request.access_profile),
        *_line_inbox_items(instance, access_profile=request.access_profile),
        *_kit_resume_items(instance, access_profile=request.access_profile),
    )
    caller = _CallerView(
        instance,
        surface=request.caller_surface,
        tools=request.caller_tools,
        caller_rung=caller_rung,
        authoring_refusal=caller_authoring_refusal,
    )
    holds = (
        stored.holds
        if stored is not None
        else _Holds(
            instance,
            coordinate=coordinate,
            claims=parsed_claims or (),
            door_events=door_events,
            door_history=lambda: store.events(at_head=attestation_head),
            evaluation_time=request.evaluation_time,
        )
        if (stored is not None or parsed_claims is not None)
        and request.access_profile.permits("instance")
        else None
    )
    items, held = _caller_queue(found, caller, holds)
    return _NextQueue(
        coordinate,
        attestation_head,
        observed,
        unobserved,
        consumer_healths,
        caller,
        items,
        held,
    )


@dataclass(frozen=True)
class PlaybillNextSummary:
    """A bounded view of the same grouped queue, without health, paging or digest work."""

    items: tuple[PlaybillNextItemV1, ...]
    total_items: int
    matching_item: PlaybillNextItemV1 | None


def summarize_playbill_next(
    instance: PlaybillInstance,
    *,
    request: NextRequest,
    caller_principal_id: str | None = None,
    caller_rung: int | None = None,
    match: Callable[[PlaybillNextItemV1], bool] | None = None,
    heads: ReadHeads | None = None,
) -> PlaybillNextSummary:
    """Count the queue and return its first three rows plus one optional matched row.

    Matches search the same first maximum-size page orient previously read.
    Claim rows at the current heads come from the worker's stored rows, with
    whatever it has not yet followed derived on read; a disabled or cold
    worker falls back to the full fold. Other inputs are observed per request.
    The summary reads at one coordinate, ``request.at`` or the head now, and
    at the door head the request names or the one now: a point-in-time read
    like next's, served from the stored rows while the worker has not passed
    those heads and folded there otherwise. ``heads`` an encompassing read
    already recorded are read at instead (orient's attention and its Claim
    tally answer at one set).
    """

    if request.cursor is not None or request.since_result_digest is not None:
        raise ValueError("a next summary reads the current queue, not a page or delta")
    coordinate = _resolve_coordinate(instance, request.at)
    request = request.model_copy(update={"at": AcceptedCoordinate.from_internal(coordinate)})
    queue = _next_queue(
        instance,
        request=request,
        caller_principal_id=caller_principal_id,
        caller_rung=caller_rung,
        read_context=ClaimVerdictReadContext(instance, coordinate),
        heads=heads,
    )
    matching = (
        None
        if match is None
        else next((item for item in queue.items[:NEXT_MAX_LIMIT] if match(item)), None)
    )
    return PlaybillNextSummary(queue.items[:3], len(queue.items), matching)


def service_playbill_next(
    instance: PlaybillInstance,
    *,
    request: NextRequest,
    provider_lane: ProviderLaneStatus | None = None,
    consumers_running: bool = False,
    caller_principal_id: str | None = None,
    caller_rung: int | None = None,
    caller_authoring_refusal: AuthoringRefusal | None = None,
) -> PlaybillNextResultV2:
    """Fold accepted state and explicit client observations into one repair queue.

    `caller_principal_id` is the daemon's authenticated caller, passed beside
    the request rather than inside it so no request can name someone else.
    `caller_rung` is that caller's permission tier (``PermissionMode.value -
    1``): a row whose repair the tier cannot perform, or whose MCP tool the
    request's ``caller_tools`` does not list, stays in the queue with its repair
    withheld and ``repair_requires`` naming the tool and the tier or profile it
    needs. ``None`` is an in-process caller that holds every tier.
    `caller_authoring_refusal` is why that caller cannot author here (whoami's
    refusal, less the tier, which `caller_rung` already gates): every repair
    that writes is then withheld with it, since running it would be refused.
    """

    continuation = None if request.cursor is None else _continuation_of(request.cursor)
    if continuation is not None:
        if continuation.at_head and _heads_moved(instance, continuation):
            # A queue read at the head is gone once the head moves: refuse
            # rather than fold the whole past queue only to find it moved (R8).
            raise _cursor_mismatch("the queue moved since the cursor's first page")
        request = _continued(request, continuation)
    queue = _next_queue(
        instance,
        request=request,
        caller_principal_id=caller_principal_id,
        caller_rung=caller_rung,
        caller_authoring_refusal=caller_authoring_refusal,
    )
    coordinate = queue.coordinate
    public_coordinate = AcceptedCoordinate.from_internal(coordinate)
    attestation_head = queue.attestation_head
    observed, unobserved = queue.observed, queue.unobserved
    consumer_healths, caller = queue.consumer_healths, queue.caller
    items, held = queue.items, queue.held
    terminal = instance.descriptor.decommissioned
    status = PlaybillNextStatusV1(
        blocking=terminal is not None,
        held=held,
        prior_refusal_coverage=instance.refusal_index().peek_coverage(instance),
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
        floor=caller.health(
            _floor_health(
                instance, coordinate=public_coordinate, observation=request.workspace_observation
            )
        ),
        ledger_mirror=caller.health(_ledger_mirror_health(instance)),
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
        procedure_catalog=caller.health(
            _procedure_catalog_health(
                instance,
                coordinate=coordinate,
                access_profile=request.access_profile,
                observation=request.workspace_observation,
            )
        ),
        compiler=caller.health(_compiler_health(instance)),
        line_dispatch=caller.health(
            _line_dispatch_health(
                instance,
                coordinate=coordinate,
                evaluation_time=request.evaluation_time,
                access_profile=request.access_profile,
            )
        ),
        consumers=caller.health(
            _consumers_health(
                instance,
                consumer_healths,
                access_profile=request.access_profile,
                running=consumers_running,
            )
        ),
        triggers=caller.health(
            _triggers_health(instance, coordinate=coordinate, access_profile=request.access_profile)
        ),
    )
    values = {
        "coordinate": public_coordinate,
        "evaluation_time": request.evaluation_time,
        "observed_domains": observed,
        "unobserved_domains": unobserved,
        "status": status,
        "items": items,
        "total_items": len(items),
        "attestation_head_digest": attestation_head,
    }
    provisional = PlaybillNextResultV2.model_construct(
        _fields_set=None,
        result_digest="sha256:" + "0" * 64,
        **values,
    )
    result_digest = playbill_next_result_digest(provisional)
    full = PlaybillNextResultV2.model_validate({**values, "result_digest": result_digest})
    scope = _queue_scope(
        instance,
        request,
        caller_principal_id=caller_principal_id,
        caller_rung=caller_rung,
        caller_authoring_refusal=caller_authoring_refusal,
    )
    _remember_queue(result_digest, full.items, scope=scope)
    answer = (
        full
        if request.since_result_digest is None
        else _delta_of(full, since=request.since_result_digest, scope=scope)
    )
    at_head = (
        continuation.at_head
        if continuation is not None
        else coordinate == instance.accepted_coordinate()
        and attestation_head == instance.claim_attestation_evidence_store().head()
    )
    return _page_of(answer, limit=request.limit, continuation=continuation, at_head=at_head)


# Bounded, per-process memory of which rows each queue digest stood for. A miss
# -- restart, eviction, a digest minted elsewhere -- is not an error: it yields
# the whole queue, which answers the caller's question either way. Entries are
# scoped to the instance and access profile that produced them: a delta names
# removed rows, so diffing against a queue read with wider access would hand
# this caller rows its own read withholds. The caller's principal is in the
# scope too, since approval and stale-proposal rows are that principal's alone.
_QUEUE_MEMO: OrderedDict[tuple[str, str], tuple[PlaybillNextItemV1, ...]] = OrderedDict()
_QUEUE_MEMO_LIMIT = 32
_QUEUE_MEMO_LOCK = RLock()


def _queue_scope(
    instance: PlaybillInstance,
    request: NextRequest,
    *,
    caller_principal_id: str | None,
    caller_rung: int | None = None,
    caller_authoring_refusal: AuthoringRefusal | None = None,
) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-next-queue-scope-v1",
        {
            "instance_id": instance.descriptor.instance_id,
            "access_profile": request.access_profile.model_dump(mode="json"),
            "caller_principal_id": caller_principal_id,
            # A delta names removed rows, so a queue filtered or rendered for one
            # caller never diffs against one read for another.
            "caller_surface": request.caller_surface,
            "caller_tools": None if request.caller_tools is None else sorted(request.caller_tools),
            "caller_rung": caller_rung,
            "caller_authoring_refusal": (
                None if caller_authoring_refusal is None else caller_authoring_refusal.code
            ),
        },
    ).tagged


def _remember_queue(
    result_digest: str, items: tuple[PlaybillNextItemV1, ...], *, scope: str
) -> None:
    with _QUEUE_MEMO_LOCK:
        _QUEUE_MEMO.pop((scope, result_digest), None)
        _QUEUE_MEMO[(scope, result_digest)] = items
        while len(_QUEUE_MEMO) > _QUEUE_MEMO_LIMIT:
            _QUEUE_MEMO.popitem(last=False)


def _delta_of(
    full: PlaybillNextResultV2,
    *,
    since: str,
    scope: str,
) -> PlaybillNextResultV2:
    """Return the reproducible symmetric difference from a remembered queue."""

    with _QUEUE_MEMO_LOCK:
        previous = _QUEUE_MEMO.get((scope, since))
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
    return full.model_copy(
        update={
            "items": changed,
            "total_items": len(changed),
            "delta_since": since,
            "removed_item_ids": tuple(
                sorted(previous_ids - current_ids, key=lambda item: item.encode("ascii"))
            ),
        }
    )


@dataclass(frozen=True)
class _Continuation:
    """What a page cursor pins: the answer it continues and where it stopped."""

    result_digest: str
    evaluation_time: datetime
    at: AcceptedCoordinate
    attestation_head_digest: str
    delta_since: str | None
    offset: int
    #: Whether the first page was read at the then-current accepted and door heads.
    at_head: bool


def _heads_moved(instance: PlaybillInstance, continuation: _Continuation) -> bool:
    current = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return (
        current != continuation.at
        or instance.claim_attestation_evidence_store().head()
        != continuation.attestation_head_digest
    )


def _cursor_mismatch(detail: str) -> NextCursorMismatch:
    return NextCursorMismatch(
        f"{NextCursorMismatch.code}: {detail}; read the queue again without a cursor"
    )


def _cursor(answer: PlaybillNextResultV2, *, offset: int, at_head: bool) -> str:
    return base64.urlsafe_b64encode(
        canonical_json(
            {
                "result_digest": answer.result_digest,
                "evaluation_time": format_datetime(answer.evaluation_time),
                "at": answer.coordinate.model_dump(mode="json"),
                "attestation_head_digest": answer.attestation_head_digest,
                "delta_since": answer.delta_since,
                "offset": offset,
                "at_head": at_head,
            }
        ).encode()
    ).decode()


def _continuation_of(cursor: str) -> _Continuation:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        if not isinstance(payload, dict):
            raise ValueError("cursor is not an object")
        result_digest = payload["result_digest"]
        head = payload["attestation_head_digest"]
        since = payload["delta_since"]
        offset = payload["offset"]
        at_head = payload["at_head"]
        raw_time = payload["evaluation_time"]
        # A decoded cursor is caller input: every field is typed before use.
        if (
            not isinstance(result_digest, str)
            or not isinstance(head, str)
            or not isinstance(raw_time, str)
            or not isinstance(at_head, bool)
        ):
            raise ValueError("cursor fields are malformed")
        for digest in (result_digest, head, since):
            if digest is not None:
                if not isinstance(digest, str):
                    raise ValueError("cursor digest is malformed")
                Sha256Value.from_tagged(digest)
        evaluation_time = parse_datetime(raw_time)
        if (
            evaluation_time is None
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 1
        ):
            raise ValueError("cursor continuation is malformed")
        at = AcceptedCoordinate.model_validate(payload["at"])
    except (KeyError, TypeError, ValueError, UnicodeError, ValidationError) as exc:
        raise _cursor_mismatch("the cursor is not a next page cursor") from exc
    return _Continuation(
        result_digest=result_digest,
        evaluation_time=ensure_utc(evaluation_time),
        at=at,
        attestation_head_digest=head,
        delta_since=since,
        offset=offset,
        at_head=at_head,
    )


def _continued(
    request: NextRequest,
    continuation: _Continuation,
) -> NextRequest:
    """Re-read exactly the answer the cursor's first page was answered from."""

    return request.model_copy(
        update={
            "at": continuation.at,
            "evaluation_time": continuation.evaluation_time,
            "since_result_digest": continuation.delta_since,
            "at_attestation_head_digest": continuation.attestation_head_digest,
        }
    )


def _page_of(
    answer: PlaybillNextResultV2,
    *,
    limit: int,
    continuation: _Continuation | None,
    at_head: bool,
) -> PlaybillNextResultV2:
    """Return one page of the answer, refusing a cursor minted for another one.

    The cursor's pins re-read the same queue unless accepted state, the
    workspace observation or the environment moved; any move changes the
    whole-queue digest, and a delta whose base the memo has since forgotten
    would silently become the whole queue, so both refuse rather than page a
    different answer at the old offset.
    """

    offset = 0
    if continuation is not None:
        if continuation.result_digest != answer.result_digest:
            raise _cursor_mismatch("the queue moved since the cursor's first page")
        if continuation.delta_since != answer.delta_since:
            raise _cursor_mismatch("the delta base is no longer remembered")
        if continuation.offset >= len(answer.items):
            raise _cursor_mismatch("the cursor points past the end of the answer")
        offset = continuation.offset
    rows = answer.items[offset : offset + limit]
    end = offset + len(rows)
    update: dict[str, object] = {
        "items": rows,
        "total_items": len(answer.items),
        "next_cursor": (
            None if end >= len(answer.items) else _cursor(answer, offset=end, at_head=at_head)
        ),
    }
    if answer.removed_item_ids:
        carried = frozenset(item.item_id for item in rows)
        update["removed_item_ids"] = tuple(
            item_id for item_id in answer.removed_item_ids if item_id in carried
        )
    return answer.model_copy(update=update)


__all__ = [
    "DEFAULT_EXPIRING_WITHIN_MICROSECONDS",
    "claim_unsure_holds",
    "NEXT_ITEM_ID_DOMAIN",
    "NEXT_RESULT_V2_DIGEST_DOMAIN",
    "NextAccessProfileInvalid",
    "NextAcceptedStateInvalid",
    "NextCoordinateNotAccepted",
    "NextCursorMismatch",
    "NextDriftObservation",
    "PlaybillNextItemV1",
    "NextRequest",
    "PlaybillNextResultV2",
    "NextSourceObservation",
    "NextWorkspaceObservationInvalid",
    "NextWorkspaceObservation",
    "playbill_next_item_id",
    "playbill_next_result_digest",
    "service_playbill_next",
    "validate_playbill_next_request",
]
