"""The B2 carrier binds one complete, result-free acquisition plan."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlanV2,
    ProcedureAdmissionMaterialManifestV1,
    ProcedureSourceCaptureAssociationV1,
    procedure_acquisition_plan_digest,
    procedure_admission_material_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
)
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV5,
    ProcedureRunAdmissionV3,
    ProcedureRunAdmissionV5,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.service.playbill_procedure_runs import service_prepare_playbill_line_admission
from tests.test_playbill.test_procedure_execution import (
    _digest,
    _fixture,
    _line_admission_v4,
    _prepare,
    _state_procedure,
    _StateReader,
)


def _plan_and_admission(
    tmp_path: Path,
) -> tuple[ProcedureAcquisitionPlanV2, ProcedureRunAdmissionV5]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    v4 = _line_admission_v4(accepted, fixture)
    binding = v4.resolved_provider_bindings[0]
    classification = binding.classification_plan
    local = VerifiedProviderBindingV1(
        provider_artifact_digest=binding.provider_artifact_digest,
        interface_artifact_digest=classification.interface_artifact_digest,
        interface_id="demo.interface",
        interface_digest=classification.interface_digest,
        implementation_digest=binding.implementation_digest,
        deployment_digest=_digest("local-deployment"),
        materialization_digest=_digest("materialization"),
        environment_manifest_digest=_digest("environment-manifest"),
        entrypoint="demo.runtime:Provider",
        declared_endpoints=("https://example.test",),
    )
    remaining = min(
        v4.budget.wall_clock.microseconds,
        v4.hard_caps.max_wall_clock.microseconds,
    )
    budget = ProviderBudgetTranslationV1(
        remaining_wall_clock_microseconds=remaining,
        procedure_wall_clock_microseconds=v4.budget.wall_clock.microseconds,
        hard_cap_wall_clock_microseconds=v4.hard_caps.max_wall_clock.microseconds,
        runtime_wall_clock_seconds=remaining // 1_000_000,
        procedure_output_bytes_cap=None,
        hard_output_bytes_cap=None,
        policy_output_bytes_cap=v4.provider_output_bytes_cap,
        runtime_output_bytes_cap=v4.provider_output_bytes_cap,
        max_provider_calls=min(v4.budget.max_provider_calls, v4.hard_caps.max_provider_calls),
        max_items=v4.budget.max_items,
        result_bytes_cap=1024,
    )
    occurrence = ProviderExternalOccurrencePlanV1(
        occurrence_path="provider",
        occurrence_kind="provider",
        node_id=binding.node_id,
        provider_artifact_digest=binding.provider_artifact_digest,
        interface_artifact_digest=classification.interface_artifact_digest,
        interface_id="demo.interface",
        interface_digest=classification.interface_digest,
        vocabulary_digest=classification.vocabulary_digest,
        classifier_digest=classification.classifier_digest,
        accepted_bucket_selectors=classification.accepted_bucket_selectors,
        implementation_digest=binding.implementation_digest,
        effect_class=binding.effect_class,
        contract_input_digest=_digest("contract-in"),
        contract_output_digest=_digest("contract-out"),
        local_execution=local,
        secret_plan=ProviderSecretResolutionPlanV1(),
        budget_translation=budget,
    )
    plan = ProcedureAcquisitionPlanV2(
        accepted_coordinate=v4.accepted_coordinate,
        line_identity=v4.line_identity,
        line_spec_digest=v4.line_spec_digest or "",
        occurrence_id=v4.occurrence_id or "",
        occurrence_evaluation_time=v4.occurrence_evaluation_time,
        acquisition_policy_format="playbill-source-acquisition-policy-v1",
        acquisition_policy_digest=v4.acquisition_policy_digest or "",
        selection_receipt_digest=v4.selection_receipt_digest,
        selection_decision=v4.selection_decision,
        selection_decision_digest=v4.selection_decision_digest,
        external_occurrences=(occurrence,),
    )
    fields = {
        name: getattr(v4, name) for name in ProcedureRunAdmissionV3.model_fields if name != "tag"
    }
    fields.update(
        {
            "run_id": "RUN-" + "0" * 64,
            "resolved_provider_bindings": v4.resolved_provider_bindings,
            "acquisition_plan_digest": procedure_acquisition_plan_digest(plan),
            "exhaust_access_binding_digest": None,
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
        }
    )
    provisional = ProcedureRunAdmissionV5.model_construct(**fields)
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    admission_digest = procedure_admission_digest(provisional)
    run_id = procedure_line_run_id(
        occurrence_id=provisional.occurrence_id or "",
        attempt=provisional.attempt,
        admission_binding_digest=admission_digest,
        occurrence_evaluation_time=provisional.occurrence_evaluation_time,
    )
    admission = ProcedureRunAdmissionV5.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "run_id": run_id,
            "admission_binding_digest": admission_digest,
        }
    )
    return plan, admission


def test_v5_semantic_key_commits_plan_digest_but_not_attempt(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    baseline = admission.semantic_replay_key_digest
    changed_plan = admission.model_copy(update={"acquisition_plan_digest": _digest("changed-plan")})
    later_attempt = admission.model_copy(update={"attempt": admission.attempt + 1})

    assert procedure_semantic_replay_key_digest(changed_plan) != baseline
    assert procedure_semantic_replay_key_digest(later_attempt) == baseline


def test_exhaust_carrier_presence_law_fails_closed(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    provisional = admission.model_copy(
        update={
            "exhaust_access_binding_digest": _digest("unexpected-exhaust"),
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
            "run_id": "RUN-" + "0" * 64,
        }
    )
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    admission_digest = procedure_admission_digest(provisional)
    run_id = procedure_line_run_id(
        occurrence_id=provisional.occurrence_id or "",
        attempt=provisional.attempt,
        admission_binding_digest=admission_digest,
        occurrence_evaluation_time=provisional.occurrence_evaluation_time,
    )
    with pytest.raises(ValidationError, match="exhaust_binding_carrier_required"):
        ProcedureRunAdmissionV5.model_validate(
            {
                **provisional.model_dump(mode="python"),
                "admission_binding_digest": admission_digest,
                "run_id": run_id,
            }
        )


def test_exhaust_carrier_presence_law_has_a_typed_admission_emitter(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    invalid = ProcedureRunAdmissionV5.model_construct(
        **{
            **admission.model_dump(mode="python"),
            "exhaust_access_binding_digest": _digest("unexpected-exhaust"),
        }
    )
    refusal = service_prepare_playbill_line_admission(
        object(),  # type: ignore[arg-type]
        admission=invalid,
        accepted_line=object(),  # type: ignore[arg-type]
    )
    assert refusal.code == "exhaust_binding_carrier_required"  # type: ignore[union-attr]


def test_prepared_v5_requires_exact_plan_and_complete_provider_coverage(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    plan, admission = _plan_and_admission(tmp_path / "other")
    manifest = ProcedureAdmissionMaterialManifestV1(members=())

    prepared = PreparedProcedureRunV5(
        admission=admission,
        accepted_state_materials=direct.accepted_state_materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
    )
    assert prepared.acquisition_plan_digest == admission.acquisition_plan_digest

    empty_plan = plan.model_copy(update={"external_occurrences": ()})
    empty_plan_digest = procedure_acquisition_plan_digest(empty_plan)
    provisional = admission.model_copy(
        update={
            "acquisition_plan_digest": empty_plan_digest,
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
            "run_id": "RUN-" + "0" * 64,
        }
    )
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    admission_digest = procedure_admission_digest(provisional)
    empty_plan_admission = ProcedureRunAdmissionV5.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "admission_binding_digest": admission_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=provisional.occurrence_id or "",
                attempt=provisional.attempt,
                admission_binding_digest=admission_digest,
                occurrence_evaluation_time=provisional.occurrence_evaluation_time,
            ),
        }
    )
    with pytest.raises(ValidationError, match="does not cover admitted Provider bindings"):
        PreparedProcedureRunV5.model_validate(
            {
                **prepared.model_dump(mode="python"),
                "admission": empty_plan_admission,
                "acquisition_plan": empty_plan,
                "acquisition_plan_digest": empty_plan_digest,
            }
        )

    occurrence = plan.external_occurrences[0]
    duplicate_plan = plan.model_copy(
        update={
            "external_occurrences": (
                occurrence,
                occurrence.model_copy(update={"occurrence_path": "repeat/provider"}),
            )
        }
    )
    duplicate_plan_digest = procedure_acquisition_plan_digest(duplicate_plan)
    provisional = admission.model_copy(
        update={
            "acquisition_plan_digest": duplicate_plan_digest,
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
            "run_id": "RUN-" + "0" * 64,
        }
    )
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    admission_digest = procedure_admission_digest(provisional)
    duplicate_admission = ProcedureRunAdmissionV5.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "admission_binding_digest": admission_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=provisional.occurrence_id or "",
                attempt=provisional.attempt,
                admission_binding_digest=admission_digest,
                occurrence_evaluation_time=provisional.occurrence_evaluation_time,
            ),
        }
    )
    with pytest.raises(ValidationError, match="does not cover admitted Provider bindings"):
        PreparedProcedureRunV5.model_validate(
            {
                **prepared.model_dump(mode="python"),
                "admission": duplicate_admission,
                "acquisition_plan": duplicate_plan,
                "acquisition_plan_digest": duplicate_plan_digest,
            }
        )


def test_retired_runtime_reservation_v2_is_not_a_live_sidecar_format(tmp_path: Path) -> None:
    from cruxible_core.playbill.material_reservations import (
        ProcedureMaterialRecoveryRequired,
        ProcedureMaterialReservationStore,
    )

    fixture = _fixture(tmp_path)
    store = ProcedureMaterialReservationStore(fixture.bodies.reservation_root)
    (store.root / ("0" * 64 + ".json")).write_text(
        '{"tag":"playbill-run-material-reservation-v2"}\n', encoding="utf-8"
    )
    with pytest.raises(ProcedureMaterialRecoveryRequired, match="sidecar is corrupt"):
        store.active()


@pytest.mark.parametrize("occurrence_path", ["", "/provider", "provider/", "a//b"])
def test_reserved_b4_source_capture_association_requires_canonical_occurrence_path(
    occurrence_path: str,
) -> None:
    with pytest.raises(ValidationError, match="occurrence path"):
        ProcedureSourceCaptureAssociationV1(
            occurrence_path=occurrence_path,
            invocation_receipt_digest=_digest("receipt"),
            capture_digest=_digest("capture"),
        )
