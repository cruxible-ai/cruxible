"""Retained source authoring through real acceptance and shared run services."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput, ProcedureInput
from cruxible_client.authoring.source import halt, invoke, procedure
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreviewRequestV1,
    SourceProcedureSelection,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequestV2,
    _journal,
    _records_for_run,
    service_get_playbill_procedure_run,
    service_run_playbill_procedure,
)
from cruxible_core.service.procedures.source_preview import service_preview_procedure_source
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import initialize_local
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
        request=ProcedureSourcePreviewRequestV1(
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
        request=ProcedureRunRequestV2(input={"positive": positive}),
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
        def get_playbill_procedure_run(self, instance_id, run_id):
            assert instance_id == instance.descriptor.instance_id
            return api.PlaybillProcedureRunState.model_validate(
                service_get_playbill_procedure_run(instance, run_id=run_id).model_dump(mode="json")
            )

    pb = SimpleNamespace(_client=Client(), _instance_id=instance.descriptor.instance_id)
    wrapped = ProcedureRun(
        pb, api.PlaybillProcedureRunState.model_validate(result.model_dump(mode="json"))
    )
    assert [child.run_id for child in wrapped.children] == [
        item["receipt"]["run_id"] for item in children
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("Completed parent replay must not execute any child")

    from cruxible_core.service.procedures.nested_runs import ServedNestedProcedureRunner

    monkeypatch.setattr(ServedNestedProcedureRunner, "run", forbidden)
    replay = service_run_playbill_procedure(
        instance,
        name="parent",
        request=ProcedureRunRequestV2(input={"positive": positive}),
        actor_context=actor,
    )
    assert replay.run_id == result.run_id
    assert payloads(instance, result.run_id) == records
    journal, _ = _journal(instance)
    assert journal is not None
    assert service_get_playbill_procedure_run(instance, run_id=result.run_id).status == "succeeded"


@pytest.mark.parametrize("planning_delay", [False, True])
def test_calls_share_the_parent_provider_budget(tmp_path, planning_delay):
    import textwrap

    from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
    from cruxible_client.contracts.procedures.source_compiler import compile_source
    from cruxible_client.contracts.procedures.source_program import (
        ProcedureSourceV1,
        SourceProcedureBinding,
    )
    from cruxible_core.procedures.execution import ProcedureExecutor
    from cruxible_core.procedures.nested import constrained_budget, constrained_caps
    from cruxible_core.providers.provider_classifiers import ProviderBucketClassifierRegistry
    from tests.core_support._p2b1_support import install_demo_classifier
    from tests.test_procedures.test_procedure_execution import (
        _Authority,
        _Contracts,
        _prepare,
        _StateReader,
    )
    from tests.test_procedures.test_source_compiler import INPUT, OUTPUT, accepted
    from tests.test_providers.test_operation_contracts import call_procedure, operation
    from tests.test_providers.test_provider_invocation_journal import _Invoker, _prepared_v5

    child = call_procedure()
    base, fixture = _prepared_v5(child, tmp_path, operation_contract=operation())
    compiled = compile_source(
        ProcedureSourceV1(
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
                    version=child.artifact_digest,
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
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
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
                clock.ticks += 2_000_000_000
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
                provider_classifier_registry=registry,
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
    from cruxible_client.contracts.acquisition_policies import (
        acquisition_policy_path,
        render_acquisition_policy,
    )
    from cruxible_client.contracts.errors import PlaybillExecutionError
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_core.procedures.execution import ProcedureRunAdmissionV8
    from cruxible_core.procedures.nested import authority_procedure
    from cruxible_core.service.procedures.nested_runs import retained_delegation
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure
    from tests.test_procedures.test_procedure_source_runs import (
        _accept_more,
        _line_mandate,
        _policy,
        _run_line,
        _served_line,
    )

    instance, owner = initialize_local(tmp_path)
    child, parent = blueprints()
    accept_blueprint(instance, owner, child)
    accept_blueprint(instance, owner, parent, child="child")
    accepted = _accepted_procedure(
        instance, name="parent", coordinate=instance.accepted_coordinate()
    )
    policy = _policy()
    line = _served_line(accepted.procedure, policy).model_copy(
        update={"parameters": {"positive": True}}
    )
    mandate = _line_mandate(accepted.procedure)
    _accept_more(
        instance,
        owner,
        {
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
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
        admission = ProcedureRunAdmissionV8.model_validate(admitted)
        assert admission.invocation_origin == "line"
        assert admission.line_identity == line.identity
        assert admission.journal_partition_id != result.receipt.partition_id
        proof = retained_delegation(instance, admission)
        assert authority_procedure(admission, proof).target.name == "parent"
        with pytest.raises(PlaybillExecutionError, match="verified parent"):
            authority_procedure(admission)
        forged = admission.model_copy(
            update={
                "actor_context": admission.actor_context.model_copy(update={"actor_id": "another"})
            }
        )
        with pytest.raises(PlaybillExecutionError, match="parent"):
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
        request=ProcedureRunRequestV2(input={"item": "wi-42"}),
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
