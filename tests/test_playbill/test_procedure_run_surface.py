"""PC-G5 served query-only Procedure readiness and run laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    PredicateOperandV1,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureBudgetExhaustedV1,
    ProcedureHaltTerminalV1,
    ProcedureNodeRefusalV1,
    ProcedureOperationalFailureV1,
    ProcedureRunReceiptV2,
    ProcedureRunReceiptV3,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import ProcedureExhaustWriter, parse_journal_payload
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_procedure_runs import (
    DirectProcedureReceiptReducer,
    ProcedureBindingTargetV1,
    ProcedureBindRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
    ProcedureSlotBindingRequestV1,
    service_bind_playbill_procedure,
    service_get_playbill_procedure_run,
    service_playbill_procedure_readiness,
    service_run_playbill_procedure,
)
from tests.test_playbill._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import FIXED_TIMESTAMP, initialize_local
from tests.test_playbill.test_procedure_owned_contracts import (
    _accepted_query_procedure,
    _activate_procedure,
    _contract,
)

READ_TIME = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)


def test_genesis_evaluation_time_comes_from_the_signed_commit(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)

    assert instance.accepted_evaluation_time(instance.accepted_coordinate().git_oid) == (
        datetime.fromisoformat(FIXED_TIMESTAMP)
    )


def _world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = seed_claims(tmp_path)
    query = work_item_query()
    inspection = service_propose_playbill_query_definition(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="served-procedure-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    procedure = _accepted_query_procedure(query_definition_digest(query).tagged).procedure
    _activate_procedure(
        instance,
        owner,
        procedure,
        sequence=4,
        timestamp="2026-08-24T15:00:00.000000Z",
    )

    return instance, owner, procedure


def _actor(instance) -> GovernedActorContext:  # type: ignore[no-untyped-def]
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="owner",
        org_id=instance.descriptor.instance_id,
        operation_id="served-procedure-test",
        timestamp=READ_TIME,
    )


def _list_output_successor(procedure, *, max_items: int):  # type: ignore[no-untyped-def]
    old_output_pin = procedure.definition.contract_out
    assert isinstance(old_output_pin, ArtifactPin)
    project = procedure.definition.nodes[-1]
    list_output = _contract(
        "query-rows-list",
        {
            "rows": PropertySchema(
                type="list",
                item_fields={
                    "bindings": PropertySchema(type="json"),
                    "conflicts": PropertySchema(type="json"),
                    "fields": PropertySchema(type="json"),
                    "includes": PropertySchema(type="json"),
                    "path": PropertySchema(type="json"),
                    "read_claims": PropertySchema(type="json"),
                    "relation_claim": PropertySchema(type="json", optional=True),
                    "result_subject_identity": PropertySchema(type="string"),
                    "tag": PropertySchema(type="string"),
                },
            )
        },
    )
    list_output_pin = ArtifactPin(
        role="contract-out",
        target=list_output.identity,
        artifact_digest=procedure_owned_contract_digest(list_output).tagged,
    )
    definition = procedure.definition.model_copy(
        update={
            "contract_out": list_output_pin,
            "nodes": (
                *procedure.definition.nodes[:-1],
                project.model_copy(update={"contract_out": list_output_pin}),
            ),
            "budget": procedure.definition.budget.model_copy(update={"max_items": max_items}),
        }
    )
    return procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "pins": tuple(
                sorted(
                    (list_output_pin if pin == old_output_pin else pin for pin in procedure.pins),
                    key=lambda pin: (
                        pin.role.encode(),
                        pin.target.qualified.encode(),
                        pin.artifact_digest.encode(),
                    ),
                )
            ),
            "owned_contracts": tuple(
                sorted(
                    (
                        list_output if contract.identity == old_output_pin.target else contract
                        for contract in procedure.owned_contracts
                    ),
                    key=lambda contract: canonical_bytes(contract.model_dump(mode="json")),
                )
            ),
            "lifecycle": procedure.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(procedure).tagged}
            ),
        }
    )


def test_readiness_and_idempotent_run_use_the_accepted_query_engine(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    readiness = service_playbill_procedure_readiness(
        instance,
        name=procedure.identity.name,
        request=ProcedureReadinessRequestV1(evaluation_time=READ_TIME),
    )

    assert readiness.state == "ready"
    assert readiness.next_operation.kind == "run"
    assert readiness.required_slots == ()
    assert readiness.unsupported_nodes == ()
    request = ProcedureRunRequestV2(evaluation_time=READ_TIME, input={})
    first = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=request,
        actor_context=_actor(instance),
    )
    second = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=request,
        actor_context=_actor(instance).model_copy(
            update={"operation_id": "random-retry-operation"}
        ),
    )

    assert first == second
    assert first.run_id.startswith("RUN-") and len(first.run_id) == 68
    assert first.status == "succeeded"
    assert first.procedure_artifact_digest == procedure_artifact_digest(procedure).tagged
    assert first.result is not None
    assert len(first.result["rows"]) == 2  # type: ignore[index]
    assert first.receipt_digest is not None
    assert isinstance(first.receipt, ProcedureRunReceiptV3)
    assert first.receipt.status == "succeeded"
    assert first.receipt.terminal is None
    assert first.receipt.budget.declared.budget == procedure.definition.budget
    assert first.receipt.budget.declared.hard_caps == procedure.definition.hard_caps
    assert first.receipt.budget.observed.max_items.high_water == 0
    assert first.receipt.budget.observed.max_items.boundary is None
    assert first.receipt.budget.observed.result_bytes.high_water > 0
    assert first.receipt.budget.observed.result_bytes.boundary == "procedure-return"
    assert first.attribution is not None
    assert first.attribution.operation_id == "served-procedure-test"
    assert first.semantic_replay_key_digest is not None
    assert first.semantic_result_digest is not None
    assert service_get_playbill_procedure_run(instance, run_id=first.run_id) == first


def test_direct_receipt_reducer_identity_is_stable() -> None:
    first = DirectProcedureReceiptReducer()
    second = DirectProcedureReceiptReducer()

    assert first.reducer_digest == second.reducer_digest
    assert QUERY_NAME


def test_explicit_historical_coordinate_uses_read_only_replay_lane(tmp_path: Path) -> None:
    instance, owner, procedure = _world(tmp_path)
    historical = instance.accepted_coordinate()
    successor = procedure.model_copy(
        update={
            "lifecycle": procedure.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(procedure).tagged}
            )
        }
    )
    _activate_procedure(
        instance,
        owner,
        successor,
        sequence=5,
        timestamp="2026-08-24T17:00:00.000000Z",
    )

    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(
            at=AcceptedCoordinate.from_internal(historical),
            input={},
        ),
        actor_context=_actor(instance),
    )

    assert run.status == "succeeded"
    assert run.lane == "replay"
    assert run.bound_coordinate.git_oid == historical.git_oid
    assert run.head_at_admission.git_oid == instance.accepted_coordinate().git_oid
    assert run.evaluation_time.isoformat() == "2026-08-24T15:00:00+00:00"


def test_explicit_at_equal_to_head_still_selects_replay_lane(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    head = instance.accepted_coordinate()

    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(
            at=AcceptedCoordinate.from_internal(head),
            input={},
        ),
        actor_context=_actor(instance),
    )

    assert run.status == "succeeded"
    assert run.lane == "replay"
    assert run.bound_coordinate.git_oid == head.git_oid
    assert run.head_at_admission.git_oid == head.git_oid


@pytest.mark.parametrize(
    "failure_code",
    ("cas_unavailable_at_replay", "replay_material_mismatch"),
)
def test_retained_material_failures_reach_the_served_typed_terminal(
    tmp_path: Path,
    monkeypatch,
    failure_code: str,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    original_prepare = procedure_run_service.prepare_direct_procedure_run

    def break_retained_material(*args, **kwargs):  # type: ignore[no-untyped-def]
        prepared = original_prepare(*args, **kwargs)
        material = prepared.accepted_state_materials[0]
        bodies = kwargs["bodies"]
        if failure_code == "cas_unavailable_at_replay":
            assert bodies.erase(material.input.material_body_digest)
            return prepared
        other = bodies.store(canonical_bytes({"different": True}))
        mismatched_input = material.input.model_copy(update={"material_body_digest": other.digest})
        return prepared.model_copy(
            update={
                "admission": prepared.admission.model_copy(
                    update={"accepted_state_inputs": (mismatched_input,)}
                ),
                "accepted_state_materials": (
                    material.model_copy(update={"input": mismatched_input}),
                ),
            }
        )

    monkeypatch.setattr(
        procedure_run_service,
        "prepare_direct_procedure_run",
        break_retained_material,
    )

    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(input={}),
        actor_context=_actor(instance),
    )

    assert run.status == "operational_failed"
    assert isinstance(run.terminal, ProcedureOperationalFailureV1)
    assert run.terminal.code == failure_code


def test_served_compute_pipeline_replays_byte_identically_at_pinned_coordinate(
    tmp_path: Path,
) -> None:
    instance, owner, procedure = _world(tmp_path)
    read = procedure.definition.nodes[0]
    project = procedure.definition.nodes[-1]
    assert isinstance(read, StateTapNodeV3)
    filter_in = _contract(
        "pipeline-filter-in",
        {"items": PropertySchema(type="json"), "where": PropertySchema(type="json")},
    )
    filter_out = _contract(
        "pipeline-filter-out",
        {
            "items": PropertySchema(type="json"),
            "input_count": PropertySchema(type="int"),
            "output_count": PropertySchema(type="int"),
        },
    )
    aggregate_in = _contract(
        "pipeline-aggregate-in",
        {"items": PropertySchema(type="json")},
    )
    aggregate_out = _contract(
        "pipeline-aggregate-out",
        {"count": PropertySchema(type="int")},
    )

    def owned_pin(role: str, contract):  # type: ignore[no-untyped-def]
        return ArtifactPin(
            role=role,
            target=contract.identity,
            artifact_digest=procedure_owned_contract_digest(contract).tagged,
        )

    filter_in_pin = owned_pin("contract-in", filter_in)
    filter_out_pin = owned_pin("contract-out", filter_out)
    aggregate_in_pin = owned_pin("contract-in", aggregate_in)
    aggregate_out_pin = owned_pin("contract-out", aggregate_out)
    nodes = (
        read.model_copy(update={"next": "filter"}),
        TransformNodeV3(
            node_id="filter",
            transform_kind="filter_items",
            contract_in=filter_in_pin,
            contract_out=filter_out_pin,
            spec={
                "tag": "playbill-transform-filter-items-spec-v1",
                "items": "$steps.query.rows",
                "where": {},
            },
            as_="filtered",
            next="aggregate",
        ),
        TransformNodeV3(
            node_id="aggregate",
            transform_kind="aggregate_items",
            contract_in=aggregate_in_pin,
            contract_out=aggregate_out_pin,
            spec={
                "tag": "playbill-transform-aggregate-items-spec-v1",
                "items": "$steps.filtered.items",
            },
            as_="counted",
            next="gate",
        ),
        GuardNodeV3(
            node_id="gate",
            predicate=GuardPredicateV1(
                left=PredicateOperandV1(kind="step", alias="counted", path=("count",)),
                operator="gt",
                right=PredicateOperandV1(kind="literal", value=0),
            ),
            on_true="project",
            refusal_code="query.empty",
            message="The query returned no rows.",
        ),
        project.model_copy(update={"fields": {"rows": "$steps.filtered.items"}}),
    )
    definition = procedure.definition.model_copy(
        update={
            "nodes": nodes,
            "budget": procedure.definition.budget.model_copy(update={"max_items": 1}),
        }
    )
    added_pins = (filter_in_pin, filter_out_pin, aggregate_in_pin, aggregate_out_pin)
    added_contracts = (filter_in, filter_out, aggregate_in, aggregate_out)
    pipeline = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "pins": tuple(
                sorted(
                    (*procedure.pins, *added_pins),
                    key=lambda pin: (
                        pin.role.encode(),
                        pin.target.qualified.encode(),
                        pin.artifact_digest.encode(),
                    ),
                )
            ),
            "owned_contracts": tuple(
                sorted(
                    (*procedure.owned_contracts, *added_contracts),
                    key=lambda contract: canonical_bytes(contract.model_dump(mode="json")),
                )
            ),
            "lifecycle": procedure.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(procedure).tagged}
            ),
        }
    )
    _activate_procedure(
        instance,
        owner,
        pipeline,
        sequence=5,
        timestamp="2026-08-24T17:00:00.000000Z",
    )
    pinned = instance.accepted_coordinate()
    request = ProcedureRunRequestV2(
        at=AcceptedCoordinate.from_internal(pinned),
        input={},
    )

    first = service_run_playbill_procedure(
        instance,
        name=pipeline.identity.name,
        request=request,
        actor_context=_actor(instance),
    )
    second = service_run_playbill_procedure(
        instance,
        name=pipeline.identity.name,
        request=request,
        actor_context=_actor(instance).model_copy(update={"operation_id": "pipeline-replay"}),
    )

    assert first.status == "succeeded"
    assert first.result is not None and len(first.result["rows"]) == 2  # type: ignore[index]
    assert isinstance(first.receipt, ProcedureRunReceiptV3)
    assert first.receipt.budget.observed.max_items.high_water == 0
    assert first.receipt.budget.observed.max_items.boundary is None
    assert first.model_dump_json() == second.model_dump_json()


def test_served_list_boundary_records_nonzero_v3_receipt_high_water(tmp_path: Path) -> None:
    instance, owner, procedure = _world(tmp_path)
    bounded = _list_output_successor(procedure, max_items=3)
    _activate_procedure(
        instance,
        owner,
        bounded,
        sequence=5,
        timestamp="2026-08-24T17:00:00.000000Z",
    )

    run = service_run_playbill_procedure(
        instance,
        name=bounded.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )

    assert run.status == "succeeded", run
    assert isinstance(run.receipt, ProcedureRunReceiptV3)
    assert run.receipt.budget.observed.max_items.high_water == 2
    assert run.receipt.budget.observed.max_items.boundary == ("contract-out:query-rows-list")
    assert run.receipt.budget.observed.max_items.field_path == "rows"


def test_served_list_boundary_refusal_remains_typed(tmp_path: Path) -> None:
    instance, owner, procedure = _world(tmp_path)
    bounded = _list_output_successor(procedure, max_items=1)
    _activate_procedure(
        instance,
        owner,
        bounded,
        sequence=5,
        timestamp="2026-08-24T17:00:00.000000Z",
    )

    run = service_run_playbill_procedure(
        instance,
        name=bounded.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )

    assert run.status == "node_refused"
    assert isinstance(run.terminal, ProcedureBudgetExhaustedV1)
    assert run.terminal.details.boundary == "contract-out:query-rows-list"
    assert run.terminal.details.field_path == "rows"


def test_old_final_payload_without_budget_reconstructs_a_v2_receipt(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )
    assert run.run_id is not None
    assert run.receipt is not None
    journal, _root = procedure_run_service._journal(instance)
    stream = procedure_run_service._stream(instance)
    records = journal.all_records(stream, run.receipt.partition_id)
    final_record = records[-1].record
    old_payload = parse_journal_payload(
        instance.body_store().read(
            final_record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert isinstance(old_payload, dict)
    assert old_payload.pop("budget")["tag"] == "playbill-procedure-run-budget-v1"
    ProcedureExhaustWriter(
        journal=journal,
        bodies=instance.body_store(),
        fencing_token=procedure_run_service.PROCEDURE_RUN_FENCING_TOKEN,
    ).append(
        stream=final_record.stream,
        partition_id=final_record.partition_id,
        event_kind="attempt_finalized",
        accepted_coordinate=final_record.accepted_coordinate,
        definition_digest=final_record.definition_digest,
        actor_context=final_record.actor_context,
        recorded_at=final_record.recorded_at,
        payload=old_payload,
        procedure_artifact_digest=final_record.procedure_artifact_digest,
        run_id=final_record.run_id,
        admission_binding_digest=final_record.admission_binding_digest,
        attempt=final_record.attempt,
    )

    reconstructed = service_get_playbill_procedure_run(instance, run_id=run.run_id)
    assert reconstructed.status == "succeeded"
    assert isinstance(reconstructed.receipt, ProcedureRunReceiptV2)
    assert not isinstance(reconstructed.receipt, ProcedureRunReceiptV3)


def test_binding_proposes_same_identity_successor_with_exact_query_pin(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    query = work_item_query()
    query_digest = query_definition_digest(query).tagged
    inspection = service_propose_playbill_query_definition(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="served-procedure-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    exact = _accepted_query_procedure(query_digest).procedure
    assert isinstance(exact, ProcedureArtifactV2)
    query_pin = next(pin for pin in exact.pins if pin.target.kind == "QueryDefinition")
    nodes = list(exact.definition.nodes)
    read = nodes[0]
    assert isinstance(read, StateTapNodeV3)
    nodes[0] = read.model_copy(update={"query": ProcedurePinSlotRefV1(slot_name="query")})
    definition = exact.definition.model_copy(
        update={
            "nodes": tuple(nodes),
            "pin_slots": (
                ProcedurePinSlotV1(
                    slot_name="query",
                    pin_role="query",
                    artifact_kind="QueryDefinition",
                    interface_digest=query_digest,
                ),
            ),
        }
    )
    abstract = exact.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "pins": tuple(pin for pin in exact.pins if pin != query_pin),
        }
    )
    _activate_procedure(
        instance,
        owner,
        abstract,
        sequence=4,
        timestamp="2026-08-24T15:00:00.000000Z",
    )

    blocked = service_run_playbill_procedure(
        instance,
        name=abstract.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )
    assert blocked.status == "admission_refused"
    assert isinstance(blocked.terminal, ProcedureAdmissionRefusalV1)
    assert blocked.terminal.code == "binding_required"

    result = service_bind_playbill_procedure(
        instance,
        name=abstract.identity.name,
        request=ProcedureBindRequestV1(
            bindings=(
                ProcedureSlotBindingRequestV1(
                    slot_name="query",
                    target=ProcedureBindingTargetV1(
                        kind="QueryDefinition",
                        name=QUERY_NAME,
                    ),
                ),
            )
        ),
        actor=AuthenticatedActor(actor_id="owner"),
        timestamp="2026-08-24T16:00:00.000000Z",
    )

    assert result.accepted_digest == procedure_artifact_digest(abstract).tagged
    assert result.accepted_readiness.state == "binding_required"
    assert result.accepted_readiness.procedure_identity == ArtifactIdentity(
        kind="Procedure", name=abstract.identity.name
    )
    assert result.pending is not None
    assert result.pending.proposal_id.startswith("sha256:")
    assert result.pending.pending_successor_digest != procedure_artifact_digest(abstract).tagged
    assert isinstance(query_pin, ArtifactPin)


def test_served_guard_runs_through_the_existing_executor(tmp_path: Path) -> None:
    instance, owner, procedure = _world(tmp_path)
    nodes = list(procedure.definition.nodes)
    read = nodes[0]
    assert isinstance(read, StateTapNodeV3)
    nodes[0] = read.model_copy(update={"next": "gate"})
    nodes.insert(
        1,
        GuardNodeV3(
            node_id="gate",
            predicate=GuardPredicateV1(
                left=PredicateOperandV1(kind="exists", alias="query"),
                operator="eq",
                right=PredicateOperandV1(kind="literal", value=True),
            ),
            on_true="project",
            refusal_code="query.empty",
            message="The query returned no result.",
        ),
    )
    definition = procedure.definition.model_copy(update={"nodes": tuple(nodes)})
    unsupported = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "lifecycle": procedure.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(procedure).tagged}
            ),
        }
    )
    _activate_procedure(
        instance,
        owner,
        unsupported,
        sequence=5,
        timestamp="2026-08-24T16:00:00.000000Z",
    )
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    assert not journal_root.exists()

    readiness = service_playbill_procedure_readiness(
        instance,
        name=unsupported.identity.name,
        request=ProcedureReadinessRequestV1(evaluation_time=READ_TIME),
    )
    run = service_run_playbill_procedure(
        instance,
        name=unsupported.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )

    assert readiness.state == "ready"
    assert readiness.unsupported_nodes == ()
    assert run.status == "succeeded"
    assert run.terminal is None
    assert run.next_operation.kind == "done"
    assert journal_root.exists()

    refusing_nodes = list(unsupported.definition.nodes)
    gate = refusing_nodes[1]
    assert isinstance(gate, GuardNodeV3)
    assert gate.predicate.right is not None
    refusing_nodes[1] = gate.model_copy(
        update={
            "predicate": gate.predicate.model_copy(
                update={"right": gate.predicate.right.model_copy(update={"value": False})}
            )
        }
    )
    refusing_definition = unsupported.definition.model_copy(update={"nodes": tuple(refusing_nodes)})
    refusing = unsupported.model_copy(
        update={
            "definition": refusing_definition,
            "definition_digest": compute_procedure_definition_digest_v3(refusing_definition).tagged,
            "lifecycle": unsupported.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(unsupported).tagged}
            ),
        }
    )
    _activate_procedure(
        instance,
        owner,
        refusing,
        sequence=6,
        timestamp="2026-08-24T17:00:00.000000Z",
    )

    refused = service_run_playbill_procedure(
        instance,
        name=refusing.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance).model_copy(update={"operation_id": "guard-refusal"}),
    )

    assert refused.status == "node_refused"
    assert isinstance(refused.terminal, ProcedureNodeRefusalV1)
    assert refused.terminal.code == "guard_refused"
    assert refused.terminal.detail_code == "query.empty"
    assert refused.terminal.node_id == "gate"
    assert refused.terminal.journal_coordinate is not None
    assert isinstance(refused.receipt, ProcedureRunReceiptV3)
    assert refused.receipt.status == "node_refused"
    assert refused.receipt.terminal == refused.terminal
    assert refused.receipt.budget.observed.result_bytes.high_water == 0

    halting_nodes = list(unsupported.definition.nodes)
    halting_gate = halting_nodes[1]
    assert isinstance(halting_gate, GuardNodeV3)
    assert halting_gate.predicate.right is not None
    halting_nodes[1] = halting_gate.model_copy(
        update={
            "predicate": halting_gate.predicate.model_copy(
                update={"right": halting_gate.predicate.right.model_copy(update={"value": False})}
            ),
            "on_false": "stop",
        }
    )
    halting_nodes.insert(2, HaltNodeV3(node_id="stop", reason="No matching rows."))
    halting_definition = unsupported.definition.model_copy(update={"nodes": tuple(halting_nodes)})
    halting = unsupported.model_copy(
        update={
            "definition": halting_definition,
            "definition_digest": compute_procedure_definition_digest_v3(halting_definition).tagged,
            "lifecycle": refusing.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(refusing).tagged}
            ),
        }
    )
    _activate_procedure(
        instance,
        owner,
        halting,
        sequence=7,
        timestamp="2026-08-24T18:00:00.000000Z",
    )

    halted = service_run_playbill_procedure(
        instance,
        name=halting.identity.name,
        request=ProcedureRunRequestV2(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance).model_copy(update={"operation_id": "guard-halt"}),
    )

    assert halted.status == "halted"
    assert halted.result is None
    assert halted.next_operation.kind == "terminal"
    assert isinstance(halted.terminal, ProcedureHaltTerminalV1)
    assert halted.terminal.node_id == "stop"
    assert halted.terminal.reason == "No matching rows."
    assert isinstance(halted.receipt, ProcedureRunReceiptV3)
    assert halted.receipt.status == "halted"
    assert halted.receipt.terminal == halted.terminal


def test_unsupported_source_refuses_before_opening_a_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance, _owner, procedure = _world(tmp_path)
    capture = ArtifactPin(
        role="capture-contract",
        target=ArtifactIdentity(kind="CaptureContract", name="unsupported-source"),
        artifact_digest="sha256:" + "7" * 64,
    )
    provider = ArtifactPin(
        role="provider",
        target=ArtifactIdentity(kind="Provider", name="unsupported-source"),
        artifact_digest="sha256:" + "8" * 64,
    )
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                SourceNodeV3(
                    node_id="source",
                    capture_contract=capture,
                    provider=provider,
                    request={},
                    as_="result",
                ),
            ),
            "returns": "result",
        }
    )
    unsupported = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "pins": tuple(
                sorted(
                    (*procedure.pins, capture, provider),
                    key=lambda pin: (
                        pin.role.encode(),
                        pin.target.qualified.encode(),
                        pin.artifact_digest.encode(),
                    ),
                )
            ),
        }
    )
    accepted = AcceptedProcedureV1(
        path=procedure_path(unsupported.identity.name),
        procedure=unsupported,
        artifact_digest=procedure_artifact_digest(unsupported).tagged,
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_accepted_procedure",
        lambda *_args, **_kwargs: accepted,
    )
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    assert not journal_root.exists()

    result = service_run_playbill_procedure(
        instance,
        name=unsupported.identity.name,
        request=ProcedureRunRequestV2(input={}),
        actor_context=_actor(instance),
    )

    assert result.status == "admission_refused"
    assert isinstance(result.terminal, ProcedureAdmissionRefusalV1)
    assert result.terminal.code == "unsupported_node"
    assert not journal_root.exists()
