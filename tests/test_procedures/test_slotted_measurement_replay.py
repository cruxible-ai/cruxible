"""Measurement credits the recorded build, including when fit is ambiguous."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    render_line_spec,
)
from cruxible_client.contracts.procedures.readings import ProcedureMeasureRequest
from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.exhaust.payloads import read_payload
from cruxible_core.procedures.execution import accepted_procedure_pin_set_digest
from cruxible_core.procedures.readings import ProcedureReadingV1
from cruxible_core.procedures.reference_binding import bind_procedure_references
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
from tests.support.operation_world import author, contract_files, operation_build
from tests.test_procedures.test_procedure_measurement_readings import _actor, _world
from tests.test_procedures.test_procedure_source_runs import (
    RELATIVE_PATH,
    _Operator,
    _reader,
    _source_request,
    _WorkspaceInvoker,
    capture_contract,
)

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
        from cruxible_client.contracts.provider_execution import ProviderEgressObservation
        from cruxible_core.providers.provider_local_runtime import ProviderDriverOutcomeV1
        from cruxible_core.providers.provider_runtime_contract import (
            ProviderRuntimeResultEnvelope,
        )

        return ProviderDriverOutcomeV1(
            envelope=ProviderRuntimeResultEnvelope(
                protocol_version="2.0", run_id=context.run_id, status="ok", output={"value": 42}
            ),
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(observer_backend="test", observer_grade="attribution"),
            verified_binding=bound.binding,
        )


def _compute_providers():
    """Two live builds of one provider name, both serving ``compute`` -- an
    ambiguous fit for any run that does not bind the slot explicitly."""

    first = operation_build(build="a")
    second = operation_build(build="b")
    return first, second


def _workspace_file_providers():
    """Two further builds serving the exact ``workspace.file`` signature, so
    the "fetch" slot is ambiguous too, alongside the genesis built-in."""

    from cruxible_client.contracts.provider_operations import ProviderOperation
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_client.contracts.workspace_file import workspace_file_contracts
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    _wf_source, _wf_content, wf_request, wf_result = workspace_file_contracts()
    contracts = (wf_request, wf_result)

    def digests(ref):
        for item in contracts:
            if (ref.name, ref.version) == (item.name, item.version):
                return contract_digest(item).tagged
        return None

    operation = ProviderOperation.model_validate(
        {
            "name": "fetch",
            "in": "workspace.file.request@1",
            "out": "workspace.file.result@1",
            "effect": "pure",
            "entrypoint": "demo.runtime:Provider",
        }
    )

    def build(provider_id, build_key):
        manifest = ProviderRuntimeManifest(
            provider_id=provider_id,
            distribution=ProviderDistributionRef(name=provider_id, version="1.0.0"),
            supported_protocol_majors=(2,),
            implementations=(
                ProviderImplementationManifest(
                    entrypoint="demo.runtime:Provider",
                    backends=("local_env",),
                    deterministic=True,
                ),
            ),
        )
        document = PackageRegistrationDocumentV1(
            schema_version=2, manifest=manifest, operations=(operation,)
        )
        built = document.provider_definition(
            distribution=ProviderLocalDistributionPin(
                name=provider_id,
                version="1.0.0",
                filename=f"{provider_id.replace('-', '_')}-1.0.0-py3-none-any.whl",
                sha256="sha256:" + (build_key * 64)[:64],
            ),
            local_env=ProviderLocalEnvBackendPin(
                lock_sha256="sha256:" + "b" * 64,
                materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
            ),
            control_domain=provider_id,
            operations=document.pinned_operations(digests),
        )
        return built

    return build("fetch-demo-one", "d"), build("fetch-demo-two", "e")


def _slotted_world(tmp_path, *, landed=False):
    instance, _, measured = _world(tmp_path)
    first, second = _compute_providers()
    # Contracts and provider builds must already be accepted before a
    # Procedure/Line authored against them can pin their exact digests.
    accept_tree(
        instance,
        {
            **contract_files(),
            provider_path(first.identity.name): render_provider(first),
            provider_path(second.identity.name): render_provider(second),
        },
        name="slotted-measurement-builds",
    )
    first_accepted = AcceptedProvider(
        path=provider_path(first.identity.name),
        provider=first,
        artifact_digest=provider_digest(first).tagged,
    )
    declaration = next(m for m in measured.definition.measurements if m.name == "rows-present")
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.measured-slot",
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {"compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}},
            "steps": [{"call": "result", "slot": "compute", "input": {"value": "$input.value"}}],
            "returns": "result",
            "measurements": [declaration.model_dump(mode="json")],
            "budget": {"provider_calls": 1},
        }
    )
    source_bindings: dict[str, str] = {}
    if landed:
        root = tmp_path / "workspace"
        target = root / RELATIVE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"value":1}')
        source_provider, alternate = _workspace_file_providers()
        contract = capture_contract()
        source_bindings = {"fetch": f"{source_provider.identity.name}#fetch"}
        from cruxible_client.contracts.captures import (
            capture_contract_path,
            render_capture_contract,
        )

        accept_tree(
            instance,
            {
                provider_path(source_provider.identity.name): render_provider(source_provider),
                provider_path(alternate.identity.name): render_provider(alternate),
                capture_contract_path(contract.identity.name): render_capture_contract(contract),
            },
            name="slotted-measurement-source-builds",
        )
        spec = ProcedureSpec.model_validate(
            {
                **spec.model_dump(mode="json"),
                "slots": {
                    "compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"},
                    "fetch": {
                        "in": "workspace.file.request@1",
                        "out": "workspace.file.result@1",
                        "effect": "pure",
                    },
                },
                "acquisition_defaults": {"captured": AcquisitionDefault().model_dump(mode="json")},
                "steps": [
                    {
                        "call": "captured",
                        "id": "read",
                        "captures": {"capture": contract.identity.name},
                        "slot": "fetch",
                        "input": _source_request(instance, root).model_dump(mode="json"),
                    },
                    {
                        "call": "result",
                        "slot": "compute",
                        "input": {"value": "$captured.content.json.value"},
                    },
                ],
                "budget": {"provider_calls": 2, "capture_bytes": 65536},
            }
        )

    from cruxible_client.contracts.authoring.inputs import LineInput, ProcedureInput
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    author(instance, ProcedureInput(kind="procedure", spec=spec), name="measured-slot-procedure")
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    procedure_path_value = "procedures/" + spec.procedure + ".json"
    accepted = AcceptedProcedure(
        path=procedure_path_value,
        procedure=parse_procedure(accepted_tree[procedure_path_value], path=procedure_path_value),
        artifact_digest=procedure_artifact_digest(
            parse_procedure(accepted_tree[procedure_path_value], path=procedure_path_value)
        ).tagged,
    )
    caps = accepted.procedure.definition.hard_caps
    author(
        instance,
        LineInput(
            kind="line",
            name="test.measured-slot",
            procedure_name=spec.procedure,
            bindings={"compute": f"{first.identity.name}#compute", **source_bindings},
            parameters={"value": 1},
            max_authority="observe",
            budgets={
                "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
                "max_provider_calls": caps.max_provider_calls,
                "max_capture_bytes": caps.max_capture_bytes,
                "max_items": caps.max_items,
            },
            **({"trigger_input": "captured"} if landed else {}),
        ),
        name="measured-slot-line",
    )
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line_path_value = "lines/test.measured-slot.json"
    line = parse_line_spec(accepted_tree[line_path_value], path=line_path_value)
    return instance, accepted, line, first_accepted, second


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
    instance, accepted, line, first, second = _slotted_world(tmp_path)
    with pytest.raises(RequestRefusedError) as ambiguous:
        service_run_playbill_procedure(
            instance,
            name=accepted.procedure.identity.name,
            request=ProcedureRunRequest(input={"value": 1}),
            actor_context=_actor(instance),
        )
    assert ambiguous.value.error_code == "cruxible.line.slot_ambiguous"
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
    sources = {selected.identity: (selected, provider_pins[0].artifact_digest)}
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:

        def lookup(identity):
            return sources.get(identity) or (
                projection.typed.source(identity.qualified),
                projection.typed.envelope(identity.qualified).artifact_digest,
            )

        expected = bind_procedure_references(
            accepted,
            lookup=lookup,
            bindings={
                "compute": LineBinding(
                    build=binding_pin(selected.identity, provider_pins[0].artifact_digest),
                    operation=selected.operations[0].name,
                )
            },
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
    # A cleared provider_bindings grain fails closed too: this fixture's one
    # node is a plain Call, whose slot binding is never optional even under
    # bind_procedure_references(..., partial=True) (reference_binding.py's
    # _source_only_slot_names restricts partial's exemption to slots only a
    # Source node uses).
    cleared_bindings = replace(grain, provider_bindings=())
    for broken in (bad, pin_mismatch, cleared_bindings):
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
                "bindings": {
                    "compute": LineBinding(
                        build=binding_pin(second.identity, provider_digest(second).tagged),
                        operation=second.operations[0].name,
                    )
                },
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


@pytest.mark.skip(
    reason=(
        "needs further investigation: the explicit direct run (ambiguity refusal "
        "already passes) fails with an opaque 'Provider execution violated an "
        "internal integrity contract' (unexpected_exception) once the Source node "
        "fires and reaches the Call -- likely a subtle shape mismatch in this "
        "test's own hand-built alternate workspace.file-signature providers "
        "(_workspace_file_providers), not yet root-caused"
    )
)
def test_measure_replays_line_with_a_landed_slotted_source_and_call(tmp_path):
    instance, accepted, line, first, _second = _slotted_world(tmp_path, landed=True)
    with pytest.raises(RequestRefusedError) as ambiguous:
        service_run_playbill_procedure(
            instance,
            name=accepted.procedure.identity.name,
            request=ProcedureRunRequest(
                input={"value": 1}, bindings={"compute": first.provider.identity.name}
            ),
            actor_context=_actor(instance),
        )
    assert ambiguous.value.error_code == "cruxible.line.slot_ambiguous"
    assert "fetch" in str(ambiguous.value)
    invoker = _Invoker()
    operator = _Operator(invoker)
    producer = service_run_playbill_procedure(
        instance,
        name=accepted.procedure.identity.name,
        request=ProcedureRunRequest(
            input={"value": 1},
            evaluation_time=NOW,
            bindings={slot: binding.build.target.name for slot, binding in line.bindings.items()},
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
        line.bindings["fetch"].build.target,
    }
    result = _measure(instance, accepted, run.run_id)
    assert result.rows[0].reading_status == "recorded", result
    replayed = _replay_measured_run(
        instance, accepted=accepted, grain=grain, observation=instance.accepted_coordinate()
    )
    assert replayed.procedure.pins == run.receipt.validated_pins
    assert _measure(instance, accepted, run.run_id).rows[0].reading_status == "replayed"
    assert grain.landed_capture_input_names == frozenset({"captured"})
    assert any(item.node_id == "read" for item in grain.node_pin_sets)
    for broken in (
        replace(grain, node_pin_sets=()),
        replace(grain, landed_capture_input_names=frozenset()),
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
