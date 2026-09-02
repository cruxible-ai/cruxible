from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.models import (
    GuardPredicateV1,
    PredicateOperandV1,
    ProviderNodeV4,
    RepeatBodyNodeV4,
    RepeatNodeV4,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlanV2,
    ProcedureAdmissionMaterialManifestV1,
    ProcedureInternalFailureCodeV1,
    ProcedureInternalFailureV1,
    ProcedureNodeRefusalV1,
    ProcedureProviderBindingV2,
    ProcedureRunReceiptV6,
    ProviderBucketClassificationPlanV1,
    procedure_acquisition_plan_digest,
    procedure_admission_material_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslationV1,
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderInvocationCompletedV1,
    ProviderInvocationReceiptV1,
    ProviderInvocationStartedV1,
    ProviderSecretBindingIdentityV1,
    ProviderSecretReceiptReferenceV1,
    ProviderSecretReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
    provider_invocation_receipt_digest,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
)
from cruxible_client.contracts.providers import AcceptedProviderV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV5,
    ProcedureExecutor,
    ProcedureRunAdmissionV4,
    ProcedureRunAdmissionV5,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.provider_local_runtime import (
    ProviderDriverOutcomeV1,
    ProviderLocalRuntimeRefused,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    accepted_provider,
    install_demo_classifier,
)
from tests.test_playbill.test_graph_v4_provider_closure import (
    _accepted_procedure as _provider_v4_procedure,
)
from tests.test_playbill.test_procedure_execution import (
    _Authority,
    _Contracts,
    _digest,
    _fixture,
    _line_admission,
    _prepare,
    _StateReader,
)


class _Invoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        from cruxible_core.playbill.provider_local_runtime import BoundLocalProviderV1

        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/test/provider-runtime"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ):
        self.calls.append(invocation_id)
        assert bound.binding == occurrence.local_execution
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="ok",
                output={"size": context.input["size"]},
            ),
            stderr="",
            duration_seconds=0.001234,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=occurrence.local_execution,
        )


class _CrashingInvoker:
    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        return _Invoker().bind_provider(occurrence=occurrence)

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ):
        raise RuntimeError("daemon lost the provider result")


class _ElapsedClock:
    def __init__(self) -> None:
        self.monotonic_calls = 0

    def now(self):  # type: ignore[no-untyped-def]
        return datetime(2026, 9, 1, tzinfo=timezone.utc)

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return 0 if self.monotonic_calls == 1 else 400_000_000


