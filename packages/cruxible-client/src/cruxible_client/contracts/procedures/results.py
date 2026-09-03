"""Shared typed failure contracts for served Procedure runs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.acquisition_policies import AcquisitionInputDecisionV1
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical, typed_digest
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_execution import (
    ProviderExternalOccurrencePlanV1,
)
from cruxible_client.contracts.repairs import ServedRepairV1, hand_edit_repair
from cruxible_client.contracts.temporal import ensure_utc

ProcedureAdmissionRefusalCodeV1: TypeAlias = Literal[
    "binding_required",
    "unsupported_node",
    "not_current",
    "artifact_binding_mismatch",
    "pin_binding_mismatch",
    "input_material_mismatch",
    "state_tap_refused",
    "replay_material_mismatch",
    "procedure_runtime_policy_absent",
    "provider_explicit_implementation_required",
    "provider_replay_receipt_required",
    "exhaust_binding_carrier_required",
    "line_identity_mismatch",
    "line_not_accepted",
    "line_closure_incomplete",
    "line_mandate_required",
    "occurrence_id_mismatch",
    "occurrence_not_due",
    "occurrence_already_admitted",
]
ProcedureNodeRefusalCodeV1: TypeAlias = Literal[
    "guard_refused",
    "repeat_exhausted",
    "budget_exhausted",
    "runtime_reference_unresolved",
    "contract_input_refused",
    "contract_output_refused",
    "adapter_value_invalid",
    "shape_items_input_invalid",
    "filter_items_input_invalid",
    "dedupe_items_input_invalid",
    "join_items_left_input_invalid",
    "join_items_right_input_invalid",
    "aggregate_items_input_invalid",
    "result_not_canonical",
    "line_binding_required",
    "source_acquisition_unavailable",
    "source_material_unavailable",
    "terminal_not_available",
    "terminal_egress_unverified",
    "provider_unavailable",
    "unclassified_input",
    "unclaimed_bucket",
    "playbill.acquisition.unavailable",
    "playbill.acquisition.stale",
    "playbill.acquisition.oversized",
    "playbill.acquisition.refused",
    "effect_grant_unrecognized",
    "effect_dispatch_requires_actor",
    "effect_dispatch_requires_authenticated_actor",
    "terminal_rung_capped_by_procedure_terminal_capability",
    "terminal_rung_capped_by_line_requested_rung",
    "terminal_rung_capped_by_propagated_sensitivity",
    "terminal_rung_capped_by_mandate_grant",
    "terminal_rung_capped_by_calibration",
    "provider_acquisition_plan_required",
    "provider_acquisition_plan_mismatch",
    "provider_effect_declaration_mismatch",
    "classifier_not_installed",
    "classifier_digest_mismatch",
    "unknown_extra",
    "budget_wall_clock",
    "budget_output_size",
    "budget_cost",
    "provider_declined",
    "secret_bundle_too_large",
    "insufficient_series_length",
    "non_finite_input",
    "degenerate_scale",
    "mismatched_lengths",
    "unknown_method",
    "unknown_test_name",
    "declared_family_mismatch",
    "unsupported_aggregation",
    "unknown_column",
    "malformed_model_ref",
    "undeclared_match_parameters",
    "invalid_parameter",
    "cross_origin_credentialed_redirect",
    "unsupported_redirect_scheme",
    "redirect_limit",
    "provider_protocol_violation",
]
ProcedureOperationalFailureCodeV1: TypeAlias = Literal[
    "wall_clock_exhausted",
    "cas_unavailable_at_replay",
    "replay_material_mismatch",
    "journal_append_failed",
    "journal_read_failed",
    "journal_conflict",
    "run_recovery_required",
    "admission_material_unavailable_by_policy",
    "replay_material_unavailable",
    "admission_material_corrupt",
    "unsupported_protocol",
    "unsupported_backend",
    "no_compatible_artifact",
    "unresolvable_source",
    "air_gapped_cache_miss",
    "network_disabled",
    "cache_permissions",
    "unresolved_secret_ref",
    "secret_epoch_unavailable",
    "secret_resolver_not_installed",
    "provider_execution_error",
    "provider_completion_not_durable",
]
ProcedureInternalFailureCodeV1: TypeAlias = Literal[
    "unexpected_exception",
    "journal_integrity_error",
    "run_record_invalid",
    "compiler_invariant_broken",
    "provider_refusal_taxonomy_unknown",
    "unknown_manifest_field",
    "manifest_divergence",
    "acceptance_divergence",
    "unaccepted_provider",
    "undeclared_interface",
    "ambiguous_implementation",
    "unknown_interface",
    "interface_digest_mismatch",
    "bucket_fixture_missing",
    "invalid_bucket_vocabulary",
    "unknown_run_context_field",
    "provider_protocol_violation",
    "lock_mismatch",
    "lock_bytes_mismatch",
    "lock_missing_hash",
    "lock_ambiguous_fork",
    "index_not_pinned",
    "index_redirect",
    "artifact_hash_mismatch",
    "cache_integrity",
    "environment_divergence",
    "undeclared_egress",
    "secret_leak",
    "non_finite_output",
    "non_finite_result",
    "image_provenance_mismatch",
    "provider_runtime_not_in_materialization",
    "provider_process_lease_invalid",
    "provider_process_lease_missing",
    "provider_process_lease_echo_failed",
    "provider_process_lease_echo_mismatch",
    "provider_process_group_survived_recovery",
]
ProcedureSettlementRefusalCodeV1: TypeAlias = Literal[
    "settlement_proposal_id_mismatch",
    "settlement_candidate_mismatch",
    "settlement_base_semantic_root_mismatch",
    "settlement_activation_coordinate_changed",
    "settlement_actor_principal_invalid",
    "settlement_candidate_scope_mismatch",
    "settlement_receipt_mismatch",
    "settlement_lost_cas",
]


class _StrictResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str | None) -> str | None:
    if value is None:
        return None
    Sha256Value.from_tagged(value)
    return value


def _with_default_repair(value: object) -> object:
    if not isinstance(value, dict) or "repair" in value:
        return value
    code = value.get("code")
    if not isinstance(code, str):
        return value
    return {**value, "repair": hand_edit_repair(code).model_dump(mode="python")}


class ProcedureJournalCoordinateV1(_StrictResultModel):
    tag: Literal["playbill-procedure-journal-coordinate-v1"] = (
        "playbill-procedure-journal-coordinate-v1"
    )
    stream_instance_id: str
    journal_family: str
    stream_id: str
    partition_id: str
    sequence: int = Field(ge=1)
    record_digest: str

    _record_digest = field_validator("record_digest")(_digest)


class ProcedureBudgetRefusalDetailV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-refusal-detail-v1"] = (
        "playbill-procedure-budget-refusal-detail-v1"
    )
    budget_kind: Literal[
        "max_items",
        "result_bytes",
        "wall_clock",
        "max_provider_calls",
        "max_capture_bytes",
    ]
    limit: int = Field(ge=0)
    observed: int = Field(ge=1)


class ProcedureAdmissionRefusalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-admission-refusal-v1"] = (
        "playbill-procedure-admission-refusal-v1"
    )
    classification: Literal["admission_refusal"] = "admission_refusal"
    code: ProcedureAdmissionRefusalCodeV1
    message: str
    details: object = Field(default_factory=dict)
    retryable: bool = False
    repair: ServedRepairV1

    _repair = model_validator(mode="before")(_with_default_repair)

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureNodeRefusalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-node-refusal-v1"] = "playbill-procedure-node-refusal-v1"
    classification: Literal["node_refusal"] = "node_refusal"
    code: ProcedureNodeRefusalCodeV1
    message: str
    node_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    detail_code: str | None = None
    details: object = Field(default_factory=dict)
    budget: ProcedureBudgetRefusalDetailV1 | None = None
    retryable: bool = False
    repair: ServedRepairV1

    _repair = model_validator(mode="before")(_with_default_repair)

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _typed_detail(self) -> "ProcedureNodeRefusalV1":
        if self.code == "guard_refused" and self.detail_code is None:
            raise ValueError("guard refusal requires the Procedure-authored detail code")
        if self.code == "budget_exhausted" and self.budget is None:
            raise ValueError("budget refusal requires typed budget detail")
        if self.code != "guard_refused" and self.detail_code is not None:
            raise ValueError("only a guard refusal carries a Procedure-authored detail code")
        if self.code != "budget_exhausted" and self.budget is not None:
            raise ValueError("only a budget refusal carries typed budget detail")
        return self


class ProcedureOperationalFailureV1(_StrictResultModel):
    tag: Literal["playbill-procedure-operational-failure-v1"] = (
        "playbill-procedure-operational-failure-v1"
    )
    classification: Literal["operational_failure"] = "operational_failure"
    code: ProcedureOperationalFailureCodeV1
    message: str
    last_node_id: str | None = None
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    details: object = Field(default_factory=dict)
    retryable: bool = True
    repair: ServedRepairV1

    _repair = model_validator(mode="before")(_with_default_repair)

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureInternalFailureV1(_StrictResultModel):
    tag: Literal["playbill-procedure-internal-failure-v1"] = (
        "playbill-procedure-internal-failure-v1"
    )
    classification: Literal["internal_failure"] = "internal_failure"
    code: ProcedureInternalFailureCodeV1
    message: str
    correlation_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    repair: ServedRepairV1

    _repair = model_validator(mode="before")(_with_default_repair)


class ProcedureSettlementRefusalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-settlement-refusal-v1"] = (
        "playbill-procedure-settlement-refusal-v1"
    )
    classification: Literal["settlement_refusal"] = "settlement_refusal"
    code: ProcedureSettlementRefusalCodeV1
    message: str
    node_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    details: object = Field(default_factory=dict)
    retryable: bool = False
    repair: ServedRepairV1

    _repair = model_validator(mode="before")(_with_default_repair)

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureRunAttributionV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-attribution-v1"] = "playbill-procedure-run-attribution-v1"
    actor_type: str
    actor_id: str
    org_id: str
    operation_id: str
    request_id: str | None = None
    recorded_time: datetime

    @field_validator("recorded_time")
    @classmethod
    def _recorded_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProcedurePendingSuccessorV1(_StrictResultModel):
    tag: Literal["playbill-procedure-pending-successor-v1"] = (
        "playbill-procedure-pending-successor-v1"
    )
    proposal_id: str
    pending_successor_digest: str


class ProcedureRunReceiptV2(_StrictResultModel):
    tag: Literal["playbill-procedure-run-receipt-v2"] = "playbill-procedure-run-receipt-v2"
    run_id: str
    admission_binding_digest: str
    semantic_replay_key_digest: str
    semantic_result_digest: str | None
    bound_coordinate: AcceptedCoordinate
    head_at_admission: AcceptedCoordinate
    lane: Literal["current", "replay"]
    evaluation_time: datetime
    validated_pins: tuple[ArtifactPin, ...]
    admitted_inputs: tuple[dict[str, object], ...]
    attribution: ProcedureRunAttributionV1
    stream_instance_id: str
    journal_family: str
    stream_id: str
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_digests: tuple[str, ...]
    chain_head_digest: str

    _digests = field_validator(
        "admission_binding_digest",
        "semantic_replay_key_digest",
        "semantic_result_digest",
        "chain_head_digest",
    )(_digest)

    @field_validator("record_digests")
    @classmethod
    def _record_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            _digest(digest)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProcedureBudgetExceededDetailV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-exceeded-detail-v1"] = (
        "playbill-procedure-budget-exceeded-detail-v1"
    )
    dimension: Literal["max_items"] = "max_items"
    limit: int = Field(ge=1)
    observed: int = Field(ge=1)
    boundary: str | None = None
    field_path: str | None = None


class ProcedureBudgetExhaustedV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-exhausted-v1"] = (
        "playbill-procedure-budget-exhausted-v1"
    )
    classification: Literal["budget_exhausted"] = "budget_exhausted"
    code: Literal["budget_max_items_exceeded"] = "budget_max_items_exceeded"
    message: Literal["A Procedure collection exceeded its declared item bound."] = (
        "A Procedure collection exceeded its declared item bound."
    )
    node_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    details: ProcedureBudgetExceededDetailV1
    retryable: Literal[False] = False


class ProcedureHaltTerminalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-halt-terminal-v1"] = "playbill-procedure-halt-terminal-v1"
    classification: Literal["halted"] = "halted"
    node_id: str
    reason: str | None = None
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None


ProcedureTerminalV1: TypeAlias = Annotated[
    ProcedureAdmissionRefusalV1
    | ProcedureNodeRefusalV1
    | ProcedureOperationalFailureV1
    | ProcedureInternalFailureV1
    | ProcedureSettlementRefusalV1
    | ProcedureBudgetExhaustedV1
    | ProcedureHaltTerminalV1,
    Field(discriminator="tag"),
]


class ProcedureBudgetBoundaryObservationV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-boundary-observation-v1"] = (
        "playbill-procedure-budget-boundary-observation-v1"
    )
    high_water: int = Field(ge=0)
    boundary: str | None = None
    field_path: str | None = None

    @model_validator(mode="after")
    def _zero_location(self) -> "ProcedureBudgetBoundaryObservationV1":
        if self.high_water == 0 and (self.boundary is not None or self.field_path is not None):
            raise ValueError("a zero boundary observation has no location")
        if self.high_water > 0 and (self.boundary is None or self.field_path is None):
            raise ValueError("a nonzero boundary observation requires its location")
        return self


class ProcedureRunBudgetDeclaredV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-declared-v1"] = (
        "playbill-procedure-run-budget-declared-v1"
    )
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    result_bytes_cap: Literal[1_048_576] = 1_048_576


class ProcedureRunBudgetObservedV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-observed-v1"] = (
        "playbill-procedure-run-budget-observed-v1"
    )
    max_items: ProcedureBudgetBoundaryObservationV1
    result_bytes: ProcedureBudgetBoundaryObservationV1
    provider_calls: int = Field(ge=0)
    capture_bytes: int = Field(ge=0)
    wall_clock_microseconds: int = Field(ge=0)


class ProcedureRunBudgetV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-v1"] = "playbill-procedure-run-budget-v1"
    declared: ProcedureRunBudgetDeclaredV1
    observed: ProcedureRunBudgetObservedV1


class ProcedureRunReceiptV3(ProcedureRunReceiptV2):
    tag: Literal["playbill-procedure-run-receipt-v3"] = "playbill-procedure-run-receipt-v3"  # type: ignore[assignment]
    status: Literal[
        "succeeded",
        "node_refused",
        "operational_failed",
        "internal_failed",
        "halted",
    ]
    terminal: ProcedureTerminalV1 | None
    budget: ProcedureRunBudgetV1


class ProcedureRunNodePinSetV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-node-pin-set-v1"] = (
        "playbill-procedure-run-node-pin-set-v1"
    )
    node_id: str
    pins: tuple[ArtifactPin, ...]

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        def pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
            return (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            )

        if value != tuple(sorted(value, key=pin_key)) or len(
            {(pin.role, pin.target.qualified) for pin in value}
        ) != len(value):
            raise ValueError("run node pins must be sorted and unique")
        return value


class ProcedureReplayInputProjectionV1(_StrictResultModel):
    tag: Literal["playbill-procedure-replay-input-projection-v1"] = (
        "playbill-procedure-replay-input-projection-v1"
    )
    input_name: str
    plane: Literal["accepted_state", "landed_capture", "exhaust"]
    kind: Literal["query_result", "capture", "reduced_exhaust"]
    value_or_body_digest: str
    provenance_digest: str

    _digests = field_validator("value_or_body_digest", "provenance_digest")(_digest)

    @model_validator(mode="after")
    def _plane_kind(self) -> "ProcedureReplayInputProjectionV1":
        expected = {
            "accepted_state": "query_result",
            "landed_capture": "capture",
            "exhaust": "reduced_exhaust",
        }
        if self.kind != expected[self.plane]:
            raise ValueError("replay input projection plane and kind disagree")
        return self


class ProcedureProviderBindingV1(_StrictResultModel):
    tag: Literal["playbill-procedure-provider-binding-v1"] = (
        "playbill-procedure-provider-binding-v1"
    )
    node_id: str
    provider_artifact_digest: str
    interface_artifact_digest: str
    interface_digest: str
    classifier_digest: str
    accepted_bucket_selectors: tuple[str, ...]
    implementation_digest: str
    secret_binding_identity_digests: tuple[str, ...]

    _binding_digests = field_validator(
        "provider_artifact_digest",
        "interface_artifact_digest",
        "interface_digest",
        "classifier_digest",
        "implementation_digest",
    )(_digest)

    @field_validator("accepted_bucket_selectors", "secret_binding_identity_digests")
    @classmethod
    def _sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Provider binding sets must be sorted and unique")
        return value

    @field_validator("secret_binding_identity_digests")
    @classmethod
    def _secret_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _digest(item)
        return value


class ProviderBucketClassificationPlanV1(_StrictResultModel):
    """Accepted, measured-bucket-free classifier plan for one occurrence."""

    tag: Literal["playbill-provider-bucket-classification-plan-v1"] = (
        "playbill-provider-bucket-classification-plan-v1"
    )
    node_id: str
    interface_artifact_digest: str
    interface_digest: str
    vocabulary_digest: str
    classifier_digest: str
    accepted_bucket_selectors: tuple[str, ...]

    _plan_digests = field_validator(
        "interface_artifact_digest",
        "interface_digest",
        "vocabulary_digest",
        "classifier_digest",
    )(_digest)

    @field_validator("accepted_bucket_selectors")
    @classmethod
    def _selectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("classification selectors must be nonempty, sorted, and unique")
        return value


class ProcedureProviderBindingV2(_StrictResultModel):
    """Successor binding carrying the full classification and RAT-9 authority."""

    tag: Literal["playbill-procedure-provider-binding-v2"] = (
        "playbill-procedure-provider-binding-v2"
    )
    node_id: str
    provider_artifact_digest: str
    classification_plan: ProviderBucketClassificationPlanV1
    implementation_digest: str
    effect_class: Literal["none", "external_read", "external_mutation"]
    secret_binding_identity_digests: tuple[str, ...]

    _binding_digests = field_validator(
        "provider_artifact_digest",
        "implementation_digest",
    )(_digest)

    @field_validator("secret_binding_identity_digests")
    @classmethod
    def _secret_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("Provider secret binding identities must be sorted and unique")
        for item in value:
            _digest(item)
        return value

    @model_validator(mode="after")
    def _node_correspondence(self) -> "ProcedureProviderBindingV2":
        if self.classification_plan.node_id != self.node_id:
            raise ValueError("Provider binding and classification plan node ids disagree")
        return self


class ProcedureSelectionDecisionV1(_StrictResultModel):
    tag: Literal["playbill-procedure-selection-decision-v1"] = (
        "playbill-procedure-selection-decision-v1"
    )
    policy_digest: str
    verdict: Literal["selected", "refused"]
    decisions: tuple[AcquisitionInputDecisionV1, ...]
    coherence_proof_digest: str | None = None

    _decision_digests = field_validator("policy_digest", "coherence_proof_digest")(_digest)

    @field_validator("decisions")
    @classmethod
    def _decisions(
        cls,
        value: tuple[AcquisitionInputDecisionV1, ...],
    ) -> tuple[AcquisitionInputDecisionV1, ...]:
        names = tuple(item.input_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("selection decisions must be sorted and input-name unique")
        return value

    @model_validator(mode="after")
    def _verdict(self) -> "ProcedureSelectionDecisionV1":
        expected = (
            "refused"
            if any(item.disposition == "refused" for item in self.decisions)
            else "selected"
        )
        if self.verdict != expected:
            raise ValueError("selection decision verdict disagrees with its input decisions")
        return self


class ProcedureAdmissionMaterialMemberV1(_StrictResultModel):
    """One admission-material retention decision.

    ``retain_until`` reads VALIDITY WINDOW.
    """

    tag: Literal["playbill-procedure-admission-material-member-v1"] = (
        "playbill-procedure-admission-material-member-v1"
    )
    input_name: str
    plane: Literal["landed_capture", "exhaust"]
    semantic_digest: str
    body_digest: str | None
    retention_authority_digest: str
    body_retention: Literal["never_materialize", "optional", "required_for_duration"]
    retain_until: datetime | None = None
    erasure_rule_digest: str | None = None

    _material_digests = field_validator(
        "semantic_digest",
        "body_digest",
        "retention_authority_digest",
        "erasure_rule_digest",
    )(_digest)

    @field_validator("retain_until")
    @classmethod
    def _retain_until(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _retention_shape(self) -> "ProcedureAdmissionMaterialMemberV1":
        if (self.retain_until is not None) != (self.body_retention == "required_for_duration"):
            raise ValueError("retain_until is present exactly for required_for_duration material")
        if self.body_retention == "never_materialize" and self.body_digest is not None:
            raise ValueError("never_materialize admission cannot name a body digest")
        return self


class ProcedureAdmissionMaterialManifestV1(_StrictResultModel):
    tag: Literal["playbill-procedure-admission-material-v1"] = (
        "playbill-procedure-admission-material-v1"
    )
    members: tuple[ProcedureAdmissionMaterialMemberV1, ...]

    @field_validator("members")
    @classmethod
    def _members(
        cls,
        value: tuple[ProcedureAdmissionMaterialMemberV1, ...],
    ) -> tuple[ProcedureAdmissionMaterialMemberV1, ...]:
        names = tuple(member.input_name for member in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("admission material members must be sorted and input-name unique")
        return value


PROCEDURE_ADMISSION_MATERIAL_DOMAIN = "playbill-procedure-admission-material-v1"
PROCEDURE_SELECTION_DECISION_DOMAIN = "playbill-procedure-selection-decision-v1"


def procedure_admission_material_digest(
    manifest: ProcedureAdmissionMaterialManifestV1,
) -> str:
    payload = manifest.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        PROCEDURE_ADMISSION_MATERIAL_DOMAIN,
        payload,
    ).tagged


def procedure_selection_decision_digest(decision: ProcedureSelectionDecisionV1) -> str:
    payload = decision.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        PROCEDURE_SELECTION_DECISION_DOMAIN,
        payload,
    ).tagged


class ProcedureAcquisitionPlanV2(_StrictResultModel):
    """Digest-composed, result-free external acquisition plan for one Line run.

    ``accepted_coordinate.generation`` reads SETTLEMENT ORDER.
    ``occurrence_evaluation_time`` reads EVALUATION INSTANT.
    """

    tag: Literal["playbill-procedure-acquisition-plan-v2"] = (
        "playbill-procedure-acquisition-plan-v2"
    )
    stage: Literal["complete"] = "complete"
    accepted_coordinate: AcceptedCoordinate
    line_identity: ArtifactIdentity
    line_spec_digest: str
    occurrence_id: str
    occurrence_evaluation_time: datetime
    acquisition_policy_format: str
    acquisition_policy_digest: str
    selection_receipt_digest: str | None = None
    selection_decision: ProcedureSelectionDecisionV1
    selection_decision_digest: str
    has_exhaust_occurrences: bool = False
    exhaust_access_binding_digest: str | None = None
    external_occurrences: tuple[ProviderExternalOccurrencePlanV1, ...] = ()

    _digests = field_validator(
        "line_spec_digest",
        "acquisition_policy_digest",
        "selection_receipt_digest",
        "selection_decision_digest",
        "exhaust_access_binding_digest",
    )(_digest)

    @field_validator("occurrence_evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("external_occurrences")
    @classmethod
    def _occurrences(
        cls,
        value: tuple[ProviderExternalOccurrencePlanV1, ...],
    ) -> tuple[ProviderExternalOccurrencePlanV1, ...]:
        paths = tuple(item.occurrence_path for item in value)
        if paths != tuple(sorted(set(paths), key=lambda item: item.encode("utf-8"))):
            raise ValueError("external occurrence plans must be path-sorted and unique")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> ProcedureAcquisitionPlanV2:
        if self.line_identity.kind != "Line":
            raise ValueError("acquisition plan requires a Line identity")
        if self.selection_decision.policy_digest != self.acquisition_policy_digest:
            raise ValueError("acquisition plan selection names another policy")
        if self.selection_decision_digest != procedure_selection_decision_digest(
            self.selection_decision
        ):
            raise ValueError("acquisition plan selection digest does not reproduce")
        if self.has_exhaust_occurrences != (self.exhaust_access_binding_digest is not None):
            raise ValueError(
                "exhaust_binding_carrier_required: Exhaust presence and binding digest disagree"
            )
        return self


PROCEDURE_ACQUISITION_PLAN_V2_DOMAIN = "playbill-procedure-acquisition-plan-v2"


def procedure_acquisition_plan_digest(plan: ProcedureAcquisitionPlanV2) -> str:
    payload = plan.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROCEDURE_ACQUISITION_PLAN_V2_DOMAIN, payload).tagged


class ProcedureSourceCaptureAssociationV1(_StrictResultModel):
    """Reserved B4 association between one Provider occurrence and one Capture."""

    tag: Literal["playbill-procedure-source-capture-association-v1"] = (
        "playbill-procedure-source-capture-association-v1"
    )
    occurrence_path: str
    invocation_receipt_digest: str
    capture_digest: str

    _digests = field_validator("invocation_receipt_digest", "capture_digest")(_digest)

    @field_validator("occurrence_path")
    @classmethod
    def _occurrence_path(cls, value: str) -> str:
        if not value or value.startswith("/") or value.endswith("/") or "//" in value:
            raise ValueError("occurrence path must be nonempty and slash-canonical")
        return value


class ProcedureRunBudgetDeclaredV2(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-declared-v2"] = (
        "playbill-procedure-run-budget-declared-v2"
    )
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    result_bytes_cap: int = Field(ge=1)
    provider_output_bytes_cap: int = Field(ge=1)


class ProcedureRunBudgetV2(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-v2"] = "playbill-procedure-run-budget-v2"
    declared: ProcedureRunBudgetDeclaredV2
    observed: ProcedureRunBudgetObservedV1


class ProcedureRunReceiptV4(ProcedureRunReceiptV3):
    tag: Literal["playbill-procedure-run-receipt-v4"] = "playbill-procedure-run-receipt-v4"  # type: ignore[assignment]
    invocation_origin: Literal["line"] = "line"
    line_identity: ArtifactIdentity
    line_spec_digest: str
    occurrence_id: str
    occurrence_evaluation_time: datetime
    node_pin_sets: tuple[ProcedureRunNodePinSetV1, ...]
    pin_set_digest: str
    replay_input_vector: tuple[ProcedureReplayInputProjectionV1, ...]
    deployment_snapshot_digest: str
    acquisition_policy_digest: str
    selection_receipt_digest: str | None
    selection_decision: ProcedureSelectionDecisionV1
    selection_decision_digest: str
    resolved_provider_bindings: tuple[ProcedureProviderBindingV1, ...]
    sensitivity_policy_digest: str
    mandate_coordinate_digest: str
    calibration_coordinate_digest: str
    taint_labels: tuple[str, ...]
    epsilon_member: bool
    admission_material_manifest: ProcedureAdmissionMaterialManifestV1
    admission_material_manifest_digest: str
    budget: ProcedureRunBudgetV2  # type: ignore[assignment]

    _line_digests = field_validator(
        "line_spec_digest",
        "pin_set_digest",
        "deployment_snapshot_digest",
        "acquisition_policy_digest",
        "selection_receipt_digest",
        "selection_decision_digest",
        "sensitivity_policy_digest",
        "mandate_coordinate_digest",
        "calibration_coordinate_digest",
        "admission_material_manifest_digest",
    )(_digest)

    @field_validator("occurrence_evaluation_time")
    @classmethod
    def _occurrence_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("node_pin_sets")
    @classmethod
    def _node_pin_sets(
        cls,
        value: tuple[ProcedureRunNodePinSetV1, ...],
    ) -> tuple[ProcedureRunNodePinSetV1, ...]:
        node_ids = tuple(item.node_id for item in value)
        if node_ids != tuple(sorted(set(node_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("receipt node pin sets must be sorted and unique")
        return value

    @field_validator("replay_input_vector")
    @classmethod
    def _replay_inputs(
        cls,
        value: tuple[ProcedureReplayInputProjectionV1, ...],
    ) -> tuple[ProcedureReplayInputProjectionV1, ...]:
        names = tuple(item.input_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("receipt replay inputs must be sorted and unique")
        return value

    @field_validator("resolved_provider_bindings")
    @classmethod
    def _bindings(
        cls,
        value: tuple[ProcedureProviderBindingV1, ...],
    ) -> tuple[ProcedureProviderBindingV1, ...]:
        node_ids = tuple(item.node_id for item in value)
        if node_ids != tuple(sorted(set(node_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("receipt Provider bindings must be sorted and unique")
        return value

    @field_validator("taint_labels")
    @classmethod
    def _taint_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("receipt taint labels must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _line_shape(self) -> "ProcedureRunReceiptV4":
        if self.line_identity.kind != "Line":
            raise ValueError("v4 receipt requires a Line identity")
        if self.selection_decision.policy_digest != self.acquisition_policy_digest:
            raise ValueError("v4 selection decision names another acquisition policy")
        if self.selection_decision_digest != procedure_selection_decision_digest(
            self.selection_decision
        ):
            raise ValueError("v4 selection decision digest does not reproduce")
        if self.admission_material_manifest_digest != procedure_admission_material_digest(
            self.admission_material_manifest
        ):
            raise ValueError("v4 admission material digest does not reproduce")
        admitted_names = tuple(
            item.get("input_name") if isinstance(item, dict) else None
            for item in self.admitted_inputs
        )
        replay_names = tuple(item.input_name for item in self.replay_input_vector)
        if admitted_names != replay_names:
            raise ValueError("v4 admitted inputs and replay projections disagree")
        material_names = tuple(item.input_name for item in self.admission_material_manifest.members)
        expected_material_names = tuple(
            name
            for item, name in zip(self.admitted_inputs, admitted_names, strict=True)
            if isinstance(item, dict)
            and item.get("tag")
            in {
                "playbill-landed-capture-run-input-v1",
                "playbill-exhaust-run-input-v1",
            }
        )
        if material_names != expected_material_names:
            raise ValueError("v4 material manifest does not cover Capture/exhaust inputs")
        return self


class ProcedureRunReceiptV5(ProcedureRunReceiptV4):
    """Receipt successor embedding Provider binding v2 without rewriting v4."""

    tag: Literal["playbill-procedure-run-receipt-v5"] = "playbill-procedure-run-receipt-v5"  # type: ignore[assignment]
    resolved_provider_bindings: tuple[ProcedureProviderBindingV2, ...]  # type: ignore[assignment]

    @field_validator("resolved_provider_bindings")
    @classmethod
    def _v2_bindings(
        cls,
        value: tuple[ProcedureProviderBindingV2, ...],
    ) -> tuple[ProcedureProviderBindingV2, ...]:
        node_ids = tuple(item.node_id for item in value)
        if node_ids != tuple(sorted(set(node_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("receipt Provider v2 bindings must be sorted and unique")
        return value


class ProcedureRunReceiptV6(ProcedureRunReceiptV5):
    """Receipt successor exposing the complete B2 plan and durable call evidence.

    The inherited ``bound_coordinate.generation`` and
    ``head_at_admission.generation`` fields and the inherited
    ``first_sequence``/``last_sequence`` journal range read SETTLEMENT ORDER.
    The inherited ``evaluation_time``, ``occurrence_evaluation_time``, and
    ``attribution.recorded_time`` fields read EVALUATION INSTANT.  Every nested
    ``admission_material_manifest.members[].retain_until`` and Provider budget
    or duration field reads VALIDITY WINDOW.
    """

    tag: Literal["playbill-procedure-run-receipt-v6"] = "playbill-procedure-run-receipt-v6"  # type: ignore[assignment]
    acquisition_plan_digest: str
    exhaust_access_binding_digest: str | None = None
    invocation_receipt_digests: tuple[str, ...] = ()
    source_capture_associations: tuple[ProcedureSourceCaptureAssociationV1, ...] = ()

    _v6_digests = field_validator("acquisition_plan_digest", "exhaust_access_binding_digest")(
        _digest
    )

    @field_validator("invocation_receipt_digests")
    @classmethod
    def _invocation_receipts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("invocation receipt digests must be unique in durable order")
        for item in value:
            _digest(item)
        return value

    @field_validator("source_capture_associations")
    @classmethod
    def _source_associations(
        cls,
        value: tuple[ProcedureSourceCaptureAssociationV1, ...],
    ) -> tuple[ProcedureSourceCaptureAssociationV1, ...]:
        paths = tuple(item.occurrence_path for item in value)
        if paths != tuple(sorted(set(paths), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Source Capture associations must be path-sorted and unique")
        return value


__all__ = [
    "PROCEDURE_ADMISSION_MATERIAL_DOMAIN",
    "PROCEDURE_ACQUISITION_PLAN_V2_DOMAIN",
    "PROCEDURE_SELECTION_DECISION_DOMAIN",
    "ProcedureAdmissionMaterialManifestV1",
    "ProcedureAdmissionMaterialMemberV1",
    "ProcedureAdmissionRefusalCodeV1",
    "ProcedureAdmissionRefusalV1",
    "ProcedureAcquisitionPlanV2",
    "ProcedureBudgetBoundaryObservationV1",
    "ProcedureBudgetExceededDetailV1",
    "ProcedureBudgetExhaustedV1",
    "ProcedureBudgetRefusalDetailV1",
    "ProcedureHaltTerminalV1",
    "ProcedureInternalFailureCodeV1",
    "ProcedureInternalFailureV1",
    "ProcedureSettlementRefusalCodeV1",
    "ProcedureSettlementRefusalV1",
    "ProcedureJournalCoordinateV1",
    "ProcedureNodeRefusalCodeV1",
    "ProcedureNodeRefusalV1",
    "ProcedureOperationalFailureCodeV1",
    "ProcedureOperationalFailureV1",
    "ProcedurePendingSuccessorV1",
    "ProviderBucketClassificationPlanV1",
    "ProcedureProviderBindingV1",
    "ProcedureProviderBindingV2",
    "ProcedureReplayInputProjectionV1",
    "ProcedureRunAttributionV1",
    "ProcedureRunBudgetDeclaredV1",
    "ProcedureRunBudgetDeclaredV2",
    "ProcedureRunBudgetObservedV1",
    "ProcedureRunBudgetV1",
    "ProcedureRunBudgetV2",
    "ProcedureRunNodePinSetV1",
    "ProcedureRunReceiptV2",
    "ProcedureRunReceiptV3",
    "ProcedureRunReceiptV4",
    "ProcedureRunReceiptV5",
    "ProcedureRunReceiptV6",
    "ProcedureSourceCaptureAssociationV1",
    "ProcedureSelectionDecisionV1",
    "ProcedureTerminalV1",
    "procedure_admission_material_digest",
    "procedure_acquisition_plan_digest",
    "procedure_selection_decision_digest",
]
