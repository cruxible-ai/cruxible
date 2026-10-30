"""The B2 carrier binds one complete, result-free acquisition plan."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts import provider_operations
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlan,
    ProcedureAdmissionMaterialManifest,
    ProcedureRunReceipt,
    ProcedureSourceCaptureAssociation,
    procedure_acquisition_plan_digest,
    procedure_admission_material_digest,
    procedure_provider_bindings,
)
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslation,
    ProviderExternalOccurrencePlan,
    ProviderSecretResolutionPlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_core.procedures.execution import (
    PreparedProcedureRun,
    ProcedureRunAdmission,
    admission_fields,
    procedure_semantic_replay_key_digest,
    seal_procedure_run_admission,
)
from cruxible_core.service.procedures.procedure_runs import (
    _plan_selection_decision,
    service_prepare_playbill_line_admission,
)
from tests.test_procedures.test_procedure_execution import (
    _digest,
    _fixture,
    _line_admission,
    _prepare,
    _state_procedure,
    _StateReader,
)


def _demo_operation(effect: str) -> ProviderOperation:
    return ProviderOperation.model_validate(
        {
            "name": "demo.interface",
            "in": {"name": "demo.input", "version": 1, "digest": _digest("contract-in")},
            "out": {"name": "demo.output", "version": 1, "digest": _digest("contract-out")},
            "effect": effect,
            "entrypoint": "demo.runtime:Provider",
        }
    )


def _plan_and_admission(
    tmp_path: Path,
) -> tuple[ProcedureAcquisitionPlan, ProcedureRunAdmission]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    base = _line_admission(accepted, fixture)
    assert base.provider_output_bytes_cap is not None
    assert base.selection_decision is not None and base.selection_decision_digest is not None
    operation = _demo_operation("external_read")
    operation_signature_digest = provider_operations.operation_signature_digest(
        operation.signature()
    )
    local = VerifiedProviderBinding(
        provider_artifact_digest=_digest("provider"),
        operation=operation.name,
        operation_signature_digest=operation_signature_digest,
        implementation_digest=_digest("implementation"),
        deployment_digest=_digest("local-deployment"),
        materialization_digest=_digest("materialization"),
        environment_manifest_digest=_digest("environment-manifest"),
        entrypoint="demo.runtime:Provider",
        declared_endpoints=("https://example.test",),
    )
    remaining = min(
        base.budget.wall_clock.microseconds,
        base.hard_caps.max_wall_clock.microseconds,
    )
    budget = ProviderBudgetTranslation(
        remaining_wall_clock_microseconds=remaining,
        procedure_wall_clock_microseconds=base.budget.wall_clock.microseconds,
        hard_cap_wall_clock_microseconds=base.hard_caps.max_wall_clock.microseconds,
        runtime_wall_clock_seconds=remaining // 1_000_000,
        procedure_output_bytes_cap=None,
        hard_output_bytes_cap=None,
        policy_output_bytes_cap=base.provider_output_bytes_cap,
        runtime_output_bytes_cap=base.provider_output_bytes_cap,
        max_provider_calls=min(base.budget.max_provider_calls, base.hard_caps.max_provider_calls),
        max_items=base.budget.max_items,
        result_bytes_cap=1024,
    )
    occurrence = ProviderExternalOccurrencePlan(
        occurrence_path="provider",
        occurrence_kind="provider",
        node_id="provider",
        slot="fetch",
        provider_artifact_digest=_digest("provider"),
        operation=operation,
        operation_signature_digest=operation_signature_digest,
        implementation_digest=_digest("implementation"),
        effect_class="external_read",
        local_execution=local,
        secret_plan=ProviderSecretResolutionPlan(),
        budget_translation=budget,
    )
    plan = ProcedureAcquisitionPlan(
        accepted_coordinate=base.accepted_coordinate,
        line_identity=base.line_identity,
        line_spec_digest=base.line_spec_digest or "",
        occurrence_id=base.occurrence_id or "",
        occurrence_evaluation_time=base.occurrence_evaluation_time,
        acquisition_policy_format="playbill-source-acquisition-policy-v1",
        acquisition_policy_digest=base.acquisition_policy_digest or "",
        selection_receipt_digest=base.selection_receipt_digest,
        selection_decision=base.selection_decision,
        selection_decision_digest=base.selection_decision_digest,
        external_occurrences=(occurrence,),
    )
    admission = seal_procedure_run_admission(
        {
            **admission_fields(base),
            "resolved_provider_bindings": procedure_provider_bindings(plan.external_occurrences),
            "acquisition_plan_digest": procedure_acquisition_plan_digest(plan),
        }
    )
    return plan, admission


def _with_plan(
    admission: ProcedureRunAdmission, plan: ProcedureAcquisitionPlan
) -> ProcedureRunAdmission:
    """The admission resealed under another plan, its bindings unchanged."""

    return seal_procedure_run_admission(
        {
            **admission_fields(admission),
            "acquisition_plan_digest": procedure_acquisition_plan_digest(plan),
        }
    )


def test_acquisition_semantic_key_commits_plan_digest_but_not_attempt(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    baseline = admission.semantic_replay_key_digest
    changed_plan = admission.model_copy(update={"acquisition_plan_digest": _digest("changed-plan")})
    later_attempt = admission.model_copy(update={"attempt": admission.attempt + 1})

    assert procedure_semantic_replay_key_digest(changed_plan) != baseline
    assert procedure_semantic_replay_key_digest(later_attempt) == baseline


def test_exhaust_carrier_presence_law_fails_closed(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    with pytest.raises(ValidationError, match="exhaust_binding_carrier_required"):
        seal_procedure_run_admission(
            {
                **admission_fields(admission),
                "exhaust_access_binding_digest": _digest("unexpected-exhaust"),
            }
        )


def test_exhaust_carrier_presence_law_has_a_typed_admission_emitter(tmp_path: Path) -> None:
    _plan, admission = _plan_and_admission(tmp_path)
    invalid = ProcedureRunAdmission.model_construct(
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


def test_prepared_run_requires_exact_plan_and_complete_provider_coverage(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    plan, admission = _plan_and_admission(tmp_path / "other")
    manifest = ProcedureAdmissionMaterialManifest(members=())

    prepared = PreparedProcedureRun(
        admission=admission,
        accepted_state_materials=direct.accepted_state_materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
    )
    assert prepared.acquisition_plan_digest == admission.acquisition_plan_digest

    empty_plan = plan.model_copy(update={"external_occurrences": ()})
    with pytest.raises(ValidationError, match="not the acquisition plan's own"):
        PreparedProcedureRun.model_validate(
            {
                **prepared.model_dump(mode="python"),
                "admission": _with_plan(admission, empty_plan),
                "acquisition_plan": empty_plan,
                "acquisition_plan_digest": procedure_acquisition_plan_digest(empty_plan),
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
    with pytest.raises(ValidationError, match="not the acquisition plan's own"):
        PreparedProcedureRun.model_validate(
            {
                **prepared.model_dump(mode="python"),
                "admission": _with_plan(admission, duplicate_plan),
                "acquisition_plan": duplicate_plan,
                "acquisition_plan_digest": procedure_acquisition_plan_digest(duplicate_plan),
            }
        )


def test_prepared_run_bindings_are_exactly_the_plans_own(tmp_path: Path) -> None:
    """A binding names its slot, operation, signature, Provider and implementation,
    all read off its planned occurrence, and no classification at all."""

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    plan, admission = _plan_and_admission(tmp_path / "other")
    manifest = ProcedureAdmissionMaterialManifest(members=())
    (binding,) = admission.resolved_provider_bindings
    assert binding.model_dump(mode="json") == {
        "tag": "cruxible-procedure-provider-binding-v4",
        "node_id": binding.node_id,
        "slot": plan.external_occurrences[0].slot,
        "operation": plan.external_occurrences[0].operation.name,
        "operation_signature_digest": plan.external_occurrences[0].operation_signature_digest,
        "provider_artifact_digest": plan.external_occurrences[0].provider_artifact_digest,
        "implementation_digest": plan.external_occurrences[0].implementation_digest,
        "effect_class": plan.external_occurrences[0].effect_class,
        "secret_binding_identity_digests": [],
    }
    other = binding.model_copy(update={"effect_class": "external_mutation"})
    with pytest.raises(ValidationError, match="not the acquisition plan's own"):
        PreparedProcedureRun(
            admission=admission.model_copy(update={"resolved_provider_bindings": (other,)}),
            accepted_state_materials=direct.accepted_state_materials,
            admission_material_manifest=manifest,
            admission_material_manifest_digest=procedure_admission_material_digest(manifest),
            acquisition_plan=plan,
            acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
        )


def test_retired_runtime_reservation_v2_is_not_a_live_sidecar_format(tmp_path: Path) -> None:
    from cruxible_core.storage.material_reservations import (
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
        ProcedureSourceCaptureAssociation(
            occurrence_path=occurrence_path,
            invocation_receipt_digest=_digest("receipt"),
            capture_digest=_digest("capture"),
        )


def test_reserved_b4_source_capture_associations_are_path_sorted_and_unique() -> None:
    first = ProcedureSourceCaptureAssociation(
        occurrence_path="a/provider",
        invocation_receipt_digest=_digest("receipt-a"),
        capture_digest=_digest("capture-a"),
    )
    second = ProcedureSourceCaptureAssociation(
        occurrence_path="b/provider",
        invocation_receipt_digest=_digest("receipt-b"),
        capture_digest=_digest("capture-b"),
    )
    assert ProcedureRunReceipt._source_associations((first, second)) == (  # noqa: SLF001
        first,
        second,
    )
    with pytest.raises(ValueError, match="path-sorted and unique"):
        ProcedureRunReceipt._source_associations((second, first))  # noqa: SLF001
    with pytest.raises(ValueError, match="path-sorted and unique"):
        ProcedureRunReceipt._source_associations((first, first))  # noqa: SLF001


def test_a_declared_input_with_no_planned_occurrence_is_not_scored(tmp_path: Path) -> None:
    """The planner scores planned occurrences, never the absence of one.

    An exhaust tap is the sharpest instance: the acceptance law requires a Line
    over an exhaust-tapping Procedure to pin an acquisition policy, and a policy
    must declare at least one input, yet `_provider_nodes` never yields an
    exhaust tap -- so that declared input can never have a planned occurrence.
    Scoring the miss here would refuse every such Line forever under a code that
    says the policy denied a Source input. "It never arrived" is a fact only the
    read can report, and `apply_acquisition_result` reports it.
    """

    plan, _admission = _plan_and_admission(tmp_path)
    rules: dict[str, AcquisitionDefault] = {}
    assert all(item.occurrence_kind != "source" for item in plan.external_occurrences)

    selection = _plan_selection_decision(
        rules,
        policy_digest=_digest("empty-acquisition-rules"),
        occurrences=plan.external_occurrences,
        capture_contracts={},
    )

    assert selection.decisions == ()
    assert selection.verdict == "selected"
