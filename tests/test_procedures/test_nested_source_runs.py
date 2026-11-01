"""Typed Procedure specs through real acceptance and shared run services."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.procedures.authored import (
    GuardStep,
    InvokeStep,
    ProjectStep,
    ReadStep,
)
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
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import initialize_local
from tests.support.lines import line_budgets, line_spec
from tests.support.mandates import procedure_mandate
from tests.support.procedures import procedure_limits, procedure_spec_data, ref, spec_input
from tests.test_indexes.test_resolution_contracts import _accept_tree

#: The run limits every spec below declares (60 s, no provider calls).
LIMITS = procedure_limits(
    wall_clock="PT120S", provider_calls=0, capture_bytes=0, items=100, repeat_attempts=3
)


def _succeeded(step: str) -> dict[str, object]:
    return {"op": "eq", "left": ref(f"$steps.{step}.succeeded"), "right": {"literal": True}}


def blueprints():
    """A child that halts unless positive, and a parent that invokes it twice."""

    child = procedure_spec_data(
        procedure="child",
        input={"positive": "bool"},
        output={"label": "string"},
        hard_caps=LIMITS,
        steps=[
            GuardStep(
                guard={"op": "eq", "left": ref("$input.positive"), "right": {"literal": True}},
                else_={"halt": "No work"},
            ),
            ProjectStep(project="label", fields={"label": "ready"}, leaf=True),
        ],
    )
    parent = procedure_spec_data(
        procedure="parent",
        params={"positive": "bool"},
        output={"label": "string"},
        hard_caps=LIMITS,
        steps=[
            InvokeStep(invoke="child", as_="first", input={"positive": ref("$params.positive")}),
            GuardStep(guard=_succeeded("first"), else_={"goto": "halted"}),
            InvokeStep(invoke="child", as_="second", input={"positive": True}),
            GuardStep(guard=_succeeded("second"), else_={"halt": "Second child failed"}),
            ProjectStep(
                project="relabelled",
                fields={"label": ref("$steps.second.value.label")},
                output={"label": "string"},
                leaf=True,
            ),
            ProjectStep(project="halted", fields={"label": "child halted"}, leaf=True),
        ],
    )
    return child, parent


def accept_blueprint(instance, owner, blueprint):
    """Author one spec through the coordinator and accept the tree it proposes."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    authored = spec_input(blueprint)
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
        if intent.semantic_identity == "Procedure:" + blueprint["procedure"]
    )
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    authored_path = procedure_path(blueprint["procedure"])
    assert (
        parse_procedure(
            lowered.proposed_tree[authored_path], path=authored_path
        ).definition.graph_format
        == 8
    )
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp="2026-08-21T12:02:00.000000Z",
        proposal_name=blueprint["procedure"],
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
    accept_blueprint(instance, owner, parent)
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
        request=ProcedureRunRequest(input={}, parameters={"positive": positive}),
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
            input={}, parameters={"positive": positive}, evaluation_time=result.evaluation_time
        ),
        actor_context=actor,
    )
    assert replay.run_id == result.run_id
    assert payloads(instance, result.run_id) == records
    journal, _ = _journal(instance)
    assert journal is not None
    assert service_get_playbill_procedure_run(instance, run_id=result.run_id).status == "succeeded"


def test_line_child_inherits_authority_and_recovery_rebuilds_only_the_recorded_delegation(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.errors import ExecutionError
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
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
    accept_blueprint(instance, owner, parent)
    accepted = _accepted_procedure(
        instance, name="parent", coordinate=instance.accepted_coordinate()
    )
    caps = accepted.procedure.definition.hard_caps
    # "parent"/"child" are a pure invoke-chain graph (no provider slots at
    # all), unlike the workspace.file advisory procedure _served_line/
    # _line_mandate are built for -- a plain, slot-free Line and Mandate here.
    line = line_spec(
        "nested-line",
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        parameters={"positive": True},
        budgets=line_budgets(caps=caps),
    )
    mandate = procedure_mandate(
        "nested-line-mandate",
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
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


def test_a_spec_combines_a_typed_claim_read_and_an_existing_query(tmp_path):
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
    incomplete = {"abort": {"code": "incomplete", "message": "Incomplete work"}}
    accept_blueprint(
        instance,
        owner,
        procedure_spec_data(
            procedure="assess-work",
            input={"item": "string"},
            output={"status": "string", "count": "int"},
            hard_caps=LIMITS,
            steps=[
                ReadStep(
                    read="status",
                    claim_type="project.work_item.status",
                    subject=ref("$input.item"),
                    subject_kind="project.work_item",
                ),
                ReadStep(read="work", query=definition.identity.name),
                GuardStep(
                    guard={
                        "op": "all_of",
                        "of": [
                            {
                                "op": "eq",
                                "left": ref("$steps.work.completed"),
                                "right": {"literal": True},
                            },
                            {
                                "op": "eq",
                                "left": ref("$steps.work.truncated"),
                                "right": {"literal": False},
                            },
                        ],
                    },
                    else_=incomplete,
                ),
                ProjectStep(
                    project="assessed",
                    fields={
                        "status": ref("$steps.status.value"),
                        "count": ref("$steps.work.result.truncation.returned_result_count"),
                    },
                    output={"status": "string", "count": "int"},
                    leaf=True,
                ),
            ],
        ),
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