def _accepted_one_provider(
    *,
    mutation: bool = False,
    repeat: bool = False,
) -> AcceptedProcedureV1:
    accepted = _provider_v4_procedure()
    definition = accepted.procedure.definition
    node = definition.nodes[0]
    assert isinstance(node, ProviderNodeV4)
    effect_policy = (
        ArtifactPin(
            role="effect-policy",
            target=ArtifactIdentity(kind="EffectPolicy", name="network"),
            artifact_digest=_digest("effect-policy"),
        )
        if mutation
        else None
    )
    node = node.model_copy(update={"input": {"size": 3}, "effect_policy": effect_policy})
    graph_node: ProviderNodeV4 | RepeatNodeV4 = node
    returns = node.as_
    if repeat:
        graph_node = RepeatNodeV4(
            node_id="repeat",
            max_attempts=1,
            body=(
                RepeatBodyNodeV4(
                    node_id=node.node_id,
                    operation="provider",
                    provider=node.provider,
                    interface=node.interface,
                    interface_digest=node.interface_digest,
                    implementation_digest=node.implementation_digest,
                    contract_in=node.contract_in,
                    contract_out=node.contract_out,
                    effect_policy=effect_policy,
                    spec={"size": 3},
                    as_=node.as_,
                ),
            ),
            until=GuardPredicateV1(
                left=PredicateOperandV1(kind="exists", alias=node.as_),
                operator="eq",
                right=PredicateOperandV1(kind="literal", value=True),
            ),
            as_="repeat_result",
        )
        returns = graph_node.as_
    definition = definition.model_copy(
        update={"nodes": (graph_node,), "returns": returns, "pin_slots": ()}
    )
    pins = tuple(
        sorted(
            set((*accepted.procedure.pins, *((effect_policy,) if effect_policy else ()))),
            key=lambda item: (
                item.role.encode(),
                item.target.qualified.encode(),
                item.artifact_digest.encode(),
            ),
        )
    )
    procedure = accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
            "pins": pins,
        }
    )
    return AcceptedProcedureV1(
        path=accepted.path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _prepared_v5(
    accepted: AcceptedProcedureV1,
    tmp_path: Path,
    *,
    effect_class: str = "external_read",
    secret_plan: ProviderSecretResolutionPlanV1 | None = None,
    provider: AcceptedProviderV1 | None = None,
    interface: AcceptedProviderInterfaceRegistrationV1 | None = None,
    local_binding: VerifiedProviderBindingV1 | None = None,
) -> tuple[PreparedProcedureRunV5, object]:
    fixture = _fixture(tmp_path)
    v3 = _line_admission(accepted, fixture)
    provider = provider or accepted_provider()
    interface = interface or accepted_interface()
    graph_node = accepted.procedure.definition.nodes[0]
    repeat_node_id: str | None = None
    if isinstance(graph_node, RepeatNodeV4):
        repeat_node_id = graph_node.node_id
        node = graph_node.body[0]
        assert isinstance(node, RepeatBodyNodeV4)
    else:
        node = graph_node
        assert isinstance(node, ProviderNodeV4)
    registration = interface.registration
    implementation_digest = provider.provider.implementations[0].implementation_digest
    secret_plan = secret_plan or ProviderSecretResolutionPlanV1()
    selectors = tuple(item.selector for item in registration.conformance_proofs)
    classification = ProviderBucketClassificationPlanV1(
        node_id=node.node_id,
        interface_artifact_digest=interface.artifact_digest,
        interface_digest=registration.interface_digest,
        vocabulary_digest=registration.vocabulary_digest,
        classifier_digest=registration.classifier_digest,
        accepted_bucket_selectors=selectors,
    )
    binding = ProcedureProviderBindingV2(
        node_id=node.node_id,
        provider_artifact_digest=provider.artifact_digest,
        classification_plan=classification,
        implementation_digest=implementation_digest,
        effect_class=effect_class,  # type: ignore[arg-type]
        secret_binding_identity_digests=secret_plan.binding_identity_digests,
    )
    v4_fields = {name: getattr(v3, name) for name in type(v3).model_fields if name != "tag"}
    v4_fields.update(
        {
            "resolved_provider_bindings": (binding,),
            "semantic_replay_key_digest": _digest("placeholder-replay"),
            "admission_binding_digest": _digest("placeholder-admission"),
            "run_id": "RUN-" + "0" * 64,
        }
    )
    v4_provisional = ProcedureRunAdmissionV4.model_construct(**v4_fields)
    v4_provisional = v4_provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(v4_provisional)}
    )
    v4_digest = procedure_admission_digest(v4_provisional)
    v4 = ProcedureRunAdmissionV4.model_validate(
        {
            **v4_provisional.model_dump(mode="python"),
            "admission_binding_digest": v4_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=v4_provisional.occurrence_id or "",
                attempt=v4_provisional.attempt,
                admission_binding_digest=v4_digest,
                occurrence_evaluation_time=v4_provisional.occurrence_evaluation_time,
            ),
        }
    )
    local = local_binding or VerifiedProviderBindingV1(
        provider_artifact_digest=provider.artifact_digest,
        interface_artifact_digest=interface.artifact_digest,
        interface_id=registration.interface_id,
        interface_digest=registration.interface_digest,
        implementation_digest=implementation_digest,
        deployment_digest=_digest("deployment-local"),
        materialization_digest=(
            provider.provider.implementations[0]
            .materialization_references[0]
            .materialization_digest
        ),
        environment_manifest_digest=_digest("environment"),
        entrypoint=provider.provider.runtime_artifact.manifest.implementations[0].entrypoint,
    )
    budget = ProviderBudgetTranslationV1(
        remaining_wall_clock_microseconds=v4.budget.wall_clock.microseconds,
        procedure_wall_clock_microseconds=v4.budget.wall_clock.microseconds,
        hard_cap_wall_clock_microseconds=v4.hard_caps.max_wall_clock.microseconds,
        runtime_wall_clock_seconds=v4.budget.wall_clock.microseconds // 1_000_000,
        policy_output_bytes_cap=v4.provider_output_bytes_cap,
        runtime_output_bytes_cap=v4.provider_output_bytes_cap,
        max_provider_calls=v4.budget.max_provider_calls,
        max_items=v4.budget.max_items,
        result_bytes_cap=1024,
    )
    occurrence = ProviderExternalOccurrencePlanV1(
        occurrence_path=(
            f"repeat/{repeat_node_id}/{node.node_id}"
            if repeat_node_id is not None
            else "provider/direct"
        ),
        occurrence_kind="provider",
        node_id=node.node_id,
        repeat_node_id=repeat_node_id,
        provider_artifact_digest=provider.artifact_digest,
        interface_artifact_digest=interface.artifact_digest,
        interface_id=registration.interface_id,
        interface_digest=registration.interface_digest,
        vocabulary_digest=registration.vocabulary_digest,
        classifier_digest=registration.classifier_digest,
        accepted_bucket_selectors=selectors,
        implementation_digest=implementation_digest,
        effect_class=effect_class,  # type: ignore[arg-type]
        contract_input_digest=node.contract_in.artifact_digest,  # type: ignore[union-attr]
        contract_output_digest=node.contract_out.artifact_digest,  # type: ignore[union-attr]
        local_execution=local,
        secret_plan=secret_plan,
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
    v5_fields = {
        name: getattr(v4, name) for name in ProcedureRunAdmissionV4.model_fields if name != "tag"
    }
    v5_fields.update(
        {
            "acquisition_plan_digest": procedure_acquisition_plan_digest(plan),
            "exhaust_access_binding_digest": None,
            "semantic_replay_key_digest": _digest("placeholder-replay-v5"),
            "admission_binding_digest": _digest("placeholder-admission-v5"),
            "run_id": "RUN-" + "0" * 64,
        }
    )
    v5_provisional = ProcedureRunAdmissionV5.model_construct(**v5_fields)
    v5_provisional = v5_provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(v5_provisional)}
    )
    v5_digest = procedure_admission_digest(v5_provisional)
    v5 = ProcedureRunAdmissionV5.model_validate(
        {
            **v5_provisional.model_dump(mode="python"),
            "admission_binding_digest": v5_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=v5_provisional.occurrence_id or "",
                attempt=v5_provisional.attempt,
                admission_binding_digest=v5_digest,
                occurrence_evaluation_time=v5_provisional.occurrence_evaluation_time,
            ),
        }
    )
    direct = _prepare(accepted, fixture, _StateReader())
    manifest = ProcedureAdmissionMaterialManifestV1(members=())
    prepared = PreparedProcedureRunV5(
        admission=v5,
        accepted_state_materials=direct.accepted_state_materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
    )
    fixture.journal.activate_writer(
        v5.journal_stream,
        v5.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(v5.journal_stream, v5.journal_partition_id),
    )
    return prepared, fixture


