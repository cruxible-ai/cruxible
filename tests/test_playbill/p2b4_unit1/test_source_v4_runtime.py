"""Source-v4 convergence self-attacks on B2's landed execution carrier."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CanonicalDurationV1,
    CaptureEnvelopeV2,
    ProviderResultToExternalCaptureV1,
    capture_contract_digest,
    parse_capture_envelope,
)
from cruxible_client.contracts.errors import (
    PlaybillCasError,
    PlaybillExecutionError,
    PlaybillJournalError,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.models import (
    CaptureEgressNodeV3,
    ProviderNodeV4,
    SourceNodeV4,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlanV2,
    ProcedureRunReceiptV6,
    procedure_acquisition_plan_digest,
    procedure_selection_decision_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProcedureDerivedSourceRequestV1,
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderInvocationCompletedV1,
    ProviderInvocationStartedV1,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.material_reservations import ProcedureMaterialReservationStore
from cruxible_core.playbill.procedures.egress import (
    CaptureTerminalEgressSink,
    TerminalEgressReceiptV2,
    compute_effective_rung,
)
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV5,
    ProcedureExecutor,
    ProcedureRunAdmissionV5,
    _ReservedCaptureStore,
    procedure_admission_digest,
    procedure_line_run_id,
    procedure_node_pin_sets,
    procedure_pin_set_digest,
    procedure_semantic_replay_key_digest,
)
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeRefusalV1,
    ProviderRuntimeResultEnvelopeV1,
)
from tests.test_playbill._p2b1_support import install_demo_classifier
from tests.test_playbill._pc_c_support import capture_contract, digest
from tests.test_playbill.test_procedure_execution import _Authority, _Contracts
from tests.test_playbill.test_provider_invocation_journal import (
    _accepted_one_provider,
    _prepared_v5,
)


class _SourceInvoker:
    def __init__(self, *, observed_at, result_size: int = 3) -> None:  # type: ignore[no-untyped-def]
        self.observed_at = observed_at
        self.result_size = result_size
        self.bind_calls = 0
        self.spawn_calls = 0
        self.coordinates = None

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        self.bind_calls += 1
        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/portable/test-provider"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ):
        self.spawn_calls += 1
        self.coordinates = context.coordinates
        assert context.input == {"size": 3}
        body = canonical_bytes({"size": self.result_size})
        output = ProviderResultToExternalCaptureV1(
            source_identity="commerce.production.orders",
            coordinate_type="postgres-lsn-v1",
            coordinate={"lsn": "0/16B6C50"},
            selector_type="relation-primary-key-v1",
            selector={"id": self.result_size, "relation": "orders"},
            replayability="exact",
            content_base64=base64.b64encode(body).decode("ascii"),
            byte_length=len(body),
            bytes_digest="sha256:" + hashlib.sha256(body).hexdigest(),
            observed_at=self.observed_at,
        )
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="ok",
                output=output.model_dump(mode="json"),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def _source_fixture(
    tmp_path: Path,
    *,
    rule: InputAcquisitionRuleV1 | None = None,
    include_terminal: bool = False,
) -> tuple[
    AcceptedProcedureV1,
    PreparedProcedureRunV5,
    object,
    SourceAcquisitionPolicyV1,
    object,
]:
    provider_accepted = _accepted_one_provider()
    provider_prepared, fixture = _prepared_v5(provider_accepted, tmp_path)
    provider_node = provider_accepted.procedure.definition.nodes[0]
    assert isinstance(provider_node, ProviderNodeV4)
    contract = capture_contract()
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    source_node = SourceNodeV4(
        node_id=provider_node.node_id,
        capture_contract=contract_pin,
        provider=provider_node.provider,
        interface=provider_node.interface,
        interface_digest=provider_node.interface_digest,
        implementation_digest=provider_node.implementation_digest,
        request={"size": "$input.size"},
        as_="source_result",
        next="capture-output" if include_terminal else None,
    )
    nodes = (
        (
            source_node,
            CaptureEgressNodeV3(
                node_id="capture-output",
                capture_contract=contract_pin,
                input="$steps.source_result",
            ),
        )
        if include_terminal
        else (source_node,)
    )
    definition = provider_accepted.procedure.definition.model_copy(
        update={"nodes": nodes, "returns": source_node.as_, "pin_slots": ()}
    )
    pins = tuple(
        sorted(
            (*provider_accepted.procedure.pins, contract_pin),
            key=lambda pin: (
                pin.role.encode("utf-8"),
                pin.target.qualified.encode("utf-8"),
                pin.artifact_digest.encode("ascii"),
            ),
        )
    )
    procedure = provider_accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
            "pins": pins,
        }
    )
    accepted = AcceptedProcedureV1(
        path=provider_accepted.path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    policy = SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="runtime-source"),
        inputs=(
            rule
            or InputAcquisitionRuleV1(
                input_name=source_node.as_,
                requirement="required",
                permitted_replayability=("attested_only", "exact"),
                max_age=CanonicalDurationV1(microseconds=60_000_000),
                on_unavailable="refuse",
                on_stale="refuse",
                on_oversized="refuse",
                on_conflict="preserve",
            ),
        ),
        coherence=IndependentCoherenceV1(),
    )
    policy_digest = acquisition_policy_digest(policy).tagged
    old_plan = provider_prepared.acquisition_plan
    old_occurrence = old_plan.external_occurrences[0]
    occurrence = ProviderExternalOccurrencePlanV1.model_validate(
        {
            **old_occurrence.model_dump(mode="python"),
            "occurrence_path": "source/direct",
            "occurrence_kind": "source",
            "input_name": source_node.as_,
            "capture_contract_digest": contract_pin.artifact_digest,
            "contract_input_digest": None,
            "contract_output_digest": None,
            "source_runtime_plan_digest": digest("source-runtime-plan", "unit1"),
        }
    )
    decision = old_plan.selection_decision.model_copy(update={"policy_digest": policy_digest})
    plan = ProcedureAcquisitionPlanV2(
        **{
            **old_plan.model_dump(mode="python", exclude={"tag"}),
            "acquisition_policy_digest": policy_digest,
            "selection_decision": decision,
            "selection_decision_digest": procedure_selection_decision_digest(decision),
            "external_occurrences": (occurrence,),
        }
    )
    node_pin_sets = procedure_node_pin_sets(accepted)
    plan_digest = procedure_acquisition_plan_digest(plan)
    old_admission = provider_prepared.admission
    fields = {
        name: getattr(old_admission, name)
        for name in ProcedureRunAdmissionV5.model_fields
        if name != "tag"
    }
    fields.update(
        {
            "procedure_artifact_digest": accepted.artifact_digest,
            "definition_digest": accepted.procedure.definition_digest,
            "invocation_input": {"size": 3},
            "full_pins": accepted.procedure.pins,
            "node_pin_sets": node_pin_sets,
            "pin_set_digest": procedure_pin_set_digest(accepted.procedure.pins, node_pin_sets),
            "acquisition_policy_digest": policy_digest,
            "selection_decision": decision,
            "selection_decision_digest": plan.selection_decision_digest,
            "acquisition_plan_digest": plan_digest,
            "semantic_replay_key_digest": digest("placeholder", "semantic"),
            "admission_binding_digest": digest("placeholder", "admission"),
            "run_id": "RUN-" + "0" * 64,
        }
    )
    provisional = ProcedureRunAdmissionV5.model_construct(**fields)
    provisional = provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(provisional)}
    )
    admission_digest = procedure_admission_digest(provisional)
    admission = ProcedureRunAdmissionV5.model_validate(
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
    prepared = PreparedProcedureRunV5.model_validate(
        {
            **provider_prepared.model_dump(mode="python"),
            "admission": admission,
            "acquisition_plan": plan,
            "acquisition_plan_digest": plan_digest,
        }
    )
    return accepted, prepared, fixture, policy, contract


class _RefusingSourceInvoker(_SourceInvoker):
    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound
    ):
        self.spawn_calls += 1
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0",
                run_id=context.run_id,
                status="refused",
                refusal=ProviderRuntimeRefusalV1(
                    code="provider_declined",
                    message="the requested row is temporarily absent",
                ),
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservationV1(
                observer_backend="test-attribution",
                observer_grade="attribution",
            ),
            verified_binding=bound.binding,
        )


def _payloads(prepared, fixture):  # type: ignore[no-untyped-def]
    records = fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    access = BodyAccessContext(principal_id="unit-test", can_read_body=True)
    return records, [
        parse_journal_payload(fixture.bodies.read(item.record.payload_digest, access=access))
        for item in records
    ]


def _next_attempt(prepared: PreparedProcedureRunV5) -> PreparedProcedureRunV5:
    provisional = prepared.admission.model_copy(
        update={
            "attempt": prepared.admission.attempt + 1,
            "admission_binding_digest": digest("placeholder", "next-admission"),
            "run_id": "RUN-" + "0" * 64,
        }
    )
    admission_digest = procedure_admission_digest(provisional)
    admission = ProcedureRunAdmissionV5.model_validate(
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
    return PreparedProcedureRunV5.model_validate(
        {**prepared.model_dump(mode="python"), "admission": admission}
    )


def test_dynamic_source_request_is_a_pre_spawn_result_and_capture_is_post_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
    )
    original_append = executor._append_event  # noqa: SLF001
    reservation_observations: list[tuple[str, ...]] = []

    def append_with_reservation_probe(admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "produced_capture":
            active = executor.material_reservations.active()
            reservation_observations.append(tuple(item.body_digest for item in active))
            assert len(active) == 2
            assert all(fixture.bodies.verify(item.body_digest) for item in active)
        return original_append(admission, records, event_kind, payload)

    monkeypatch.setattr(executor, "_append_event", append_with_reservation_probe)

    result = executor.execute(prepared, accepted)

    assert result.status == "succeeded"
    assert result.output == {"size": 3}
    assert (invoker.bind_calls, invoker.spawn_calls) == (1, 1)
    assert invoker.coordinates == {
        "instance_id": prepared.admission.instance_id,
        "accepted_generation": prepared.admission.accepted_coordinate.git_oid,
        "accepted_generation_digest": prepared.admission.accepted_coordinate.generation_root,
        "procedure_artifact_digest": prepared.admission.procedure_artifact_digest,
        "line_spec_digest": prepared.admission.line_spec_digest,
        "occurrence_id": prepared.admission.occurrence_id,
        "admission_binding_digest": prepared.admission.admission_binding_digest,
    }
    records, payloads = _payloads(prepared, fixture)
    kinds = [item.record.event_kind for item in records]
    assert kinds.index("attempt_started") < kinds.index("admission_bound")
    assert (
        kinds.index("admission_bound")
        < kinds.index("source_request_derived")
        < kinds.index("provider_invocation_started")
        < kinds.index("provider_invocation_completed")
        < kinds.index("source_acquisition")
        < kinds.index("produced_capture")
        < kinds.index("node_fired")
        < kinds.index("attempt_finalized")
    )
    derived = ProcedureDerivedSourceRequestV1.model_validate(
        payloads[kinds.index("source_request_derived")]
    )
    assert derived.admission_binding_digest == prepared.admission.admission_binding_digest
    assert derived.request == {"size": 3}
    admission_wire = canonical_bytes(prepared.admission.model_dump(mode="json")).decode("utf-8")
    assert "source_result" not in admission_wire
    started = ProviderInvocationStartedV1.model_validate(
        payloads[kinds.index("provider_invocation_started")]
    )
    completed = ProviderInvocationCompletedV1.model_validate(
        payloads[kinds.index("provider_invocation_completed")]
    )
    assert started.invocation_id == completed.invocation_id
    produced = payloads[kinds.index("produced_capture")]
    assert isinstance(produced, dict)
    assert produced["invocation_receipt_digest"] == completed.receipt_digest
    capture = parse_capture_envelope(
        fixture.bodies.read(
            str(produced["capture_digest"]),
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(capture, CaptureEnvelopeV2)
    assert capture.producer_receipt_digest == completed.receipt_digest
    assert fixture.bodies.verify(capture.commitment.digest)
    assert reservation_observations
    assert not executor.material_reservations.active()

    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=prepared.admission.run_id, receipt=result.receipt
    )
    assert isinstance(state.receipt, ProcedureRunReceiptV6)
    assert state.receipt.invocation_receipt_digests == (completed.receipt_digest,)
    assert state.receipt.source_capture_associations[0].capture_digest == produced["capture_digest"]


def test_exact_run_id_replay_does_not_construct_an_invoker_or_spawn(
    tmp_path: Path,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    first_invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    first = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=first_invoker,
        provider_classifier_registry=registry,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
    ).execute(prepared, accepted)
    accepted_path = tmp_path / "accepted.json"
    prepared_path = tmp_path / "prepared.json"
    marker_path = tmp_path / "invoker-constructed"
    accepted_path.write_text(accepted.model_dump_json(), encoding="utf-8")
    prepared_path.write_text(prepared.model_dump_json(), encoding="utf-8")
    fixture.run_index.close()
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
        from cruxible_core.playbill.cas import ContentAddressedBodyStore
        from cruxible_core.playbill.exhaust import LocalJournalBackend
        from cruxible_core.playbill.procedures.execution import (
            PreparedProcedureRunV5,
            ProcedureExecutor,
        )
        from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
        from tests.test_playbill.test_procedure_execution import _Authority, _Contracts

        root, accepted_path, prepared_path, marker_path = map(Path, sys.argv[1:])
        accepted = AcceptedProcedureV1.model_validate_json(accepted_path.read_text())
        prepared = PreparedProcedureRunV5.model_validate_json(prepared_path.read_text())

        def construct_invoker():
            marker_path.write_text("constructed")
            raise AssertionError("exact replay constructed a Provider invoker")

        index = ProcedureRunIndex(root / "run-index.sqlite")
        try:
            replay = ProcedureExecutor(
                journal=LocalJournalBackend(root / "journal"),
                bodies=ContentAddressedBodyStore(root / "cas"),
                run_index=index,
                fencing_token="writer",
                activation_authority=_Authority(accepted.artifact_digest),
                contract_validator=_Contracts(),
                provider_runtime_invoker_factory=construct_invoker,
            ).execute(prepared, accepted)
            print(replay.model_dump_json())
        finally:
            index.close()
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(accepted_path),
            str(prepared_path),
            str(marker_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not marker_path.exists()
    assert f'"run_id":"{first.run_id}"' in completed.stdout
    assert '"status":"succeeded"' in completed.stdout


def test_capture_reservation_precedes_the_first_body_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accepted, prepared, fixture, _policy, _contract = _source_fixture(tmp_path)
    reserved_store = _ReservedCaptureStore(  # noqa: SLF001
        bodies=fixture.bodies,
        reservations=ProcedureMaterialReservationStore(fixture.bodies.reservation_root),
        admission=prepared.admission,
    )

    def fail_store(_content: bytes):  # type: ignore[no-untyped-def]
        raise PlaybillCasError("simulated body-store loss")

    monkeypatch.setattr(fixture.bodies, "store", fail_store)
    with pytest.raises(PlaybillCasError, match="simulated"):
        reserved_store.store(b'{"size":3}')

    active = ProcedureMaterialReservationStore(fixture.bodies.reservation_root).active()
    assert len(active) == 1
    assert active[0].body_digest == fixture.bodies.digest_bytes(b'{"size":3}').tagged


def test_crash_after_capture_cas_before_produced_event_retains_both_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(tmp_path)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_SourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        provider_classifier_registry=registry,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
    )
    original_append = executor._append_event  # noqa: SLF001

    def crash_before_produced_event(admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "produced_capture":
            raise PlaybillJournalError("simulated journal loss")
        return original_append(admission, records, event_kind, payload)

    monkeypatch.setattr(executor, "_append_event", crash_before_produced_event)

    result = executor.execute(prepared, accepted)

    assert result.status == "failed"
    active = executor.material_reservations.active()
    assert len(active) == 2
    assert all(fixture.bodies.verify(item.body_digest) for item in active)


def test_only_input_attributed_provider_refusal_uses_the_declared_source_default(
    tmp_path: Path,
) -> None:
    default_rule = InputAcquisitionRuleV1(
        input_name="source_result",
        requirement="conservative_default",
        permitted_replayability=("attested_only", "exact"),
        max_age=CanonicalDurationV1(microseconds=60_000_000),
        on_unavailable="declared_conservative_default",
        on_stale="refuse",
        on_oversized="refuse",
        on_conflict="preserve",
        conservative_default=False,
    )
    accepted, prepared, fixture, policy, contract = _source_fixture(
        tmp_path,
        rule=default_rule,
    )
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_RefusingSourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        provider_classifier_registry=registry,
        capture_contracts={capture_contract_digest(contract).tagged: contract},
        acquisition_policy=policy,
        default_authorizations=("source_result",),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    assert result.output is False
    records, payloads = _payloads(prepared, fixture)
    kinds = [item.record.event_kind for item in records]
    assert kinds.index("provider_invocation_completed") < kinds.index("source_acquisition")
    acquisition = payloads[kinds.index("source_acquisition")]
    assert isinstance(acquisition, dict)
    assert acquisition["decision"]["disposition"] == "defaulted"  # type: ignore[index]
    assert "produced_capture" not in kinds


def test_incomplete_source_closure_refuses_before_attempt_or_invoker_construction(
    tmp_path: Path,
) -> None:
    accepted, prepared, fixture, policy, _contract = _source_fixture(tmp_path)
    invoker = _SourceInvoker(observed_at=prepared.admission.occurrence_evaluation_time)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        acquisition_policy=policy,
        capture_contracts={},
    )

    with pytest.raises(PlaybillExecutionError, match="Source closure does not reproduce"):
        executor.execute(prepared, accepted)

    assert (invoker.bind_calls, invoker.spawn_calls) == (0, 0)
    assert not fixture.journal.all_records(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )


def test_two_run_ids_under_one_semantic_key_keep_independent_source_results(
    tmp_path: Path,
) -> None:
    accepted, first_prepared, fixture, policy, contract = _source_fixture(tmp_path)
    second_prepared = _next_attempt(first_prepared)
    assert first_prepared.admission.run_id != second_prepared.admission.run_id
    assert (
        first_prepared.admission.semantic_replay_key_digest
        == second_prepared.admission.semantic_replay_key_digest
    )
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)

    def execute(prepared: PreparedProcedureRunV5, result_size: int):  # type: ignore[no-untyped-def]
        return ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_runtime_invoker=_SourceInvoker(
                observed_at=prepared.admission.occurrence_evaluation_time,
                result_size=result_size,
            ),
            provider_classifier_registry=registry,
            capture_contracts={capture_contract_digest(contract).tagged: contract},
            acquisition_policy=policy,
        ).execute(prepared, accepted)

    first = execute(first_prepared, 3)
    second = execute(second_prepared, 4)

    assert first.output == {"size": 3}
    assert second.output == {"size": 4}
    first_replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(first_prepared, accepted)
    second_replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(second_prepared, accepted)
    assert first_replay.output == {"size": 3}
    assert second_replay.output == {"size": 4}


def test_live_v5_capture_terminal_uses_the_v2_topological_receipt_chain(
    tmp_path: Path,
) -> None:
    accepted, prepared, fixture, policy, contract = _source_fixture(
        tmp_path,
        include_terminal=True,
    )
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    contract_digest = capture_contract_digest(contract).tagged
    rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=0,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=prepared.admission.occurrence_evaluation_time,
        procedure_definition_digest=prepared.admission.definition_digest,
        line_spec_digest=prepared.admission.line_spec_digest or "",
        sensitivity_policy_digest=prepared.admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=prepared.admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=prepared.admission.calibration_coordinate_digest or "",
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_SourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        provider_classifier_registry=registry,
        capture_contracts={contract_digest: contract},
        acquisition_policy=policy,
        effective_rung=rung,
        egress_sink=CaptureTerminalEgressSink(
            store=fixture.bodies,
            contracts={contract_digest: contract},
            producer=accepted.procedure.identity,
            producer_binding_digest=accepted.artifact_digest,
        ),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    records, payloads = _payloads(prepared, fixture)
    kinds = [item.record.event_kind for item in records]
    terminal = payloads[kinds.index("terminal_egress")]
    assert isinstance(terminal, dict)
    receipt = TerminalEgressReceiptV2.model_validate(terminal["receipt"])
    terminal_capture = parse_capture_envelope(
        fixture.bodies.read(
            receipt.children[0].egress_digest,
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(terminal_capture, CaptureEnvelopeV2)
    assert terminal_capture.producer_receipt_digest == receipt.producer_receipt_digest
    assert result.receipt.chain_head_digest == records[-1].record_digest
