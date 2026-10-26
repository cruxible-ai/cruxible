"""Measurement credits the recorded build, including when fit is ambiguous."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.named_references import name_provider
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    NamedProcedureArtifact,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import (
    NamedLineSpec,
    line_spec_digest,
    render_line_spec,
)
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition
from cruxible_client.contracts.procedures.readings import ProcedureMeasureRequest
from cruxible_client.contracts.provider_execution import ProviderEgressObservation
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    provider_expected_implementation_records,
    provider_path,
    render_provider,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.exhaust.payloads import read_payload
from cruxible_core.governance.seed_artifacts.workspace_file import WORKSPACE_FILE_INTERFACE_ID
from cruxible_core.procedures.execution import accepted_procedure_pin_set_digest
from cruxible_core.procedures.readings import ProcedureReadingV1
from cruxible_core.procedures.reference_binding import bind_procedure_references
from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelopeV1
from cruxible_core.service.procedures.measurements import (
    ProcedureMeasurementRefused,
    _journal,
    _replay_measured_run,
    service_measure_playbill_procedure,
)
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureRunRequest,
    _DeterministicClock,
    load_playbill_procedure_run_grain,
    service_run_playbill_line,
    service_run_playbill_procedure,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._p2b1_support import provider_v2
from tests.support.provider_builds import as_build
from tests.test_procedures.test_procedure_measurement_readings import _actor, _world
from tests.test_procedures.test_procedure_source_runs import (
    RELATIVE_PATH,
    _Operator,
    _policy,
    _reader,
    _source_request,
    _WorkspaceInvoker,
    capture_contract,
)
from tests.test_procedures.test_slotted_procedures import slotted
from tests.test_providers.test_slotted_build_moves import _other

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class _Invoker(_WorkspaceInvoker):
    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):
        if occurrence.occurrence_kind == "source":
            return super().invoke_provider(
                occurrence=occurrence,
                context=context,
                invocation_id=invocation_id,
                bound=bound,
                deadline=deadline,
            )
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0", run_id=context.run_id, status="ok", output={"value": 42}
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(observer_backend="test", observer_grade="attribution"),
            verified_binding=bound.binding,
        )


def _slotted_world(tmp_path, *, landed=False, invoke_child=False):
    instance, _, measured = _world(tmp_path)
    _, _, first, interface = slotted()
    second = _other(interface.registration)
    declaration = next(m for m in measured.definition.measurements if m.name == "rows-present")
    # Hand-authored graph, rather than decompiling the binder's own output.
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.measured-slot",
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {"compute": "test.compute@1"},
            "steps": [
                {
                    "call": "result",
                    "interface": "test.compute@1",
                    "provider": "compute",
                    "input": {"value": "$input.value"},
                }
            ],
            "returns": "result",
            "measurements": [declaration.model_dump(mode="json")],
            "budget": {"provider_calls": 1},
        }
    )
    extra_members = {}
    source_bindings = {}
    if landed:
        from cruxible_client.contracts.procedures.windows import NamedCaptureEventSelector

        root = tmp_path / "workspace"
        target = root / RELATIVE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"value":1}')
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        path = provider_interface_path(WORKSPACE_FILE_INTERFACE_ID)
        source_interface = parse_provider_interface(tree[path], path=path)
        template = provider_v2(registration=source_interface)
        # Each wheel has its own build key, including across interface families.
        runtime = template.runtime_artifact.model_copy(
            update={
                "distribution": template.runtime_artifact.distribution.model_copy(
                    update={"sha256": "sha256:" + "e" * 64}
                ),
            }
        )
        source_provider = name_provider(
            as_build(
                template.model_copy(
                    update={
                        "runtime_artifact": runtime,
                        "implementations": provider_expected_implementation_records(runtime),
                    }
                )
            )
        )
        alternate_runtime = runtime.model_copy(
            update={
                "distribution": runtime.distribution.model_copy(
                    update={"sha256": "sha256:" + "d" * 64}
                ),
            }
        )
        alternate = name_provider(
            as_build(
                template.model_copy(
                    update={
                        "runtime_artifact": alternate_runtime,
                        "implementations": provider_expected_implementation_records(
                            alternate_runtime
                        ),
                    }
                )
            )
        )
        contract = capture_contract()
        source_bindings = {"fetch": ArtifactNameRef(target=source_provider.identity)}
        extra_members = {
            acquisition_policy_path("default"): render_acquisition_policy(_policy()),
            provider_path(source_provider.identity.name): render_provider(source_provider),
            provider_path(alternate.identity.name): render_provider(alternate),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
        }
        spec = ProcedureSpec.model_validate(
            {
                **spec.model_dump(mode="json"),
                "slots": {"compute": "test.compute@1", "fetch": "workspace.file"},
                "steps": [
                    {
                        "source": "captured",
                        "id": "read",
                        "contract": contract.identity.name,
                        "interface": "workspace.file",
                        "provider": "fetch",
                        "request": _source_request(instance, root).model_dump(mode="json"),
                    },
                    {
                        "call": "result",
                        "interface": "test.compute@1",
                        "provider": "compute",
                        "input": {"value": "$captured.content.json.value"},
                    },
                ],
                "budget": {"provider_calls": 2, "capture_bytes": 65536},
            }
        )
    if invoke_child:
        child_spec = ProcedureSpec.model_validate(
            {
                "procedure": "test.recovery-child",
                "input": {"value": "integer"},
                "output": {"value": "integer"},
                "steps": [
                    {
                        "call": "child_result",
                        "id": "child-call",
                        "interface": "test.compute@1",
                        "provider": first.provider.identity.name,
                        "input": {"value": "$input.value"},
                    }
                ],
                "returns": "child_result",
                "budget": {"provider_calls": 1},
            }
        )
        child_payload = lower_authoring_input(lower_procedure_spec(child_spec))
        child_definition = NamedProcedureDefinition.model_validate(child_payload.definition)
        child = NamedProcedureArtifact(
            identity=ArtifactIdentity(kind="Procedure", name=child_spec.procedure),
            definition=child_definition,
            definition_digest=compute_procedure_definition_digest(child_definition).tagged,
            owned_contracts=child_payload.owned_contracts,
            activation_policy="snapshot",
        )
        extra_members["procedures/" + child.identity.name + ".json"] = render_procedure(child)
        spec = ProcedureSpec.model_validate(
            {
                **spec.model_dump(mode="json"),
                "steps": [
                    *[step.model_dump(mode="json", by_alias=True) for step in spec.steps],
                    {
                        "invoke": child.identity.name,
                        "id": "delegate",
                        "input": {"value": "$result.value"},
                    },
                    {"project": "finished", "fields": {"value": "$result.value"}},
                ],
                "returns": "finished",
                "budget": {"provider_calls": 3, "capture_bytes": 65536},
            }
        )
    payload = lower_authoring_input(lower_procedure_spec(spec))
    definition = NamedProcedureDefinition.model_validate(payload.definition)
    procedure = NamedProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=spec.procedure),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=payload.owned_contracts,
        activation_policy="snapshot",
    )
    caps = procedure.definition.hard_caps
    line = NamedLineSpec(
        identity=ArtifactIdentity(kind="Line", name="test.measured-slot"),
        occurrence_epoch=1,
        procedure=ArtifactNameRef(target=procedure.identity),
        bindings={"compute": ArtifactNameRef(target=first.provider.identity), **source_bindings},
        parameters={"value": 1},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": caps.max_provider_calls,
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
        },
        epsilon={"$decimal": "0.1"},
        **(
            {
                "trigger_input": "captured",
                "trigger_event": NamedCaptureEventSelector(
                    capture_contract_identity=contract.identity,
                ),
            }
            if landed
            else {}
        ),
    )
    accepted = AcceptedProcedure(
        path="procedures/" + spec.procedure + ".json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    accept_tree(
        instance,
        {
            **extra_members,
            accepted.path: render_procedure(procedure),
            interface.path: render_provider_interface(interface.registration),
            first.path: render_provider(first.provider),
            provider_path(second.identity.name): render_provider(second),
            "lines/" + line.identity.name + ".json": render_line_spec(line),
        },
        name="slotted-measurement",
    )
    return instance, accepted, line, first, second, interface


def _measure(instance, accepted, run_id, *, at=None):
    return service_measure_playbill_procedure(
        instance,
        name=accepted.procedure.identity.name,
        request=ProcedureMeasureRequest(run_id=run_id, at=at, evaluation_time=NOW),
        actor_context=_actor(instance),
        recorded_at=NOW + timedelta(seconds=1),
    )


@pytest.mark.parametrize("origin", ["line", "direct"])
def test_measure_replays_explicit_build_and_refuses_retired_build(tmp_path, origin):
    instance, accepted, line, first, second, interface = _slotted_world(tmp_path)
    with pytest.raises(RequestRefusedError) as ambiguous:
        service_run_playbill_procedure(
            instance,
            name=accepted.procedure.identity.name,
            request=ProcedureRunRequest(input={"value": 1}),
            actor_context=_actor(instance),
        )
    assert ambiguous.value.error_code == "cruxible.procedure.slot_binding_ambiguous"
    operator = _Operator(_Invoker())
    if origin == "line":
        selected = first.provider
        run = service_run_playbill_line(
            instance,
            path_identity_digest=line.identity.name,
            request=LineRunRequest(line=line.identity.name),
            actor_context=_actor(instance),
            caller_rung=2,
            daemon_clock=_DeterministicClock(NOW),
            provider_runtime_operator=operator,
        )
    else:
        selected = second
        run = service_run_playbill_procedure(
            instance,
            name=accepted.procedure.identity.name,
            request=ProcedureRunRequest(
                input={"value": 1},
                bindings={"compute": selected.identity.name},
                evaluation_time=NOW,
            ),
            actor_context=_actor(instance),
            provider_runtime_operator=operator,
        )
    assert run.status == "succeeded", run.terminal
    assert run.receipt is not None and run.run_id is not None
    provider_pins = [pin for pin in run.receipt.validated_pins if pin.target.kind == "Provider"]
    assert [pin.target for pin in provider_pins] == [selected.identity]
    grain = load_playbill_procedure_run_grain(instance, run_id=run.run_id)
    assert len(grain.provider_bindings) == 1
    assert grain.provider_bindings[0].provider_artifact_digest == provider_pins[0].artifact_digest
    result = _measure(instance, accepted, run.run_id)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.reading_status == "recorded", row
    assert row.reading.run_id == run.run_id
    assert row.reading.run_receipt_digest == run.receipt_digest
    # The persisted credit commits to the selected build's exact execution pins.
    sources = {
        selected.identity: (selected, provider_pins[0].artifact_digest),
        interface.registration.identity: (interface.registration, interface.artifact_digest),
    }
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:

        def lookup(identity):
            return sources.get(identity) or (
                projection.typed.source(identity.qualified),
                projection.typed.envelope(identity.qualified).artifact_digest,
            )

        expected = bind_procedure_references(
            accepted,
            lookup=lookup,
            slots=lambda _: pytest.fail("Recorded build needs no fit search"),
            bindings={"compute": selected.identity},
        )
    journal, stream = _journal(instance)
    stored = journal.select_records(
        stream, event_kind="procedure_reading", partition_id=row.reading.journal_partition_id
    )
    assert len(stored) == 1
    reading = ProcedureReadingV1.model_validate(
        read_payload(
            instance.body_store(),
            stored[0].record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert reading.pin_set_digest == accepted_procedure_pin_set_digest(expected)
    assert _measure(instance, accepted, run.run_id).rows[0].reading_status == "replayed"

    # Missing pins and missing per-node bindings fail closed through the typed
    # measurement refusal, with authentic runs supplying the rest of the grain.
    bad_receipt = run.receipt.model_copy(update={"validated_pins": ()})
    bad = replace(grain, state=run.model_copy(update={"receipt": bad_receipt}))
    extra_contract = ArtifactPin(
        role="contract-out",
        target=ArtifactIdentity(kind="Contract", name="unrecorded-contract"),
        artifact_digest="sha256:" + "0" * 64,
    )
    mismatched_pins = run.receipt.model_copy(
        update={"validated_pins": (*run.receipt.validated_pins, extra_contract)}
    )
    pin_mismatch = replace(grain, state=run.model_copy(update={"receipt": mismatched_pins}))
    for broken in (bad, replace(grain, provider_bindings=()), pin_mismatch):
        with pytest.raises(ProcedureMeasurementRefused) as mismatch:
            _replay_measured_run(
                instance,
                accepted=accepted,
                grain=broken,
                observation=instance.accepted_coordinate(),
            )
        assert mismatch.value.error_code == "measurement_run_mismatch"
        if broken is pin_mismatch:
            assert "pins do not reproduce" in mismatch.value.details["detail"]

    admission_coordinate = run.bound_coordinate
    retired = selected.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=provider_pins[0].artifact_digest
            )
        }
    )
    retirement = {provider_path(selected.identity.name): render_provider(retired)}
    if origin == "line":
        # A live Line cannot retain a dependency on a retired build. Its
        # successor selects the other build; the old run must still refuse.
        successor = line.model_copy(
            update={
                "bindings": {"compute": ArtifactNameRef(target=second.identity)},
                "lifecycle": ArtifactLifecycle(
                    state="live", predecessor_digest=line_spec_digest(line).tagged
                ),
            }
        )
        retirement["lines/" + line.identity.name + ".json"] = render_line_spec(successor)
    accept_tree(
        instance,
        retirement,
        name="retire-recorded-build",
    )
    with pytest.raises(ProcedureMeasurementRefused) as retired_error:
        _measure(instance, accepted, run.run_id)
    assert retired_error.value.error_code == "measurement_run_mismatch"
    assert selected.identity.qualified in retired_error.value.details.values()
    # Explicit historical measurement retains its original live observation.
    assert (
        _measure(instance, accepted, run.run_id, at=admission_coordinate).rows[0].reading_status
        == "replayed"
    )


def test_measure_replays_line_with_a_landed_slotted_source_and_call(tmp_path):
    instance, accepted, line, first, _, _ = _slotted_world(tmp_path, landed=True)
    with pytest.raises(RequestRefusedError) as ambiguous:
        service_run_playbill_procedure(
            instance,
            name=accepted.procedure.identity.name,
            request=ProcedureRunRequest(
                input={"value": 1}, bindings={"compute": first.provider.identity.name}
            ),
            actor_context=_actor(instance),
        )
    assert ambiguous.value.error_code == "cruxible.procedure.slot_binding_ambiguous"
    assert "fetch" in str(ambiguous.value)
    invoker = _Invoker()
    operator = _Operator(invoker)
    producer = service_run_playbill_procedure(
        instance,
        name=accepted.procedure.identity.name,
        request=ProcedureRunRequest(
            input={"value": 1},
            evaluation_time=NOW,
            bindings={slot: ref.target.name for slot, ref in line.bindings.items()},
        ),
        actor_context=_actor(instance),
        provider_runtime_operator=operator,
        workspace_file_reader=_reader(instance, tmp_path / "workspace"),
    )
    assert producer.status == "succeeded", producer.terminal
    assert invoker.spawn_calls == 1
    assert _measure(instance, accepted, producer.run_id).rows[0].reading_status == "recorded"
    event = next(o.capture_event for o in producer.outcomes if o.event_kind == "produced_capture")
    run = service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequest(line=line.identity.name, event=event),
        actor_context=_actor(instance),
        caller_rung=2,
        daemon_clock=_DeterministicClock(NOW + timedelta(seconds=1)),
        provider_runtime_operator=operator,
        # No workspace reader: the Source consumes the exact landed Capture.
    )
    assert run.status == "succeeded", run.terminal
    assert invoker.spawn_calls == 1, "The Line consumes the Capture without refetching"
    assert run.receipt is not None and run.run_id is not None
    grain = load_playbill_procedure_run_grain(instance, run_id=run.run_id)
    # The acquisition plan records only the Call; the supplied Source does not invoke a Provider.
    assert [b.node_id for b in grain.provider_bindings] == ["result"]
    assert {p.target for p in run.receipt.validated_pins if p.target.kind == "Provider"} == {
        first.provider.identity,
        line.bindings["fetch"].target,
    }
    result = _measure(instance, accepted, run.run_id)
    assert result.rows[0].reading_status == "recorded", result
    replayed = _replay_measured_run(
        instance, accepted=accepted, grain=grain, observation=instance.accepted_coordinate()
    )
    assert replayed.procedure.pins == run.receipt.validated_pins
    assert replayed.procedure.definition.nodes[0].provider.target == line.bindings["fetch"].target
    assert _measure(instance, accepted, run.run_id).rows[0].reading_status == "replayed"
    assert grain.landed_capture_input_names == frozenset({"captured"})
    assert any(item.node_id == "read" for item in grain.node_pin_sets)
    for broken in (
        replace(grain, node_pin_sets=()),
        replace(grain, landed_capture_input_names=frozenset()),
        replace(grain, provider_bindings=()),
        replace(
            grain,
            node_pin_sets=tuple(item for item in grain.node_pin_sets if item.node_id != "result"),
        ),
    ):
        with pytest.raises(ProcedureMeasurementRefused) as mismatch:
            _replay_measured_run(
                instance,
                accepted=accepted,
                grain=broken,
                observation=instance.accepted_coordinate(),
            )
        assert mismatch.value.error_code == "measurement_run_mismatch"