@pytest.mark.parametrize("effect_class", ["none", "external_read"])
@pytest.mark.parametrize("repeat", [False, True])
def test_graph_v4_provider_journals_completed_receipt_before_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect_class: str,
    repeat: bool,
) -> None:
    accepted = _accepted_one_provider(repeat=repeat)
    prepared, fixture = _prepared_v5(accepted, tmp_path, effect_class=effect_class)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    invoker = _Invoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    kinds = [record.record.event_kind for record in records]
    assert (
        kinds.index("provider_invocation_started")
        < kinds.index("provider_invocation_completed")
        < kinds.index("node_fired")
    )
    completed = records[kinds.index("provider_invocation_completed")]
    payload = parse_journal_payload(
        fixture.bodies.read(
            completed.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert payload["receipt"]["duration_microseconds"] == 1234  # type: ignore[index]
    assert payload["receipt"]["fence_scope"] == "process_group+descendant_sweep"  # type: ignore[index]
    indexed = fixture.run_index.get(prepared.admission.run_id)
    assert indexed is not None
    assert indexed.provider_invocation_started_count == 1
    assert indexed.provider_invocation_completed_count == 1
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=prepared.admission.run_id
    )
    assert isinstance(state.receipt, ProcedureRunReceiptV6)
    assert state.receipt.invocation_receipt_digests == (
        payload["receipt_digest"],  # type: ignore[index]
    )

    mismatch_index = ProcedureRunIndex(tmp_path / "mismatched-invocations.sqlite")
    try:
        for stored in records:
            actual_payload = parse_journal_payload(
                fixture.bodies.read(
                    stored.record.payload_digest,
                    access=BodyAccessContext(principal_id="test", can_read_body=True),
                )
            )
            if stored.record.event_kind != "provider_invocation_completed":
                mismatch_index.apply_record(stored, payload=actual_payload)
                continue
            original = ProviderInvocationCompletedV1.model_validate(actual_payload)
            forged_receipt = ProviderInvocationReceiptV1.model_validate(
                {
                    **original.receipt.model_dump(mode="python"),
                    "invocation_id": _digest("another-invocation"),
                }
            )
            forged_completion = ProviderInvocationCompletedV1(
                invocation_id=forged_receipt.invocation_id,
                receipt=forged_receipt,
                receipt_digest=provider_invocation_receipt_digest(forged_receipt),
            )
            with pytest.raises(PlaybillExecutionError, match="exact unmatched durable start"):
                mismatch_index.apply_record(
                    stored,
                    payload=forged_completion.model_dump(mode="json"),
                )
            break
    finally:
        mismatch_index.close()


def test_classifier_failure_projects_as_a_typed_node_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=ProviderBucketClassifierRegistry(),
    ).execute(prepared, accepted)
    assert result.status == "refused"
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=prepared.admission.run_id
    )
    assert isinstance(state.terminal, ProcedureNodeRefusalV1)
    assert state.terminal.code == "classifier_not_installed"


