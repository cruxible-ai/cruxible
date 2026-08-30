"""PC-G5 served query-only Procedure readiness and run laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactV2,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    StateTapNodeV3,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureNodeRefusalV1,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_core.playbill.actor_context import GovernedActorContext
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
