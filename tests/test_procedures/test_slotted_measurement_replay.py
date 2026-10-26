"""Measurement credits the recorded build, including when fit is ambiguous."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.cas_contracts import BodyAccessContext
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
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import provider_path, render_provider
from cruxible_core.errors import RequestRefusedError
from cruxible_core.exhaust.payloads import read_payload
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
from tests.test_procedures.test_procedure_measurement_readings import _actor, _world
from tests.test_procedures.test_procedure_source_runs import _Operator, _WorkspaceInvoker
from tests.test_procedures.test_slotted_procedures import slotted
from tests.test_providers.test_slotted_build_moves import _other

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class _Invoker(_WorkspaceInvoker):
    def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):
        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelopeV1(
                protocol_version="1.0", run_id=context.run_id, status="ok", output={"value": 42}
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(observer_backend="test", observer_grade="attribution"),
            verified_binding=bound.binding,
        )


def _slotted_world(tmp_path):
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
        bindings={"compute": ArtifactNameRef(target=first.provider.identity)},
        parameters={"value": 1},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": 1,
            "max_capture_bytes": 0,
            "max_items": caps.max_items,
        },
        epsilon={"$decimal": "0.1"},
    )
    accepted = AcceptedProcedure(
        path="procedures/" + spec.procedure + ".json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    accept_tree(
        instance,
        {
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
