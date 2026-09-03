"""Closed clock taxonomy for Playbill contract and runtime fields.

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
    # Canonical candidate timestamps are the author's assertion of when the
    # candidate was made; nothing checks them against a daemon clock.
    ("AuthoringIntentV1", "canonical_timestamp"): "ASSERTION TIME",
    ("PreflightCertificateV1", "canonical_timestamp"): "ASSERTION TIME",
    ("SemanticCandidate", "timestamp"): "ASSERTION TIME",
    ("SemanticCandidateV2", "timestamp"): "ASSERTION TIME",
    ("_MemberContext", "timestamp"): "ASSERTION TIME",
    ("AcceptedAuthorityBasisV1", "valid_from"): "VALIDITY WINDOW",
    ("AcceptedAuthorityBasisV1", "valid_until"): "VALIDITY WINDOW",
    ("AuditClaimFactorsV1", "first_accepted_generation"): "SETTLEMENT ORDER",
    ("AuditClaimFactorsV1", "last_independent_verification_generation"): "SETTLEMENT ORDER",
    ("AuditCursorV1", "evaluation_time"): "EVALUATION INSTANT",
    ("AuditEvidenceRefV1", "generation"): "SETTLEMENT ORDER",
    ("AuditRunV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("AuditRunV1", "evaluation_time"): "EVALUATION INSTANT",
    ("AuthoringClaimStatementV1", "effective_from"): "VALIDITY WINDOW",
    ("AuthoringClaimStatementV1", "effective_until"): "VALIDITY WINDOW",
    ("AuthoringIntentEventV1", "sequence"): "SETTLEMENT ORDER",
    ("AuthoringIntentEventV2", "sequence"): "SETTLEMENT ORDER",
    ("AuthoringIntentEventV3", "sequence"): "SETTLEMENT ORDER",
    ("BlockObservationV1", "scan_generation"): "SETTLEMENT ORDER",
    ("BoundedWindowCoherenceV1", "max_cross_source_skew"): "VALIDITY WINDOW",
    ("CandidateStatusV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("CaptureAcquisitionReceiptV1", "observed_at"): "EVALUATION INSTANT",
    ("CaptureAcquisitionReceiptV1", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureCursorV1", "sequence"): "SETTLEMENT ORDER",
    ("CaptureEnvelopeV1", "observed_at"): "EVALUATION INSTANT",
    ("CaptureEnvelopeV1", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureEnvelopeV2", "observed_at"): "EVALUATION INSTANT",
    ("CaptureEnvelopeV2", "source_effective_time"): "VALIDITY WINDOW",
    ("CaptureLandingEventV1", "landed_at"): "EVALUATION INSTANT",
    ("CaptureLandingEventV1", "sequence"): "SETTLEMENT ORDER",
    ("CaptureLandingEventV2", "landed_at"): "EVALUATION INSTANT",
    ("CaptureLandingEventV2", "sequence"): "SETTLEMENT ORDER",
    ("CaptureRetentionErasurePolicyV1", "minimum_retention"): "VALIDITY WINDOW",
    ("CaptureRunCoordinateV1", "bound_generation"): "SETTLEMENT ORDER",
    ("CaptureRunCoordinateV2", "bound_generation"): "SETTLEMENT ORDER",
    ("CaptureVerdictEvidenceV1", "observed_at"): "ASSERTION TIME",
    ("CaptureVerdictEvidenceV1", "source_effective_until"): "VALIDITY WINDOW",
    ("ChangeSetRecord", "sequence"): "SETTLEMENT ORDER",
    ("ChangeSetRecordV2", "sequence"): "SETTLEMENT ORDER",
    ("ChangeSetRecordV3", "sequence"): "SETTLEMENT ORDER",
    ("CheckpointGeneration", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAdjudicationRuleV1", "max_evidence_age"): "VALIDITY WINDOW",
    ("ClaimAdmissionCandidateContextV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimAttestationAppendResultV1", "partition_sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationAppendResultV1", "recorded_at"): "ASSERTION TIME",
    ("ClaimAttestationEventPayloadV1", "recorded_at"): "ASSERTION TIME",
    ("ClaimAttestationEventV1", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPartitionGenesisV1", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPartitionHeadV1", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationPublishedRootV1", "sequence"): "SETTLEMENT ORDER",
    ("ClaimAttestationStatement", "observed_at"): "ASSERTION TIME",
    ("ClaimAttestationStatement", "valid_until"): "VALIDITY WINDOW",
    ("ClaimAttestationStatementV2", "attested_at"): "ASSERTION TIME",
    ("ClaimAttestationStatementV2", "valid_until"): "VALIDITY WINDOW",
    ("ClaimAttestationStoreManifestV1", "initialized_at"): "ASSERTION TIME",
    ("ClaimEvidenceFreshnessLineV1", "expires_at"): "VALIDITY WINDOW",
    ("ClaimEvidenceFreshnessLineV1", "observed_at"): "ASSERTION TIME",
    ("ClaimInput", "effective_from"): "VALIDITY WINDOW",
    ("ClaimInput", "effective_until"): "VALIDITY WINDOW",
    ("ClaimLawEvidenceV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimLawEvidenceV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimQueryResultV1", "evaluated_at"): "EVALUATION INSTANT",
    ("ClaimQueryResultV1", "expires_at"): "VALIDITY WINDOW",
    ("ClaimReferentContext", "observed_at"): "ASSERTION TIME",
    ("ClaimRetireDependentV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirePreflightV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetireRequestV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementInput", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementMemberV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimRetirementResultItemV1", "effective_until"): "VALIDITY WINDOW",
    ("ClaimStatement", "effective_from"): "VALIDITY WINDOW",
    ("ClaimStatement", "effective_until"): "VALIDITY WINDOW",
    ("ClaimTypeDependentDispositionV3", "claim_effective_until"): "VALIDITY WINDOW",
    ("ClaimTypeMigrationDispositionV3", "claim_effective_until"): "VALIDITY WINDOW",
    ("ClaimVerdictResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ClaimVerdictResultV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ConsumptionAggregateV1", "consumption_epoch_generation"): "SETTLEMENT ORDER",
    ("ConsumptionEpochV1", "consumption_epoch_generation"): "SETTLEMENT ORDER",
    ("ContextCapsuleV1", "evaluation_time"): "EVALUATION INSTANT",
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
    ("DiscoveryPageV1", "evaluation_time"): "EVALUATION INSTANT",
    ("DiscoveryRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("EvidenceFreshnessExpirationV1", "expires_at"): "VALIDITY WINDOW",
    ("EvidenceFreshnessExpirationV1", "observed_at"): "ASSERTION TIME",
    ("ExhaustPromotionV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ExhaustPromotionV1", "last_sequence"): "SETTLEMENT ORDER",
    ("ExhaustReceiptSetManifestV1", "first_sequence"): "SETTLEMENT ORDER",
    ("ExhaustReceiptSetManifestV1", "last_sequence"): "SETTLEMENT ORDER",
    ("ExpandRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ExternalSourceReadRequestV1", "observed_at"): "EVALUATION INSTANT",
    ("FloorGenerationPairV1", "current_generation"): "SETTLEMENT ORDER",
    ("FloorGenerationPairV1", "floor_generation"): "SETTLEMENT ORDER",
    ("GovernedActorContext", "timestamp"): "ASSERTION TIME",
    ("InputAcquisitionRuleV1", "max_age"): "VALIDITY WINDOW",
    ("InsertionExpectationV2", "expires_at"): "VALIDITY WINDOW",
    ("InsertionTerminalTombstoneV2", "finalized_at"): "ASSERTION TIME",
    ("InsertionTerminalTombstoneV2", "retain_until"): "VALIDITY WINDOW",
    ("JournalHeadStatementV1", "asserted_at"): "ASSERTION TIME",
    ("JournalPartitionHeadV1", "sequence"): "SETTLEMENT ORDER",
    ("JournalRangeV1", "first_sequence"): "SETTLEMENT ORDER",
    ("JournalRangeV1", "last_sequence"): "SETTLEMENT ORDER",
    ("JournalSegmentDescriptorV1", "first_sequence"): "SETTLEMENT ORDER",
    ("JournalSegmentDescriptorV1", "last_sequence"): "SETTLEMENT ORDER",
    ("JournalWriterStateV1", "generation"): "SETTLEMENT ORDER",
    ("LineEgressReadingV1", "sequence"): "SETTLEMENT ORDER",
    ("LineRunRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("CadenceTriggerPolicyV1", "interval_seconds"): "VALIDITY WINDOW",
    ("WindowCloseTriggerPolicyV1", "window_seconds"): "VALIDITY WINDOW",
    ("MandateInvocationV1", "evaluation_time"): "EVALUATION INSTANT",
    ("MandateRuntimeCapV1", "valid_until"): "VALIDITY WINDOW",
    ("MemberLawEvaluationV2", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditCursor", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditEvidenceRef", "generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditFactors", "first_accepted_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditFactors", "last_independent_verification_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditResult", "audited_through_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditResultV1", "audited_through_generation"): "SETTLEMENT ORDER",
    ("PlaybillAuditResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillAuditResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillBlockSyncReadResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillBlockSyncSuccessorCandidateV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCandidateStatus", "accepted_generation"): "SETTLEMENT ORDER",
    ("PlaybillClaimExplanation", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV2", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV2", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV3", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimExplanationV3", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("PlaybillClaimQueryResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimQueryResultV2", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimRetirePreflight", "effective_until"): "VALIDITY WINDOW",
    ("PlaybillClaimVerdictQueryV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimVerdictQueryV2", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillClaimViewV2", "admission_evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillContextCapsule", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillCurationActionResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationActionResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationListRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillCurationListResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillCurationListResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationListResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillCurationListResultV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillCurationSuppressRequestV1", "until_generation"): "SETTLEMENT ORDER",
    ("PlaybillDocumentHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("PlaybillNextRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillNextRequestV1", "expiring_within"): "VALIDITY WINDOW",
    ("PlaybillNextResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillNextResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillPredictRequestV1", "deadline"): "VALIDITY WINDOW",
    ("PlaybillPredictionDeclarationV1", "deadline"): "VALIDITY WINDOW",
    ("PlaybillPredictionDeclarationV1", "declared_at"): "EVALUATION INSTANT",
    ("PlaybillProcedureReadiness", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillProcedureRunState", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillProposalListEntry", "admitted_at"): "ASSERTION TIME",
    ("PlaybillProposalListEntryV1", "admitted_at"): "ASSERTION TIME",
    ("PlaybillReviewOperationalEventV1", "accepted_generation"): "SETTLEMENT ORDER",
    ("PlaybillReviewOperationalEventV1", "recorded_at"): "ASSERTION TIME",
    ("PlaybillReviewOperationalEventV1", "sequence"): "SETTLEMENT ORDER",
    ("PlaybillSearchOrientationV1", "generation"): "SETTLEMENT ORDER",
    ("PlaybillSearchRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillSearchResult", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillSearchResultV1", "evaluation_time"): "EVALUATION INSTANT",
    ("PlaybillSinceCursor", "last_generation"): "SETTLEMENT ORDER",
    ("PlaybillSinceCursor", "lower_generation"): "SETTLEMENT ORDER",
    ("PlaybillSinceRequest", "generation"): "SETTLEMENT ORDER",
    ("PlaybillSinceResult", "generation"): "SETTLEMENT ORDER",
    ("PlaybillSinceRow", "generation"): "SETTLEMENT ORDER",
    ("PlaybillSubjectHistoryEntry", "sequence"): "SETTLEMENT ORDER",
    ("PreparedClaimAttestationRequestV1", "attested_at"): "ASSERTION TIME",
    ("PreparedClaimAttestationRequestV1", "valid_until"): "VALIDITY WINDOW",
    ("ProcedureAcquisitionPlanV2", "occurrence_evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureAdmissionMaterialMemberV1", "retain_until"): "VALIDITY WINDOW",
    ("ProcedureBudgetV3", "wall_clock"): "VALIDITY WINDOW",
    ("ProcedureHardCapsV3", "max_wall_clock"): "VALIDITY WINDOW",
    ("ProcedureJournalCoordinateV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureJournalRecordDraftV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureJournalRecordV1", "recorded_at"): "ASSERTION TIME",
    ("ProcedureJournalRecordV1", "sequence"): "SETTLEMENT ORDER",
    ("ProcedureMandateAuthoringPayloadV1", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateAuthoringPayloadV1", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMandateInputV1", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateInputV1", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMandateInvocationV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureMandateV1", "expires_at"): "VALIDITY WINDOW",
    ("ProcedureMandateV1", "valid_from"): "VALIDITY WINDOW",
    ("ProcedureMeasurementDeclarationV1", "check_after"): "VALIDITY WINDOW",
    ("ProcedureMeasurementDeclarationV1", "expires_after"): "VALIDITY WINDOW",
    ("ProcedureMeasurementReviewTriggerV1", "window"): "VALIDITY WINDOW",
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
    ("ProcedureRunAttributionV1", "recorded_time"): "ASSERTION TIME",
    ("ProcedureRunBudgetObservedV1", "wall_clock_microseconds"): "VALIDITY WINDOW",
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
    ("ProcedureRunRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunRequestV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ProcedureRunStateV2", "evaluation_time"): "EVALUATION INSTANT",
    ("ProjectionBlockStampV1", "declared_generation"): "SETTLEMENT ORDER",
    ("ProjectionQueryBackingV1", "declared_evaluation_time"): "EVALUATION INSTANT",
    ("ProposalAdmissionRecord", "admitted_at"): "ASSERTION TIME",
    ("ProposalEvaluationRecord", "evaluated_at"): "EVALUATION INSTANT",
    ("ProviderBudgetTranslationV1", "hard_cap_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslationV1", "procedure_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslationV1", "remaining_wall_clock_microseconds"): "VALIDITY WINDOW",
    ("ProviderBudgetTranslationV1", "runtime_wall_clock_seconds"): "VALIDITY WINDOW",
    ("ProviderDriverOutcomeV1", "duration_seconds"): "VALIDITY WINDOW",
    ("ProviderInvocationReceiptV1", "duration_microseconds"): "VALIDITY WINDOW",
    ("ProviderResultToExternalCaptureV1", "observed_at"): "EVALUATION INSTANT",
    ("ProviderResultToExternalCaptureV1", "source_effective_time"): "VALIDITY WINDOW",
    ("ProviderRuntimeBudgetsV1", "wall_clock_seconds"): "VALIDITY WINDOW",
    ("ProviderSigningKeyV1", "valid_from"): "VALIDITY WINDOW",
    ("ProviderSigningKeyV1", "valid_until"): "VALIDITY WINDOW",
    ("PublicationPreparationV2", "accepted_generation"): "SETTLEMENT ORDER",
    ("PublicationPreparationV2", "expires_at"): "VALIDITY WINDOW",
    ("QueryEvaluationPolicyV1", "result_expiry"): "VALIDITY WINDOW",
    ("QueryExecutionReceiptV1", "evaluation_time"): "EVALUATION INSTANT",
    ("RecoveredGeneration", "sequence"): "SETTLEMENT ORDER",
    ("ReplayCheckpointBodyV2", "sequence"): "SETTLEMENT ORDER",
    ("ReplayCheckpointFileV2", "written_at"): "ASSERTION TIME",
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
    ("SettledOutcomesQueryRequestV1", "evaluation_time"): "EVALUATION INSTANT",
    ("SettledOutcomesQueryResultV1", "evaluation_time"): "EVALUATION INSTANT",
    # The instant the daemon actually read the workspace file it receipted.
    ("SourceReadReceiptV1", "read_at"): "EVALUATION INSTANT",
    ("SourceEffectiveTimeV1", "effective_from"): "VALIDITY WINDOW",
    ("SourceEffectiveTimeV1", "effective_until"): "VALIDITY WINDOW",
    ("SourceSelectionReceiptV1", "evaluation_time"): "EVALUATION INSTANT",
    ("StandingMandate", "valid_from"): "VALIDITY WINDOW",
    ("StandingMandate", "valid_until"): "VALIDITY WINDOW",
    ("SubjectProfileV1", "evaluation_time"): "EVALUATION INSTANT",
    ("TerminalChildReceiptV1", "sequence"): "SETTLEMENT ORDER",
    ("TerminalEgressRequestV1", "prepared_at"): "EVALUATION INSTANT",
    ("TerminalEgressRequestV2", "evaluation_time"): "EVALUATION INSTANT",
    ("VerifiedClaimAttestationV2", "recorded_at"): "ASSERTION TIME",
    ("VerifiedExhaustRecordV1", "sequence"): "SETTLEMENT ORDER",
    ("WitnessRecord", "sequence"): "SETTLEMENT ORDER",
    ("_AuditHistoryIndex", "attestation_first_generation"): "SETTLEMENT ORDER",
    ("_AuditHistoryIndex", "first_statement_generation"): "SETTLEMENT ORDER",
    ("_CaptureObservation", "generation"): "SETTLEMENT ORDER",
    ("_CaptureObservation", "observed_at"): "ASSERTION TIME",
    ("_ClaimNode", "generation"): "SETTLEMENT ORDER",
    ("_CurationHistoryIndex", "last_generation"): "SETTLEMENT ORDER",
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
        ("QueryEvaluationPolicyV1", "requires_explicit_evaluation_time"),
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
