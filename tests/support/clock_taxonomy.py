"""Closed clock taxonomy for Cruxible contract and runtime fields.

The names here are wire-law language.  A time-bearing field has exactly one
domain; code may order values within a domain, test an instant for membership
in a validity window, or derive a window from an instant and duration.  It may
not use ordering between two instant domains as a law input.

Discovery and declaration are deliberately separate.  ``is_time_bearing_field``
is the ruled AST predicate; ``CLOCK_FIELD_DECLARATIONS`` is the declaration.  A
classifier that derived the domain from the field name would agree with itself
on every field and therefore prove nothing -- and it would be wrong: it reads
``deadline`` as an assertion, ``landed_at`` as an assertion, and the boolean
``requires_explicit_evaluation_time`` as an instant.  A field discovered here
and declared nowhere fails the architecture guard.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias

ClockDomainV1: TypeAlias = Literal[
    "SETTLEMENT ORDER",
    "ASSERTION TIME",
    "VALIDITY WINDOW",
    "EVALUATION INSTANT",
]

CLOCK_DOMAINS: frozenset[ClockDomainV1] = frozenset(
    {
        "SETTLEMENT ORDER",
        "ASSERTION TIME",
        "VALIDITY WINDOW",
        "EVALUATION INSTANT",
    }
)

TIME_FIELD_SUFFIXES = (
    "_at",
    "_time",
    "_instant",
    "_seconds",
    "_microseconds",
    "_until",
    "generation",
    "sequence",
    "timestamp",
)
_TIME_ANNOTATION_TOKENS = ("datetime", "timedelta", "CanonicalDuration")
# `generation`, `sequence` and `timestamp` name a whole field or its last
# segment. Matching a bare suffix also caught `consequence`, a Literal that
# carries no time at all. `timestamp` is the codebase's dominant canonical-time
# spelling and is usually annotated `str`, so the annotation tokens never find
# it: it is discovered by name or not at all.
_WORD_SUFFIXES = ("generation", "sequence", "timestamp")


def is_time_bearing_field(name: str, annotation: str) -> bool:
    """Return the ruled AST discovery predicate for one annotated field."""

    if any(token in annotation for token in _TIME_ANNOTATION_TOKENS):
        return True
    for suffix in TIME_FIELD_SUFFIXES:
        if suffix in _WORD_SUFFIXES:
            if name == suffix or name.endswith(f"_{suffix}"):
                return True
        elif name.endswith(suffix):
            return True
    return False


# Every field the predicate discovers, declared exactly once by the class that
# owns it. Two classes may read one field name on different clocks: a capture is
# `observed_at` the instant the daemon evaluated the source, while an attestation
# is `observed_at` the time its attestor asserts.
CLOCK_FIELD_DECLARATIONS: Mapping[tuple[str, str], ClockDomainV1] = {
    ("FloorConsumerOutcome", "generation"): "SETTLEMENT ORDER",
    ("LineTriggerBinding", "generation"): "SETTLEMENT ORDER",
    ("LineRunRequest", "trigger_generation"): "SETTLEMENT ORDER",
    # A signed mint consent is accepted only within a window around the
    # daemon's clock; issued_at anchors that window.
    ("RuntimeCredentialMintStatement", "issued_at"): "VALIDITY WINDOW",
    ("LineArm", "armed_at"): "VALIDITY WINDOW",
    ("LineArm", "evaluated_until"): "VALIDITY WINDOW",
    ("LineArm", "stopped_at"): "VALIDITY WINDOW",
    ("LineTriggerCheckRequest", "since"): "VALIDITY WINDOW",
    ("LineTriggerCheckRequest", "until"): "VALIDITY WINDOW",
    ("LineTriggerOccurrence", "eligible_at"): "VALIDITY WINDOW",
    ("LineTriggerCheckResult", "checked_since"): "VALIDITY WINDOW",
    ("LineTriggerCheckResult", "checked_until"): "VALIDITY WINDOW",
    ("FixedWindow", "starts_at"): "VALIDITY WINDOW",
    ("FixedWindow", "duration_seconds"): "VALIDITY WINDOW",
    ("CaptureEventWindow", "duration_seconds"): "VALIDITY WINDOW",
    ("BoundObservationWindow", "starts_at"): "VALIDITY WINDOW",
    ("BoundObservationWindow", "ends_at"): "VALIDITY WINDOW",
    ("TriggerEventReference", "sequence"): "SETTLEMENT ORDER",
    ("ResolutionContractActivationV3", "activated_at"): "EVALUATION INSTANT",
    ("AcceptedGenerationLocation", "sequence"): "SETTLEMENT ORDER",
    ("AcceptedGenerationLocation", "parent_sequence"): "SETTLEMENT ORDER",
    ("ArtifactVersionLocation", "occurrence_sequence"): "SETTLEMENT ORDER",
    ("AcceptedMemberLocation", "sequence"): "SETTLEMENT ORDER",
    # Canonical candidate timestamps are the author's assertion of when the
    # candidate was made; nothing checks them against a daemon clock.
    ("AuthoringIntentV1", "canonical_timestamp"): "ASSERTION TIME",
    ("PreflightCertificate", "canonical_timestamp"): "ASSERTION TIME",
    ("SemanticCandidateV1", "timestamp"): "ASSERTION TIME",
    ("SemanticCandidate", "timestamp"): "ASSERTION TIME",
    ("_MemberContext", "timestamp"): "ASSERTION TIME",
    ("AuditClaimFactorsV1", "first_accepted_generation"): "SETTLEMENT ORDER",
    ("AuditClaimFactorsV1", "last_independent_verification_generation"): "SETTLEMENT ORDER",
    ("AuditCursor", "evaluation_time"): "EVALUATION INSTANT",
    ("AuditEvidenceRefV1", "generation"): "SETTLEMENT ORDER",
    ("AuditRunV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("AuditRunV1", "evaluation_time"): "EVALUATION INSTANT",
    ("AuthoringClaimStatement", "effective_from"): "VALIDITY WINDOW",
    ("AuthoringClaimStatement", "effective_until"): "VALIDITY WINDOW",
    ("AuthoringIntentEventV1", "sequence"): "SETTLEMENT ORDER",
    ("AuthoringIntentEventV2", "sequence"): "SETTLEMENT ORDER",
    ("AuthoringIntentEventV3", "sequence"): "SETTLEMENT ORDER",
    ("BlockObservationV1", "scan_generation"): "SETTLEMENT ORDER",
    ("BoundedWindowCoherence", "max_cross_source_skew"): "VALIDITY WINDOW",
    ("CandidateStatus", "accepted_generation"): "SETTLEMENT ORDER",
    ("CaptureAcquisitionReceiptV1", "observed_at"): "EVALUATION INSTANT",
    ("CaptureAcquisitionReceiptV1", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureCursor", "sequence"): "SETTLEMENT ORDER",
    ("CaptureEnvelopeV1", "observed_at"): "EVALUATION INSTANT",
    ("CaptureEnvelopeV1", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureEnvelope", "observed_at"): "EVALUATION INSTANT",
    ("CaptureEnvelope", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureLandingEventV1", "landed_at"): "EVALUATION INSTANT",
    ("CaptureLandingEventV1", "sequence"): "SETTLEMENT ORDER",
    ("CaptureLandingEvent", "landed_at"): "EVALUATION INSTANT",
    ("CaptureLandingEvent", "sequence"): "SETTLEMENT ORDER",
    ("CaptureRetentionErasurePolicy", "minimum_retention"): "VALIDITY WINDOW",
    ("CaptureRunCoordinateV1", "bound_generation"): "SETTLEMENT ORDER",
    ("CaptureRunCoordinate", "bound_generation"): "SETTLEMENT ORDER",
    ("CaptureVerdictEvidence", "observed_at"): "ASSERTION TIME",
    ("CaptureVerdictEvidence", "source_effective_until"): "VALIDITY WINDOW",
    ("ChangeSetRecord", "sequence"): "SETTLEMENT ORDER",
    ("ChangeSetRecordV2", "sequence"): "SETTLEMENT ORDER",
    ("ChangeSetRecordV3", "sequence"): "SETTLEMENT ORDER",
    ("CheckpointGeneration", "sequence"): "SETTLEMENT ORDER",
    ("_ClaimAdjudicationRuleBase", "max_evidence_age"): "VALIDITY WINDOW",
    ("ClaimAdmissionCandidateContext", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimAttestationAppendResult", "partition_sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationAppendResult", "recorded_at"): "ASSERTION TIME",
    ("ClaimAttestationEventPayload", "recorded_at"): "ASSERTION TIME",
    ("ClaimAttestationEvent", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPartitionGenesis", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPartitionHead", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPublishedRoot", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationStatementV1", "observed_at"): "ASSERTION TIME",
    ("ClaimAttestationStatementV1", "valid_until"): "VALIDITY WINDOW",
    ("ClaimAttestationStatement", "attested_at"): "ASSERTION TIME",
    ("ClaimAttestationStatement", "valid_until"): "VALIDITY WINDOW",
    ("_UnsureHold", "attested_at"): "ASSERTION TIME",
    ("_UnsureHold", "valid_until"): "VALIDITY WINDOW",
    ("_Continuation", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimAttestationStoreManifest", "initialized_at"): "ASSERTION TIME",
    ("ClaimEvidenceFreshnessLineV1", "expires_at"): "VALIDITY WINDOW",
    ("ClaimEvidenceFreshnessLineV1", "observed_at"): "ASSERTION TIME",
    ("ClaimInput", "effective_from"): "VALIDITY WINDOW",
    ("ClaimInput", "effective_until"): "VALIDITY WINDOW",
    ("ClaimLawEvidenceV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimLawEvidence", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimQueryResult", "evaluated_at"): "EVALUATION INSTANT",
    ("ClaimQueryResult", "expires_at"): "VALIDITY WINDOW",
    ("ClaimReferentContext", "observed_at"): "ASSERTION TIME",
    ("ClaimRetireDependent", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementInput", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementMember", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementResultItemV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimStatement", "effective_from"): "VALIDITY WINDOW",
    ("ClaimStatement", "effective_until"): "VALIDITY WINDOW",
    ("ClaimTypeDependentDisposition", "claim_effective_until"): "VALIDITY WINDOW",
    ("ClaimTypeMigrationDispositionV3", "claim_effective_until"): "VALIDITY WINDOW",
    ("ClaimTypeSuccessionDependent", "claim_effective_until"): "VALIDITY WINDOW",
    ("ClaimVerdictResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimVerdictResult", "evaluation_time"): "EVALUATION INSTANT",
    ("ConsumptionAggregateV1", "consumption_epoch_generation"): "SETTLEMENT ORDER",
    ("ConsumptionAggregateV1", "observed_since_generation"): "SETTLEMENT ORDER",
    ("ConsumptionEpochV1", "consumption_epoch_generation"): "SETTLEMENT ORDER",
    ("ConsumptionObservationGapV1", "unobserved_from_generation"): "SETTLEMENT ORDER",
    ("ConsumptionObservationResumeV1", "observed_from_generation"): "SETTLEMENT ORDER",
    ("CoverageManifestFileV1", "written_at"): "ASSERTION TIME",
    ("CoverageManifestFileV2", "written_at"): "ASSERTION TIME",
    ("CurationAcceptedFixedV1", "resolved_generation"): "SETTLEMENT ORDER",
    ("CurationEvidenceRefV1", "generation"): "SETTLEMENT ORDER",
    ("CurationItemV1", "first_proposed_generation"): "SETTLEMENT ORDER",
    ("CurationItemV1", "last_observed_generation"): "SETTLEMENT ORDER",
    ("CurationItemV1", "resolved_at_generation"): "SETTLEMENT ORDER",
    ("CurationPatternObservedV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("CurationSuppressedV1", "until_generation"): "SETTLEMENT ORDER",
    ("CurationSuppressionV1", "until_generation"): "SETTLEMENT ORDER",
    ("DependencyImpactRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("DependencyImpactV1", "evaluated_at"): "EVALUATION INSTANT",
    ("DiscoveryPage", "evaluation_time"): "EVALUATION INSTANT",
    ("DiscoveryRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("EvidenceFreshnessExpiration", "expires_at"): "VALIDITY WINDOW",
    ("EvidenceFreshnessExpiration", "observed_at"): "ASSERTION TIME",
    ("ExhaustPromotionV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ExhaustPromotionV1", "last_sequence"): "SETTLEMENT ORDER",
    ("ExhaustReceiptSetManifestV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ExhaustReceiptSetManifestV1", "last_sequence"): "SETTLEMENT ORDER",
    ("ExternalSourceReadRequestV1", "observed_at"): "EVALUATION INSTANT",
    ("FloorGenerationPairV1", "current_generation"): "SETTLEMENT ORDER",
    ("FloorGenerationPairV1", "floor_generation"): "SETTLEMENT ORDER",
    # The floor carries no verdicts and no instants: a file's changed_at is the
    # accepted generation that last touched one of its inputs.
    ("FloorEntry", "changed_at"): "SETTLEMENT ORDER",
    ("FloorManifest", "generation"): "SETTLEMENT ORDER",
    ("FloorHead", "generation"): "SETTLEMENT ORDER",
    ("FloorDeltaFile", "changed_at"): "SETTLEMENT ORDER",
    ("FloorDelta", "base_generation"): "SETTLEMENT ORDER",
    ("FloorApplyResult", "generation"): "SETTLEMENT ORDER",
    ("FloorManifestFileV1", "changed_at"): "SETTLEMENT ORDER",
    ("FloorFreshnessManifestV2", "generation"): "SETTLEMENT ORDER",
    ("FloorInputs", "generation"): "SETTLEMENT ORDER",
    ("_SubjectRender", "changed_at"): "SETTLEMENT ORDER",
    ("_SubjectRender", "own_changed_at"): "SETTLEMENT ORDER",
    ("GovernedActorContext", "timestamp"): "ASSERTION TIME",
    ("InputAcquisitionRule", "max_age"): "VALIDITY WINDOW",
    ("InsertionExpectation", "expires_at"): "VALIDITY WINDOW",
    ("InsertionTerminalTombstone", "finalized_at"): "ASSERTION TIME",
    ("InsertionTerminalTombstone", "retain_until"): "VALIDITY WINDOW",
    ("JournalHeadStatementV1", "asserted_at"): "ASSERTION TIME",
    ("JournalPartitionHeadV1", "sequence"): "SETTLEMENT ORDER",
    ("JournalRangeV1", "first_sequence"): "SETTLEMENT ORDER",
    ("JournalRangeV1", "last_sequence"): "SETTLEMENT ORDER",
    ("JournalSegmentDescriptorV1", "first_sequence"): "SETTLEMENT ORDER",
    ("JournalSegmentDescriptorV1", "last_sequence"): "SETTLEMENT ORDER",
    ("JournalWriterStateV1", "generation"): "SETTLEMENT ORDER",
    ("LineEgressReadingV1", "sequence"): "SETTLEMENT ORDER",
    ("LineRunRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("CadenceTriggerPolicy", "interval_seconds"): "VALIDITY WINDOW",
    ("CadenceSchedule", "interval_seconds"): "VALIDITY WINDOW",
    ("WindowCloseTriggerPolicyV1", "window_seconds"): "VALIDITY WINDOW",
    ("MemberLawEvaluation", "evaluation_time"): "EVALUATION INSTANT",
    ("AuditEvidenceRef", "generation"): "SETTLEMENT ORDER",
    ("AuditFactors", "first_accepted_generation"): "SETTLEMENT ORDER",
    ("AuditFactors", "last_independent_verification_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("AuditResult", "audited_through_generation"): "SETTLEMENT ORDER",
    ("AuditResult", "evaluation_time"): "EVALUATION INSTANT",
    ("AuditResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditResultV1", "audited_through_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditResultV1", "generation"): "SETTLEMENT ORDER",
    ("BlockSyncReadResult", "generation"): "SETTLEMENT ORDER",
    ("BlockSyncSuccessorCandidate", "generation"): "SETTLEMENT ORDER",
    ("CandidateStatusRecord", "accepted_generation"): "SETTLEMENT ORDER",
    ("QueryReceipt", "evaluation_time"): "EVALUATION INSTANT",
    ("QueryRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("_RowRenderer", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV2", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV2", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV3", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV3", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("PlaybillClaimVerdictQueryV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimVerdictQueryV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimViewRecord", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("CurationActionResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationActionResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationListRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("CurationListResult", "evaluation_time"): "EVALUATION INSTANT",
    ("CurationListResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationListResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillCurationListResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationSuppressRequestV1", "until_generation"): "SETTLEMENT ORDER",
    ("PlaybillDocumentHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("NextRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("NextRequestV1", "expiring_within"): "VALIDITY WINDOW",
    ("NextResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillNextResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureReadiness", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunState", "evaluation_time"): "EVALUATION INSTANT",
    # When the daemon last tried to push. An assertion about an attempt, not a
    # coordinate: the mirror is a copy of accepted state and its timing orders
    # nothing.
    ("LedgerMirror", "attempted_at"): "ASSERTION TIME",
    ("LedgerMirrorStateV1", "attempted_at"): "ASSERTION TIME",
    # Request order within one mirror destination, never accepted generation numbers.
    ("LedgerMirrorStateV1", "requested_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirrorStateV1", "attempted_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirrorStateV1", "published_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirrorStateV1", "wait_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirror", "requested_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirror", "attempted_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirror", "published_sequence"): "SETTLEMENT ORDER",
    ("LedgerMirror", "wait_sequence"): "SETTLEMENT ORDER",
    ("ClaimReadBatchRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimValuesRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimValuesResult", "evaluation_time"): "EVALUATION INSTANT",
    # The instants between which a remembered slot answer holds.
    ("_RememberedSlot", "interval"): "VALIDITY WINDOW",
    ("ProposalListEntry", "admitted_at"): "ASSERTION TIME",
    ("PlaybillProposalListEntryV1", "admitted_at"): "ASSERTION TIME",
    ("PlaybillReviewOperationalEventV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("PlaybillReviewOperationalEventV1", "recorded_at"): "ASSERTION TIME",
    ("PlaybillReviewOperationalEventV1", "sequence"): "SETTLEMENT ORDER",
    # The accepted candidate's own timestamp: the author's assertion of when
    # the head generation was made, surfaced by orient beside its sequence.
    ("OrientResult", "accepted_at"): "ASSERTION TIME",
    ("OrientResult", "evaluation_time"): "EVALUATION INSTANT",
    ("OrientResult", "generation"): "SETTLEMENT ORDER",
    # The get read: its evaluation instant, and the times it reads back.
    ("GetRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("GetResult", "evaluation_time"): "EVALUATION INSTANT",
    ("GetProposalCard", "admitted_at"): "ASSERTION TIME",
    ("GetProcedureTrackRecord", "first_sequence"): "SETTLEMENT ORDER",
    ("GetProcedureTrackRecord", "last_sequence"): "SETTLEMENT ORDER",
    ("GetCaptureEvidence", "observed_at"): "ASSERTION TIME",
    ("GetAttestationEvidence", "at"): "ASSERTION TIME",
    ("GetRevision", "sequence"): "SETTLEMENT ORDER",
    ("GetCoordinate", "generation"): "SETTLEMENT ORDER",
    ("_RevisionEntry", "sequence"): "SETTLEMENT ORDER",
    # Operational reads: Lines, Captures, predictions, mandates and runs. An
    # arm's and an occurrence's instants are the dispatch store's validity
    # windows; a Capture's observed_at is its producer's assertion; a run's
    # times are its admission's evaluation instant, which the deterministic
    # executor clock stamps on every journal record of the run.
    ("GetLineArm", "armed_at"): "VALIDITY WINDOW",
    ("GetLineArm", "stopped_at"): "VALIDITY WINDOW",
    ("GetLineOccurrence", "eligible_at"): "VALIDITY WINDOW",
    ("GetCaptureCard", "observed_at"): "ASSERTION TIME",
    ("OrientCapture", "observed_at"): "ASSERTION TIME",
    ("GetPredictionWindow", "starts_at"): "VALIDITY WINDOW",
    ("GetPredictionWindow", "ends_at"): "VALIDITY WINDOW",
    ("OrientPrediction", "next_close"): "VALIDITY WINDOW",
    ("GetMandateCard", "valid_from"): "VALIDITY WINDOW",
    ("GetMandateCard", "expires_at"): "VALIDITY WINDOW",
    ("OrientMandate", "expires_at"): "VALIDITY WINDOW",
    ("RunRow", "started_at"): "EVALUATION INSTANT",
    ("GetProcedureRunCard", "started_at"): "EVALUATION INSTANT",
    ("GetRunCurrentNode", "started_at"): "EVALUATION INSTANT",
    ("GetPendingInput", "waiting_since"): "EVALUATION INSTANT",
    ("GetRunNode", "sequence"): "SETTLEMENT ORDER",
    # The accepted head a live operational read was taken at.
    ("LiveHead", "generation"): "SETTLEMENT ORDER",
    # The accepted head (or named coordinate) a head read answers.
    ("Head", "generation"): "SETTLEMENT ORDER",
    # One internal get batch, evaluated at one instant.
    ("GetBatchRequest", "evaluation_time"): "EVALUATION INSTANT",
    # The instant a query's first page pinned; its cursor continues it.
    ("_QueryCursor", "evaluation_time"): "EVALUATION INSTANT",
    ("RunLocator", "admitted_at"): "EVALUATION INSTANT",
    ("SinceCursor", "last_generation"): "SETTLEMENT ORDER",
    ("SinceCursor", "lower_generation"): "SETTLEMENT ORDER",
    ("SinceRequest", "generation"): "SETTLEMENT ORDER",
    ("SinceResult", "generation"): "SETTLEMENT ORDER",
    ("SinceRow", "generation"): "SETTLEMENT ORDER",
    ("PlaybillSubjectHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("PreparedClaimAttestationRequest", "attested_at"): "ASSERTION TIME",
    ("PreparedClaimAttestationRequest", "valid_until"): "VALIDITY WINDOW",
    ("ProcedureAcquisitionPlan", "occurrence_evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureAdmissionMaterialMember", "retain_until"): "VALIDITY WINDOW",
    ("ProcedureBudget", "wall_clock"): "VALIDITY WINDOW",
    ("ProcedureHardCaps", "max_wall_clock"): "VALIDITY WINDOW",
    ("ProcedureJournalCoordinate", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureJournalRecordDraftV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureJournalRecordV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureJournalRecordV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureMandateAuthoringPayload", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateAuthoringPayload", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMandateInput", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateInput", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMandateInvocation", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureMandateV1", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateV1", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMandate", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandate", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMeasurementDeclaration", "check_after"): "VALIDITY WINDOW",
    ("ProcedureMeasurementDeclaration", "expires_after"): "VALIDITY WINDOW",
    ("ProcedureMeasurementReviewTrigger", "window"): "VALIDITY WINDOW",
    ("ProcedureReadinessRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureReadinessResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureReadingV1", "observed_at"): "ASSERTION TIME",
    ("ProcedureReadingV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureResolutionDispositionV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureResolutionDispositionV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureResolutionV1", "observed_at"): "ASSERTION TIME",
    ("ProcedureResolutionV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureResolutionV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunAdmissionV1", "admitted_at"): "ASSERTION TIME",
    ("ProcedureRunAdmissionV3", "occurrence_evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunAttribution", "recorded_time"): "ASSERTION TIME",
    ("ProcedureRunAttributionWithheld", "recorded_time"): "ASSERTION TIME",
    ("_LinkedReadmission", "accepted_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunBudgetObserved", "wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProcedureRunIndexEntryV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunIndexEntryV1", "last_sequence"): "SETTLEMENT ORDER",
    # The daemon-configured bound on how far a caller's asserted evaluation
    # instant may sit from the daemon clock; it guards a ProcedureMandate's
    # validity window, so it is a window, not an instant.
    (
        "ProcedureRunOperationalConfigV1",
        "evaluation_instant_skew_seconds",
    ): "VALIDITY WINDOW",
    ("ProcedureRunOutcomeV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunReceiptV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunReceiptV1", "last_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunReceiptV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunReceiptV2", "first_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunReceiptV2", "last_sequence"): "SETTLEMENT ORDER",
    ("ProcedureRunReceiptV4", "occurrence_evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunStateV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ProposalAwaitingApproval", "admitted_at"): "ASSERTION TIME",
    ("_ProjectionBlockStamp", "declared_generation"): "SETTLEMENT ORDER",
    ("BlockSyncReadRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ProjectionCheckRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ProjectionCheckResult", "evaluation_time"): "EVALUATION INSTANT",
    ("BlockDeclareResult", "declared_generation"): "SETTLEMENT ORDER",
    ("BlockRepinResult", "declared_generation"): "SETTLEMENT ORDER",
    ("DeclaredBlockRegistration", "declared_generation"): "SETTLEMENT ORDER",
    # The instant the daemon recorded a workspace's declaration. It is
    # protocol state -- nothing orders, expires or evaluates by it -- but it IS
    # the instant the assertion "this instance stands behind this marker" was
    # made, so it declares the same clock every other such record does.
    ("DeclaredBlockRegistration", "declared_at"): "ASSERTION TIME",
    ("ProjectionQueryBacking", "declared_evaluation_time"): "EVALUATION INSTANT",
    ("ProposalAdmissionRecord", "admitted_at"): "ASSERTION TIME",
    ("ProposalWithdrawalRecord", "withdrawn_at"): "ASSERTION TIME",
    ("PlaybillProposalWithdrawResultV1", "withdrawn_at"): "ASSERTION TIME",
    ("ProposalWithdrawResult", "withdrawn_at"): "ASSERTION TIME",
    ("ProposalEvaluationRecord", "evaluated_at"): "EVALUATION INSTANT",
    ("ProviderBudgetTranslation", "hard_cap_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslation", "procedure_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslation", "remaining_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslation", "runtime_wall_clock_seconds"): "VALIDITY WINDOW",
    ("ProviderDriverOutcomeV1", "duration_seconds"): "VALIDITY WINDOW",
    ("ProviderInvocationReceipt", "duration_microseconds"): "VALIDITY WINDOW",
    ("ProviderResultToExternalCapture", "observed_at"): "EVALUATION INSTANT",
    ("ProviderResultToExternalCapture", "source_effective_time"): "VALIDITY WINDOW",
    ("ProviderRuntimeBudgetsV1", "wall_clock_seconds"): "VALIDITY WINDOW",
    ("ProviderSigningKey", "valid_from"): "VALIDITY WINDOW",
    ("ProviderSigningKey", "valid_until"): "VALIDITY WINDOW",
    ("PublicationPreparation", "accepted_generation"): "SETTLEMENT ORDER",
    ("PublicationPreparation", "expires_at"): "VALIDITY WINDOW",
    ("QueryEvaluationPolicy", "result_expiry"): "VALIDITY WINDOW",
    ("QueryExecutionReceipt", "evaluation_time"): "EVALUATION INSTANT",
    ("RecoveredGeneration", "sequence"): "SETTLEMENT ORDER",
    ("ReplayCheckpointBodyV2", "sequence"): "SETTLEMENT ORDER",
    ("ReplayCheckpointFileV2", "written_at"): "ASSERTION TIME",
    ("MeasurementActivationBasisV1", "activated_at"): "ASSERTION TIME",
    ("ClaimVerdictObservationV1", "observation_time"): "EVALUATION INSTANT",
    ("_Continuation", "observation_time"): "EVALUATION INSTANT",
    ("ProcedureMeasureRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureMeasureResult", "observation_time"): "EVALUATION INSTANT",
    ("ProcedureReadingsRequest", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureReadingsResult", "observation_time"): "EVALUATION INSTANT",
    ("ProcedureMeasurementEligibility", "activated_at"): "ASSERTION TIME",
    ("ProcedureMeasurementEligibility", "check_at"): "EVALUATION INSTANT",
    ("ProcedureMeasurementEligibility", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMeasurementEligibility", "observation_time"): "EVALUATION INSTANT",
    ("ProcedureMeasurementResolutionSummary", "observed_at"): "ASSERTION TIME",
    ("ProcedureMeasurementResolutionSummary", "recorded_at"): "ASSERTION TIME",
    ("ProcedureMeasurementResolutionSummary", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureReadingSummary", "observed_at"): "ASSERTION TIME",
    ("ProcedureReadingSummary", "recorded_at"): "ASSERTION TIME",
    ("ResolutionContractActivationV1", "activated_at"): "ASSERTION TIME",
    ("ResolutionContractActivationV1", "check_at"): "EVALUATION INSTANT",
    ("ResolutionContractActivationV1", "expires_at"): "VALIDITY WINDOW",
    ("ReviewOperationalHeadV1", "initialized_generation"): "SETTLEMENT ORDER",
    ("ReviewOperationalPartitionHeadV1", "sequence"): "SETTLEMENT ORDER",
    ("ReviewOperationalStoreManifestV1", "initialized_at"): "ASSERTION TIME",
    ("ReviewOperationalStoreManifestV1", "initialized_generation"): "SETTLEMENT ORDER",
    ("RuntimeCredentialMetadata", "created_at"): "ASSERTION TIME",
    ("RuntimeCredentialMetadata", "revoked_at"): "ASSERTION TIME",
    ("SettledOutcomesQueryReceiptV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ServedNestedProcedureRunner", "evaluation_time"): "EVALUATION INSTANT",
    ("SettledOutcomesQueryRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("SettledOutcomesQueryResultV1", "evaluation_time"): "EVALUATION INSTANT",
    # The instant the daemon stamped the terminal lifecycle state; nothing is
    # evaluated against it, it records when the operator's act landed.
    ("Decommission", "decommissioned_at"): "ASSERTION TIME",
    ("InstanceDecommissionResult", "decommissioned_at"): "ASSERTION TIME",
    # The instant the daemon actually read the workspace file it receipted.
    ("SourceReadReceipt", "read_at"): "EVALUATION INSTANT",
    ("SourceEffectiveTime", "effective_from"): "VALIDITY WINDOW",
    ("SourceClaimCandidateV1", "effective_from"): "VALIDITY WINDOW",
    ("SourceClaimCandidateV1", "effective_until"): "VALIDITY WINDOW",
    ("SourceEffectiveTime", "effective_until"): "VALIDITY WINDOW",
    ("SourceSelectionReceipt", "evaluation_time"): "EVALUATION INSTANT",
    ("SubjectProfileV1", "evaluation_time"): "EVALUATION INSTANT",
    ("StaleProposal", "admitted_at"): "ASSERTION TIME",
    ("TerminalChildReceiptV1", "sequence"): "SETTLEMENT ORDER",
    ("TerminalEgressRequestV1", "prepared_at"): "EVALUATION INSTANT",
    ("TerminalEgressRequestV2", "evaluation_time"): "EVALUATION INSTANT",
    ("VerifiedClaimAttestation", "recorded_at"): "ASSERTION TIME",
    ("VerifiedExhaustRecordV1", "sequence"): "SETTLEMENT ORDER",
    ("WitnessRecord", "sequence"): "SETTLEMENT ORDER",
    ("_AuditHistoryIndex", "attestation_first_generation"): "SETTLEMENT ORDER",
    ("_AuditHistoryIndex", "first_statement_generation"): "SETTLEMENT ORDER",
    ("_CaptureObservation", "generation"): "SETTLEMENT ORDER",
    ("_CaptureObservation", "observed_at"): "ASSERTION TIME",
    ("ClaimLineageNode", "generation"): "SETTLEMENT ORDER",
    ("_CurationHistoryIndex", "last_generation"): "SETTLEMENT ORDER",
    ("_StoredClaimQueue", "valid_from"): "EVALUATION INSTANT",
    ("_StoredClaimQueue", "valid_until"): "EVALUATION INSTANT",
    ("_DeterministicClock", "evaluation_time"): "EVALUATION INSTANT",
    ("_GenerationWindow", "generation"): "SETTLEMENT ORDER",
    ("_ProcessOutcome", "duration_seconds"): "VALIDITY WINDOW",
    ("_RecordedCurationPatternObservedV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("_RunState", "wall_clock_microseconds"): "VALIDITY WINDOW",
    ("_VersionedRecord", "source_effective_time"): "VALIDITY WINDOW",
}

# Discovered by name, carrying no clock value: a supplier of instants, a boolean
# policy flag, and the kernel start tick that disambiguates a recycled pid.
NON_CLOCK_DECLARED_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("AuthoringIntentCoordinator", "clock"),
        ("QueryEvaluationPolicy", "requires_explicit_evaluation_time"),
        ("ProviderProcessLeaseV1", "process_start_time"),
        ("ProviderDescendantProcessV1", "process_start_time"),
    }
)


def declared_clock(class_name: str, field_name: str) -> ClockDomainV1 | None:
    """Return the declared domain, or None when the owner declared none."""

    return CLOCK_FIELD_DECLARATIONS.get((class_name, field_name))


def classify_clock_field(
    class_name: str,
    name: str,
    annotation: str,
) -> ClockDomainV1 | None:
    """Return the declared clock of one discovered field, else None."""

    if not is_time_bearing_field(name, annotation):
        return None
    return declared_clock(class_name, name)


def clock_description(domain: ClockDomainV1) -> str:
    """Return the canonical Pydantic Field-description sentence."""

    return f"Reads {domain}."


__all__ = [
    "CLOCK_DOMAINS",
    "CLOCK_FIELD_DECLARATIONS",
    "NON_CLOCK_DECLARED_FIELDS",
    "TIME_FIELD_SUFFIXES",
    "ClockDomainV1",
    "classify_clock_field",
    "clock_description",
    "declared_clock",
    "is_time_bearing_field",
]