_PROCESS_FENCE_CODES = (
    "provider_process_lease_invalid",
    "provider_process_lease_missing",
    "provider_process_lease_echo_failed",
    "provider_process_lease_echo_mismatch",
    "provider_process_group_survived_recovery",
)


def test_every_process_fence_code_is_in_the_internal_failure_vocabulary() -> None:
    assert set(_PROCESS_FENCE_CODES).issubset(set(get_args(ProcedureInternalFailureCodeV1)))


@pytest.mark.parametrize("code", _PROCESS_FENCE_CODES)
def test_process_fence_failures_project_their_exact_typed_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    class _FenceFailure:
        def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
            return _Invoker().bind_provider(occurrence=occurrence)

        def invoke_provider(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProviderLocalRuntimeRefused(code, "provider process fence failed")

    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_FenceFailure(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    assert result.status == "failed"
    records = tuple(
        fixture.journal.all_records(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert isinstance(state.terminal, ProcedureInternalFailureV1)
    assert state.terminal.code == code


@pytest.mark.parametrize("repeat", [False, True])
def test_line_external_mutation_prepares_intent_and_invokes_zero_times(
    tmp_path: Path,
    repeat: bool,
) -> None:
    accepted = _accepted_one_provider(mutation=True, repeat=repeat)
    prepared, fixture = _prepared_v5(accepted, tmp_path, effect_class="external_mutation")
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    invoker = _Invoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)

    assert result.status == "refused"
    assert invoker.calls == []
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    assert [item.record.event_kind for item in records].count("effect_intent") == 1
    assert all(item.record.event_kind != "provider_invocation_started" for item in records)


def test_started_without_completed_poison_is_never_auto_reissued(tmp_path: Path) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_CrashingInvoker(),
        provider_classifier_registry=registry,
    )

    with pytest.raises(PlaybillExecutionError, match="provider_completion_not_durable"):
        executor.execute(prepared, accepted)
    with pytest.raises(PlaybillExecutionError, match="incomplete Provider invocation"):
        executor.execute(prepared, accepted)


def test_startup_recovery_closes_the_exact_start_and_terminalizes_the_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_CrashingInvoker(),
        provider_classifier_registry=registry,
    )
    with pytest.raises(PlaybillExecutionError, match="provider_completion_not_durable"):
        executor.execute(prepared, accepted)
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    started_record = next(
        item for item in records if item.record.event_kind == "provider_invocation_started"
    )
    started = parse_journal_payload(
        fixture.bodies.read(
            started_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    invocation_id = started["invocation_id"]  # type: ignore[index]

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    monkeypatch.setattr(
        procedure_run_service,
        "_journal_for_write",
        lambda _instance: (fixture.journal, tmp_path),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_stream",
        lambda _instance: prepared.admission.journal_stream,
    )
    assert procedure_run_service.service_recover_provider_invocations(
        _Instance(),  # type: ignore[arg-type]
        invocation_ids=(invocation_id,),
        recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ) == (invocation_id,)
    kinds = tuple(
        item.record.event_kind
        for item in fixture.journal.all_records(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
    )
    assert kinds.count("provider_invocation_completed") == 1
    assert kinds[-1] == "attempt_finalized"
    writer_state = fixture.journal.writer_state(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    assert writer_state is not None
    assert not writer_state.active
    assert executor.execute(prepared, accepted).status == "failed"


def test_unclean_start_marks_recovery_required_without_completion_or_terminalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_CrashingInvoker(),
        provider_classifier_registry=registry,
    )
    with pytest.raises(PlaybillExecutionError, match="provider_completion_not_durable"):
        executor.execute(prepared, accepted)
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    started_record = next(
        item for item in records if item.record.event_kind == "provider_invocation_started"
    )
    started = parse_journal_payload(
        fixture.bodies.read(
            started_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    invocation_id = started["invocation_id"]  # type: ignore[index]
    before = tuple(item.record_digest for item in records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    monkeypatch.setattr(
        procedure_run_service,
        "_journal_for_write",
        lambda _instance: (fixture.journal, tmp_path),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_stream",
        lambda _instance: prepared.admission.journal_stream,
    )

    with pytest.raises(procedure_run_service.ProcedureRunRecoveryRequired, match=invocation_id):
        procedure_run_service.service_recover_provider_invocations(
            _Instance(),  # type: ignore[arg-type]
            invocation_ids=(),
            recovery_failure_codes={invocation_id: "provider_process_group_survived_recovery"},
            recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
    after = tuple(
        item.record_digest
        for item in fixture.journal.all_records(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
    )
    assert after == before


def test_recovery_aggregates_prior_provider_receipts_and_budget_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _accepted_one_provider()
    successful_root = tmp_path / "successful"
    successful_root.mkdir()
    successful, successful_fixture = _prepared_v5(accepted, successful_root)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=successful_fixture.journal,
        bodies=successful_fixture.bodies,
        run_index=successful_fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=registry,
    ).execute(successful, accepted)
    successful_completion_record = next(
        item
        for item in successful_fixture.journal.all_records(
            successful.admission.journal_stream,
            successful.admission.journal_partition_id,
        )
        if item.record.event_kind == "provider_invocation_completed"
    )
    successful_completion = ProviderInvocationCompletedV1.model_validate(
        parse_journal_payload(
            successful_fixture.bodies.read(
                successful_completion_record.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
    )

    orphan_root = tmp_path / "orphan"
    orphan_root.mkdir()
    prepared, fixture = _prepared_v5(accepted, orphan_root)
    crashing = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_CrashingInvoker(),
        provider_classifier_registry=registry,
    )
    with pytest.raises(PlaybillExecutionError, match="provider_completion_not_durable"):
        crashing.execute(prepared, accepted)
    orphan_records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    orphan_start = ProviderInvocationStartedV1.model_validate(
        parse_journal_payload(
            fixture.bodies.read(
                next(
                    item
                    for item in orphan_records
                    if item.record.event_kind == "provider_invocation_started"
                ).record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
    )
    prior_invocation_id = _digest("prior-completed-invocation")
    prior_start = orphan_start.model_copy(update={"invocation_id": prior_invocation_id})
    prior_receipt = successful_completion.receipt.model_copy(
        update={
            "invocation_id": prior_invocation_id,
            "run_id": prepared.admission.run_id,
            "admission_binding_digest": prepared.admission.admission_binding_digest,
            "occurrence_path": orphan_start.occurrence_path,
            "implementation_digest": orphan_start.implementation_digest,
            "materialization_digest": orphan_start.materialization_digest,
            "input_digest": orphan_start.input_digest,
            "input_bucket": orphan_start.input_bucket,
        }
    )
    prior_completion = ProviderInvocationCompletedV1(
        invocation_id=prior_invocation_id,
        receipt=prior_receipt,
        receipt_digest=provider_invocation_receipt_digest(prior_receipt),
    )
    writer = ProcedureExhaustWriter(
        journal=fixture.journal,
        bodies=fixture.bodies,
        fencing_token="writer",
    )
    for event_kind, payload in (
        ("provider_invocation_started", prior_start),
        ("provider_invocation_completed", prior_completion),
    ):
        writer.append(
            stream=prepared.admission.journal_stream,
            partition_id=prepared.admission.journal_partition_id,
            event_kind=event_kind,  # type: ignore[arg-type]
            accepted_coordinate=prepared.admission.accepted_coordinate,
            procedure_artifact_digest=prepared.admission.procedure_artifact_digest,
            definition_digest=prepared.admission.definition_digest,
            run_id=prepared.admission.run_id,
            line_spec_digest=prepared.admission.line_spec_digest,
            occurrence_id=prepared.admission.occurrence_id,
            attempt=prepared.admission.attempt,
            admission_binding_digest=prepared.admission.admission_binding_digest,
            actor_context=prepared.admission.actor_context,
            recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            payload=payload.model_dump(mode="json"),
        )

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    monkeypatch.setattr(
        procedure_run_service,
        "_journal_for_write",
        lambda _instance: (fixture.journal, tmp_path),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_stream",
        lambda _instance: prepared.admission.journal_stream,
    )
    assert procedure_run_service.service_recover_provider_invocations(
        _Instance(),  # type: ignore[arg-type]
        invocation_ids=(orphan_start.invocation_id,),
        recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ) == (orphan_start.invocation_id,)
    final_record = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )[-1]
    final = parse_journal_payload(
        fixture.bodies.read(
            final_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert final["provider_calls"] == 2  # type: ignore[index]
    assert len(final["invocation_receipt_digests"]) == 2  # type: ignore[index]
    assert final["budget"]["observed"]["provider_calls"] == 2  # type: ignore[index]
    assert final["budget"]["observed"]["wall_clock_microseconds"] == 1234  # type: ignore[index]


def test_recovery_does_not_touch_a_healthy_partition_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    before = fixture.journal.writer_state(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    monkeypatch.setattr(
        procedure_run_service,
        "_journal_for_write",
        lambda _instance: (fixture.journal, tmp_path),
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_stream",
        lambda _instance: prepared.admission.journal_stream,
    )
    assert (
        procedure_run_service.service_recover_provider_invocations(
            _Instance(),  # type: ignore[arg-type]
            invocation_ids=(_digest("unrelated"),),
            recorded_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        == ()
    )
    assert (
        fixture.journal.writer_state(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
        == before
    )


def test_typed_start_failure_journals_completion_and_is_replayable(tmp_path: Path) -> None:
    class _StartFailure:
        def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
            return _Invoker().bind_provider(occurrence=occurrence)

        def invoke_provider(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_missing", "child did not acquire its fence"
            )

    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_StartFailure(),
        provider_classifier_registry=registry,
    )

    first = executor.execute(prepared, accepted)
    assert first.status == "failed"
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    kinds = tuple(item.record.event_kind for item in records)
    assert kinds.count("provider_invocation_started") == 1
    assert kinds.count("provider_invocation_completed") == 1
    assert executor.execute(prepared, accepted).status == "failed"


def test_invocation_receipt_commits_only_secret_binding_digest_and_purpose(
    tmp_path: Path,
) -> None:
    reference = ProviderSecretReferenceV1(
        realm="private_realm",
        name="credential_name",
        epoch="secret_epoch",
        purpose="billing lookup",
        resolver_kind="environment",
    )
    identity_digest = provider_secret_binding_identity_digest(
        ProviderSecretBindingIdentityV1(realm=reference.realm, name=reference.name)
    )
    plan = ProviderSecretResolutionPlanV1(
        references=(reference,),
        binding_identity_digests=(identity_digest,),
    )
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path, secret_plan=plan)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    completed_record = next(
        item
        for item in fixture.journal.all_records(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
        if item.record.event_kind == "provider_invocation_completed"
    )
    completed = ProviderInvocationCompletedV1.model_validate(
        parse_journal_payload(
            fixture.bodies.read(
                completed_record.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
    )
    assert completed.receipt.secret_references == (
        ProviderSecretReceiptReferenceV1(
            binding_identity_digest=identity_digest,
            purpose=reference.purpose,
        ),
    )
    serialized = completed.model_dump_json()
    assert reference.realm not in serialized
    assert reference.name not in serialized
    assert reference.epoch not in serialized


@pytest.mark.parametrize("mutation", ["duplicate_completion", "orphan_completion"])
def test_authoritative_replay_matches_cache_for_provider_completion_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Invoker(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    records = list(
        fixture.journal.all_records(
            prepared.admission.journal_stream,
            prepared.admission.journal_partition_id,
        )
    )
    completed = next(
        item for item in records if item.record.event_kind == "provider_invocation_completed"
    )
    if mutation == "duplicate_completion":
        records.append(completed)
    else:
        records = [
            item for item in records if item.record.event_kind != "provider_invocation_started"
        ]
    monkeypatch.setattr(
        procedure_run_service,
        "_records_for_run",
        lambda *_args, **_kwargs: tuple(records),
    )

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    with pytest.raises(
        procedure_run_service.ProcedureRunRecoveryRequired,
        match="exact unmatched durable start",
    ):
        procedure_run_service._state_from_records(  # noqa: SLF001
            _Instance(), run_id=prepared.admission.run_id
        )


def test_provider_call_budget_subtracts_elapsed_run_time_at_each_spawn(tmp_path: Path) -> None:
    class _BudgetCapturingInvoker(_Invoker):
        def __init__(self) -> None:
            super().__init__()
            self.wall_windows: list[float] = []

        def invoke_provider(self, **kwargs):  # type: ignore[no-untyped-def]
            self.wall_windows.append(kwargs["context"].budgets.wall_clock_seconds)
            return super().invoke_provider(**kwargs)

    accepted = _accepted_one_provider()
    prepared, fixture = _prepared_v5(accepted, tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    invoker = _BudgetCapturingInvoker()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
        clock=_ElapsedClock(),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    admitted_window = prepared.acquisition_plan.external_occurrences[
        0
    ].budget_translation.runtime_wall_clock_seconds
    assert invoker.wall_windows == [pytest.approx(admitted_window - 0.4)]
