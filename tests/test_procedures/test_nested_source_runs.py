"""Retained source authoring through real acceptance and shared run services."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.authoring.source import halt, invoke, procedure
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequest,
    SourceProcedureSelection,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    _journal,
    _records_for_run,
    service_get_playbill_procedure_run,
    service_run_playbill_procedure,
)
from cruxible_core.service.procedures.source_preview import service_preview_procedure_source
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import initialize_local
from tests.support.procedures import RUN_WALL_CLOCK
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_procedure_execution import _budget, _hard_caps


def blueprints():
    Request = CarriedContractInput(
        name="decision.request", fields={"positive": PropertySchema(type="bool")}
    )
    Result = CarriedContractInput(
        name="decision.result", fields={"label": PropertySchema(type="string")}
    )

    @procedure(
        name="child",
        input=Request,
        output=Result,
        budget=_budget().model_copy(update={"max_items": None}),
        hard_caps=_hard_caps(),
    )
    def child(request):
        if not request.positive:
            return halt("No work")
        return Result.value(label="ready")

    @procedure(
        name="parent",
        input=Request,
        output=Result,
        budget=_budget().model_copy(update={"max_items": None}),
        hard_caps=_hard_caps(),
    )
    def parent(request, bindings):
        first = invoke(bindings.child, input=bindings.child.input(positive=request.positive))
        if not first.succeeded:
            return Result.value(label="child halted")
        second = invoke(bindings.child, input=bindings.child.input(positive=True))
        if not second.succeeded:
            return halt("Second child failed")
        return Result.value(label=second.value.label)

    return child, parent


def accept_blueprint(instance, owner, blueprint, **bindings):
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    source = blueprint._at(SimpleNamespace())
    if bindings:
        source = source.model_copy(
            update={
                "bindings": {
                    key: (SourceProcedureSelection(name=name) if isinstance(name, str) else name)
                    for key, name in bindings.items()
                }
            }
        )
    preview = service_preview_procedure_source(
        instance,
        request=ProcedureSourcePreviewRequest(
            source=source,
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
    )
    assert preview.ready_for_prepare, preview.errors
    authored = ProcedureInput(
        kind="procedure",
        activation_policy=blueprint.activation_policy,
        definition={"name": blueprint.name, "source_request": source.model_dump(mode="json")},
    )
    assert "sha256:" not in authored.model_dump_json()
    assert "artifact_digest" not in authored.model_dump_json()
    compiled = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(authored),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert compiled.verdict == "passed", compiled.frontier.model_dump_json()
    pending = next(
        intent
        for intent in coordinator.list_pending(actor=actor).intents
        if intent.semantic_identity == "Procedure:" + blueprint.name
    )
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_core.compiler.compiler import CURRENT_COMPILER

    authored_path = procedure_path(blueprint.name)
    assert parse_procedure(
        lowered.proposed_tree[authored_path], path=authored_path
    ).definition.graph_format == (
        7 if instance.accepted_coordinate().compiler == CURRENT_COMPILER else 6
    )
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp="2026-08-21T12:02:00.000000Z",
        proposal_name=blueprint.name,
    )


def payloads(instance, run_id):
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    return [
        (
            row.record.event_kind,
            parse_journal_payload(
                instance.body_store().read(row.record.payload_digest, access=access)
            ),
        )
        for row in _records_for_run(instance, run_id)
    ]


@pytest.mark.parametrize("positive", [True, False])
def test_child_occurrences_have_exact_bindings_and_replay_without_execution(
    tmp_path, monkeypatch, positive
):
    instance, owner = initialize_local(tmp_path)
    child, parent = blueprints()
    accept_blueprint(instance, owner, child)
    accept_blueprint(instance, owner, parent, child="child")
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

    definition = _accepted_procedure(
        instance, name="parent", coordinate=instance.accepted_coordinate()
    ).procedure.definition
    assert [node.as_ for node in definition.nodes if node.kind == "invoke"] == ["first", "second"]
    body_store = type(instance.body_store())
    read_body = body_store.read
    verification_reads = []

    def track_read(store, digest, *, access):
        if access.principal_id == "nested-procedure":
            verification_reads.append(digest)
        return read_body(store, digest, access=access)

    monkeypatch.setattr(body_store, "read", track_read)
    actor = GovernedActorContext(
        actor_id="owner",
        actor_type="human_user",
        org_id=instance.descriptor.instance_id,
        operation_id="test-nested",
        timestamp=datetime(2026, 8, 21, 12, 3, tzinfo=timezone.utc),
    )
    result = service_run_playbill_procedure(
        instance,
        name="parent",
        request=ProcedureRunRequest(input={"positive": positive}),
        actor_context=actor,
    )
    assert result.status == "succeeded", payloads(instance, result.run_id)[-1][1]
    records = payloads(instance, result.run_id)
    children = [
        value
        for kind, value in records
        if kind == "child_invocation" and value["verdict"] == "completed"
    ]
    assert len(children) == (2 if positive else 1)
    assert len(verification_reads) == 2 * len(children)
    assert len({item["receipt"]["run_id"] for item in children}) == len(children)
    for child_record in children:
        child_payloads = payloads(instance, child_record["receipt"]["run_id"])
        admission = next(
            value["admission"] for kind, value in child_payloads if kind == "admission_bound"
        )
        assert admission["parent_binding"]["parent_run_id"] == result.run_id
        assert admission["parent_binding"]["ancestors"][0]["target"]["name"] == "parent"
        assert admission["accepted_coordinate"] == next(
            value["admission"]["accepted_coordinate"]
            for kind, value in records
            if kind == "admission_bound"
        )
        assert child_payloads[-1][1]["output"] == ({"label": "ready"} if positive else None)
        assert child_payloads[-1][1]["status"] == ("succeeded" if positive else "halted")
    assert records[-1][1]["output"] == {"label": "ready" if positive else "child halted"}

    from cruxible_client import contracts as api
    from cruxible_client.authoring.sdk import ProcedureRun

    class Client:
        def get_procedure_run(self, instance_id, run_id):
            assert instance_id == instance.descriptor.instance_id
            return api.ProcedureRunState.model_validate(
                service_get_playbill_procedure_run(instance, run_id=run_id).model_dump(mode="json")
            )

    pb = SimpleNamespace(_client=Client(), _instance_id=instance.descriptor.instance_id)
    wrapped = ProcedureRun(pb, api.ProcedureRunState.model_validate(result.model_dump(mode="json")))
    assert [child.run_id for child in wrapped.children] == [
        item["receipt"]["run_id"] for item in children
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("Completed parent replay must not execute any child")

    from cruxible_core.service.procedures.nested_runs import ServedNestedProcedureRunner

    monkeypatch.setattr(ServedNestedProcedureRunner, "run", forbidden)
    # The same evaluation instant is the same run: a direct run without one
    # evaluates at its admission instant, so the replay names the first's.
    replay = service_run_playbill_procedure(
        instance,
        name="parent",
        request=ProcedureRunRequest(
            input={"positive": positive}, evaluation_time=result.evaluation_time
        ),
        actor_context=actor,
    )
    assert replay.run_id == result.run_id
    assert payloads(instance, result.run_id) == records
    journal, _ = _journal(instance)
    assert journal is not None
    assert service_get_playbill_procedure_run(instance, run_id=result.run_id).status == "succeeded"


@pytest.mark.skip(
    reason=(
        "needs reconstruction against the new operation model: imports "
        "call_procedure/operation from tests.test_providers.test_operation_contracts, "
        "a module that no longer exists (test_provider_invocation_journal.py's own "
        "migration replaced that pattern with _accepted_one_provider()/_provider_for(), "
        "which build a single Line/Call world, not the parent-invokes-child nested "
        "scenario this test needs); also calls _prepared_acquisition_run(..., "
        "operation_contract=operation()), a kwarg that function no longer takes."
    )
)
@pytest.mark.parametrize("planning_delay", [False, True])
def test_calls_share_the_parent_provider_budget(tmp_path, planning_delay):
    import textwrap

    from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
    from cruxible_client.contracts.procedures.source_compiler import compile_source
    from cruxible_client.contracts.procedures.source_program import (
        ProcedureSourceProgram,
        SourceProcedureBinding,
    )
    from cruxible_core.procedures.execution import ProcedureExecutor
    from cruxible_core.procedures.nested import constrained_budget, constrained_caps
    from tests.test_procedures.test_procedure_execution import (
        _Authority,
        _Contracts,
        _prepare,
        _StateReader,
    )
    from tests.test_procedures.test_source_compiler import INPUT, OUTPUT, accepted
    from tests.test_providers.test_operation_contracts import call_procedure, operation
    from tests.test_providers.test_provider_invocation_journal import (
        _Invoker,
        _prepared_acquisition_run,
    )

    child = call_procedure()
    base, fixture = _prepared_acquisition_run(child, tmp_path, operation_contract=operation())
    compiled = compile_source(
        ProcedureSourceProgram(
            text=textwrap.dedent("""
                def example(request, bindings):
                    first = invoke(bindings.child, input=bindings.child.input(size=1))
                    if not first.succeeded:
                        return halt('first failed')
                    second = invoke(bindings.child, input=bindings.child.input(size=2))
                    if not second.succeeded:
                        return Output.value(value='budget preserved')
                    return Output.value(value='unexpected second call')
            """),
            filename="budget.py",
            function="example",
            contracts={"Output": OUTPUT},
            bindings={
                "child": SourceProcedureBinding(
                    name=child.procedure.identity.name,
                    input=operation().input,
                    output=operation().output,
                )
            },
        ),
        name="example",
        input=INPUT,
        output=OUTPUT,
        budget=_budget().model_copy(update={"max_provider_calls": 1}),
        hard_caps=_hard_caps().model_copy(update={"max_provider_calls": 1}),
    )
    parent = accepted(compiled)
    invoker = _Invoker()

    class Clock:
        ticks = 0

        def now(self):
            return base.admission.admitted_at

        def monotonic_ns(self):
            return self.ticks

    clock = Clock()

    class Runner:
        budgets = []

        def preflight(self, accepted, admission):
            pass

        def run(self, context, value):
            caps = constrained_caps(
                child.procedure.definition.hard_caps, context.admission.hard_caps
            )
            budget = constrained_budget(child.procedure.definition.budget, context.remaining, caps)
            self.budgets.append(budget.max_provider_calls)
            child_base = base.model_copy(
                update={
                    "acquisition_plan": base.acquisition_plan.model_copy(
                        update={"selection_receipt_digest": None}
                    ),
                    "admission": base.admission.model_copy(
                        update={
                            "budget": budget,
                            "hard_caps": caps,
                            "selection_receipt_digest": None,
                        }
                    ),
                }
            )
            prepared = context.bind(child_base)
            if planning_delay:
                # Planning the child spends one second more than the whole run's budget.
                clock.ticks += (RUN_WALL_CLOCK.microseconds + 1_000_000) * 1_000
            fixture.journal.activate_writer(
                prepared.admission.journal_stream,
                prepared.admission.journal_partition_id,
                fencing_token="writer",
                expected_head=fixture.journal.read_head(
                    prepared.admission.journal_stream, prepared.admission.journal_partition_id
                ),
            )
            return ProcedureExecutor(
                journal=fixture.journal,
                bodies=fixture.bodies,
                run_index=fixture.run_index,
                fencing_token="writer",
                activation_authority=_Authority(child.artifact_digest),
                contract_validator=_Contracts(),
                provider_runtime_invoker=invoker,
                parent_context=context,
                clock=clock,
            ).execute(prepared, child)

    runner = Runner()
    preparation = _prepare(
        parent, fixture, _StateReader(), invocation_input={"choice": True, "count": 1}
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(parent.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(parent),
        nested_runner=runner,
        clock=clock,
    ).execute(preparation, parent)
    if planning_delay:
        assert result.status == "failed"
        final = fixture.journal.all_records(
            preparation.admission.journal_stream, preparation.admission.journal_partition_id
        )[-1]
        final_payload = parse_journal_payload(
            fixture.bodies.read(
                final.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        assert final_payload["failure_code"] == "wall_clock_exhausted"
        assert invoker.calls == []
        return
    final = fixture.journal.all_records(
        preparation.admission.journal_stream, preparation.admission.journal_partition_id
    )[-1]
    assert result.status == "succeeded", parse_journal_payload(
        fixture.bodies.read(
            final.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )["failure"]
    assert result.output == {"value": "budget preserved"}
    assert runner.budgets == [1, 0]
    assert len(invoker.calls) == 1


def test_line_child_inherits_authority_and_recovery_rebuilds_only_the_recorded_delegation(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
    from cruxible_client.contracts.errors import ExecutionError
    from cruxible_client.contracts.procedure_mandates import (
        ProcedureMandate,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        LineSpec,
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from cruxible_core.procedures.execution import ProcedureRunAdmission
    from cruxible_core.procedures.nested import authority_procedure
    from cruxible_core.service.procedures.nested_runs import retained_delegation
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure
    from tests.test_procedures.test_procedure_source_runs import _accept_more, _run_line

    instance, owner = initialize_local(tmp_path)
    child, parent = blueprints()
    accept_blueprint(instance, owner, child)
    accept_blueprint(instance, owner, parent, child="child")
    accepted = _accepted_procedure(
        instance, name="parent", coordinate=instance.accepted_coordinate()
    )
    caps = accepted.procedure.definition.hard_caps
    # "parent"/"child" are a pure invoke-chain graph (no provider slots at
    # all), unlike the workspace.file advisory procedure _served_line/
    # _line_mandate are built for -- a plain, slot-free Line and Mandate here.
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name="nested-line"),
        occurrence_epoch=1,
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        parameters={"positive": True},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": caps.max_provider_calls,
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
        },
        epsilon={"$decimal": "0"},
    )
    mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="nested-line-mandate"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="propose",
        resource_ceiling=caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    _accept_more(
        instance,
        owner,
        {
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="nested-line",
    )
    result, _ = _run_line(instance, tmp_path / "workspace", line)
    assert result.status == "succeeded", result.model_dump_json()
    completed = [
        value
        for kind, value in payloads(instance, result.run_id)
        if kind == "child_invocation" and value["verdict"] == "completed"
    ]
    assert len(completed) == 2
    for link in completed:
        admitted = next(
            value["admission"]
            for kind, value in payloads(instance, link["receipt"]["run_id"])
            if kind == "admission_bound"
        )
        admission = ProcedureRunAdmission.model_validate(admitted)
        assert admission.parent_binding is not None
        assert admission.invocation_origin == "line"
        assert admission.line_identity == line.identity
        assert admission.journal_partition_id != result.receipt.partition_id
        proof = retained_delegation(instance, admission)
        assert authority_procedure(admission, proof).target.name == "parent"
        with pytest.raises(ExecutionError, match="verified parent"):
            authority_procedure(admission)
        forged = admission.model_copy(
            update={
                "actor_context": admission.actor_context.model_copy(update={"actor_id": "another"})
            }
        )
        with pytest.raises(ExecutionError, match="parent"):
            authority_procedure(forged, proof)
        assert (
            service_get_playbill_procedure_run(instance, run_id=admission.run_id).status
            == "succeeded"
        )


def test_source_combines_typed_field_read_and_existing_query(tmp_path):
    from cruxible_client.authoring.source import query, require
    from cruxible_client.contracts.procedures.source_requests import SourceQuerySelection
    from cruxible_client.contracts.query.definitions import (
        query_definition_path,
        render_query_definition,
    )
    from tests.core_support._knowledge_loop_support import seed_claims, work_item_query
    from tests.test_procedures.test_procedure_source_runs import _accept_more

    instance, owner = seed_claims(tmp_path)
    definition = work_item_query()
    _accept_more(
        instance,
        owner,
        {
            query_definition_path(definition.identity.name): render_query_definition(definition),
        },
        name="work-query",
    )
    Request = CarriedContractInput(
        name="assessment.input", fields={"item": PropertySchema(type="string")}
    )
    Result = CarriedContractInput(
        name="assessment.output",
        fields={
            "status": PropertySchema(type="string"),
            "count": PropertySchema(type="int"),
        },
    )

    @procedure(
        name="assess-work",
        input=Request,
        output=Result,
        budget=_budget().model_copy(update={"max_items": None}),
        hard_caps=_hard_caps(),
    )
    def assessment(request, world, bindings):
        item = world.project.work_item[request.item]
        status = item.status.one()
        work = query(bindings.work, parameters=bindings.work.parameters())
        require(work.completed and not work.truncated, code="incomplete", message="Incomplete work")
        return Result.value(status=status.value, count=work.result.truncation.returned_result_count)

    accept_blueprint(
        instance, owner, assessment, work=SourceQuerySelection(name=definition.identity.name)
    )
    actor = GovernedActorContext(
        actor_id="owner",
        actor_type="human_user",
        org_id=instance.descriptor.instance_id,
        operation_id="assess-work",
        timestamp=datetime(2026, 8, 21, 12, 3, tzinfo=timezone.utc),
    )
    result = service_run_playbill_procedure(
        instance,
        name="assess-work",
        request=ProcedureRunRequest(input={"item": "wi-42"}),
        actor_context=actor,
    )
    assert result.status == "succeeded", result.model_dump_json(indent=2)
    assert result.result == {"status": "ready", "count": 2}
    admission = next(
        value["admission"]
        for kind, value in payloads(instance, result.run_id)
        if kind == "admission_bound"
    )
    assert {value["kind"] for value in admission["accepted_state_inputs"]} == {
        "accepted_claim",
        "accepted_state_v2",
    }


@pytest.mark.skip(
    reason=(
        "blocked on tests.test_procedures.test_slotted_measurement_replay, a "
        "sibling fork's in-progress file (still ModuleNotFoundError on "
        "acquisition_policies as of this writing) that this test imports "
        "_slotted_world/_Invoker/NOW/_actor from -- re-enable once that file "
        "collects."
    )
)
def test_crashed_child_recovers_delegation_from_a_landed_slotted_source(tmp_path):
    from datetime import timedelta

    from cruxible_core.procedures.execution import ProcedureRunAdmission, procedure_node_pin_sets
    from cruxible_core.procedures.nested import authority_procedure
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.procedures.nested_runs import retained_delegation
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        ProcedureRunRecoveryRequired,
        _DeterministicClock,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_procedure_source_runs import _Operator, _reader
    from tests.test_procedures.test_slotted_measurement_replay import (
        NOW,
        _actor,
        _Invoker,
        _slotted_world,
    )

    class Crash(BaseException):
        pass

    class Invoker(_Invoker):
        crash = False
        child_run_id = None

        def invoke_provider(self, *, occurrence, context, invocation_id, bound, deadline):
            if self.crash and occurrence.node_id == "child-call":
                self.child_run_id = context.run_id
                raise Crash()
            return super().invoke_provider(
                occurrence=occurrence,
                context=context,
                invocation_id=invocation_id,
                bound=bound,
                deadline=deadline,
            )

    instance, accepted, line, _, _, _ = _slotted_world(
        tmp_path,
        landed=True,
        invoke_child=True,
    )
    invoker = Invoker()
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
    event = next(o.capture_event for o in producer.outcomes if o.event_kind == "produced_capture")
    invoker.crash = True
    with pytest.raises(Crash):
        service_run_playbill_line(
            instance,
            path_identity_digest=line.identity.name,
            request=LineRunRequest(line=line.identity.name, event=event),
            actor_context=_actor(instance),
            caller_rung=2,
            daemon_clock=_DeterministicClock(NOW + timedelta(seconds=1)),
            provider_runtime_operator=operator,
        )
    assert invoker.spawn_calls == 1, "The Line consumes the Capture without refetching"
    assert invoker.child_run_id is not None
    child_records = payloads(instance, invoker.child_run_id)
    admission = ProcedureRunAdmission.model_validate(
        next(value["admission"] for kind, value in child_records if kind == "admission_bound")
    )
    assert admission.parent_binding is not None
    parent_records = payloads(instance, admission.parent_binding.parent_run_id)
    parent = ProcedureRunAdmission.model_validate(
        next(value["admission"] for kind, value in parent_records if kind == "admission_bound")
    )
    assert parent.landed_capture_inputs[0].input_name == "captured"
    assert [binding.node_id for binding in parent.resolved_provider_bindings] == ["result"]
    assert "attempt_finalized" not in {kind for kind, _ in child_records}
    assert "attempt_finalized" not in {kind for kind, _ in parent_records}
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    recovered = retained_delegation(reopened, admission)
    assert authority_procedure(admission, recovered).target == accepted.procedure.identity
    assert recovered.context.accepted.procedure.pins == parent.full_pins
    assert procedure_node_pin_sets(recovered.context.accepted) == parent.node_pin_sets
    source = recovered.context.accepted.procedure.definition.nodes[0]
    assert source.node_id == "read" and source.as_ == "captured"
    assert source.provider.target == line.bindings["fetch"].target
    # Explicit invalid retained inputs exercise the recovery boundary without
    # changing the authentic journal or substituting any production service.
    for broken in (
        admission.model_copy(
            update={
                "full_pins": tuple(
                    pin for pin in admission.full_pins if pin.target.kind != "Provider"
                ),
            }
        ),
        admission.model_copy(update={"node_pin_sets": ()}),
        admission.model_copy(
            update={
                "actor_context": admission.actor_context.model_copy(update={"actor_id": "another"}),
            }
        ),
    ):
        with pytest.raises(ProcedureRunRecoveryRequired) as refused:
            retained_delegation(reopened, broken)
        assert refused.value.code == "cruxible.procedure.run.recovery_required"
