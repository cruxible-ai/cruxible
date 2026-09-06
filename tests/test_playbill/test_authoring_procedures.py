"""Ergonomic Procedure draft lowering onto the existing graph-v3 artifact."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.examples import procedure_example, query_claims_by_type_example
from cruxible_client.contracts.approval_policy import ApprovalPolicyV1
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayloadV1,
    ChangeSetAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    ProcedureRuntimePolicyAuthoringPayloadV1,
    QueryDefinitionAuthoringPayloadV1,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureOwnedContractV1,
    parse_procedure,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureDefinitionV4,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    StateTapNodeV3,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureHaltTerminalV1,
    ProcedureRunReceiptV3,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    query_definition_digest,
)
from cruxible_client.contracts.query.grammar import QueryProjectionV1
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.authoring import lowering
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.preflight import compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_procedure_runs import (
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
    service_playbill_procedure_readiness,
    service_run_playbill_procedure,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_resolution_contracts import _accept_tree

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-authoring-test-v1", {"label": label}).tagged


def _slot_definition() -> ProcedureDefinitionV3:
    contract_in = ProcedurePinSlotRefV1(slot_name="contract-in")
    contract_out = ProcedurePinSlotRefV1(slot_name="contract-out")
    query = ProcedurePinSlotRefV1(slot_name="query")
    return ProcedureDefinitionV3(
        name="triage",
        description="Read accepted claims and shape a bounded result.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(node_id="read", query=query, parameters={}, as_="rows"),
            ProjectNodeV3(
                node_id="shape",
                fields={"rows": "$steps.rows"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(
            ProcedurePinSlotV1(
                slot_name="contract-in",
                pin_role="contract-in",
                artifact_kind="Contract",
                interface_digest=_digest("contract-in-interface"),
            ),
            ProcedurePinSlotV1(
                slot_name="contract-out",
                pin_role="contract-out",
                artifact_kind="Contract",
                interface_digest=_digest("contract-out-interface"),
            ),
            ProcedurePinSlotV1(
                slot_name="query",
                pin_role="query",
                artifact_kind="QueryDefinition",
                interface_digest=_digest("query-interface"),
            ),
        ),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )


def _layout_slot_definition(*, halt_before_return: bool) -> ProcedureDefinitionV3:
    base = _slot_definition()
    query = ProcedurePinSlotRefV1(slot_name="query")
    contract_out = ProcedurePinSlotRefV1(slot_name="contract-out")
    read = StateTapNodeV3(
        node_id="read",
        query=query,
        parameters={},
        as_="rows",
        next="gate",
    )
    gate = GuardNodeV3(
        node_id="gate",
        predicate=GuardPredicateV1(
            left=PredicateOperandV1(kind="count", alias="rows"),
            operator="gt",
            right=PredicateOperandV1(kind="literal", value=0),
        ),
        on_true="result",
        on_false="stop",
        refusal_code="rows.empty",
        message="No rows are available.",
    )
    result = ProjectNodeV3(
        node_id="result",
        fields={"rows": "$steps.rows"},
        contract_out=contract_out,
        as_="result",
    )
    stop = HaltNodeV3(node_id="stop", reason="No rows are available.")
    tail = (stop, result) if halt_before_return else (result, stop)
    return base.model_copy(update={"nodes": (read, gate, *tail)})


def _coordinator(tmp_path: Path) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    instance, _owner = initialize_local(tmp_path)
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: "3" * 32,
    )
    return AuthoringIntentCoordinator(instance=instance, store=store), AuthenticatedActor(
        actor_id="owner"
    )


def _payload(definition: dict[str, object]) -> ProcedureAuthoringPayloadV1:
    return ProcedureAuthoringPayloadV1(
        definition=definition,
        activation_policy="drain",
    )


def _carried_contract(name: str, field: PropertySchema) -> ProcedureOwnedContractV1:
    return ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=ContractSchema(fields={} if name == "empty-input" else {"rows": field}),
    )


def _carried_definition() -> dict[str, object]:
    return {
        "graph_format": 3,
        "name": "bounded-projection",
        "contract_in": {
            "kind": "carried_contract",
            "name": "empty-input",
            "role": "contract-in",
        },
        "contract_out": {
            "kind": "carried_contract",
            "name": "rows-output",
            "role": "contract-out",
        },
        "nodes": [
            {
                "kind": "project",
                "node_id": "project",
                "fields": {"rows": []},
                "contract_out": {
                    "kind": "carried_contract",
                    "name": "rows-output",
                    "role": "contract-out",
                },
                "as": "result",
            }
        ],
        "returns": "result",
        "budget": {
            "wall_clock": {"microseconds": 1_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            "max_items": 10,
        },
        "hard_caps": {
            "max_wall_clock": {"microseconds": 2_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            "max_items": 20,
            "max_repeat_attempts": 1,
        },
        "terminal_capability": 1,
    }


def _change_set_query() -> QueryDefinitionV1:
    example = query_claims_by_type_example().query_definition
    assert example.projection is not None
    return example.model_copy(
        update={
            "pins": (),
            "projection": QueryProjectionV1(fields=(example.projection.fields[0],)),
        }
    )


def _change_set_payload(query: QueryDefinitionV1) -> ChangeSetAuthoringPayloadV1:
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["nodes"][0]["query"] = {  # type: ignore[index]
        "tag": "playbill-authoring-candidate-reference-v1",
        "role": "query",
        "target": query.identity.model_dump(mode="json"),
        "resolution": "candidate_in_change_set",
    }
    definition["pin_slots"] = [
        slot
        for slot in definition["pin_slots"]
        if slot["slot_name"] != "query"  # type: ignore[index]
    ]
    return ChangeSetAuthoringPayloadV1(
        members=(
            ProcedureAuthoringPayloadV1(
                definition=definition,
                activation_policy="drain",
            ),
            QueryDefinitionAuthoringPayloadV1(query_definition=query),
        )
    )


def _with_accepted_query_reference(
    payload: ChangeSetAuthoringPayloadV1,
) -> ChangeSetAuthoringPayloadV1:
    procedure = payload.members[0]
    query = payload.members[1]
    assert isinstance(procedure, ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2)
    assert isinstance(query, QueryDefinitionAuthoringPayloadV1)
    definition = dict(procedure.definition)
    nodes = [dict(node) for node in definition["nodes"]]  # type: ignore[arg-type]
    nodes[0]["query"] = {
        "tag": "playbill-authoring-artifact-reference-v1",
        "role": "query",
        "target": query.query_definition.identity.model_dump(mode="json"),
        "resolution": "accepted_at_intent_base",
    }
    definition["nodes"] = nodes
    return payload.model_copy(
        update={
            "members": (
                procedure.model_copy(update={"definition": definition}),
                query,
            )
        }
    )


def _runnable_change_set_payload(
    query: QueryDefinitionV1,
    *,
    description: str,
) -> ChangeSetAuthoringPayloadV1:
    definition: dict[str, object] = {
        "graph_format": 3,
        "name": "candidate-query-run",
        "description": description,
        "contract_in": {
            "kind": "carried_contract",
            "name": "empty-input",
            "role": "contract-in",
        },
        "contract_out": {
            "kind": "carried_contract",
            "name": "query-output",
            "role": "contract-out",
        },
        "nodes": [
            {
                "kind": "state_tap",
                "node_id": "read",
                "query": {
                    "tag": "playbill-authoring-candidate-reference-v1",
                    "role": "query",
                    "target": query.identity.model_dump(mode="json"),
                    "resolution": "candidate_in_change_set",
                },
                "parameters": {},
                "as": "rows",
            },
            {
                "kind": "project",
                "node_id": "result",
                "fields": {"rows": "$steps.rows"},
                "contract_out": {
                    "kind": "carried_contract",
                    "name": "query-output",
                    "role": "contract-out",
                },
                "as": "result",
            },
        ],
        "returns": "result",
        "budget": {
            "wall_clock": {"microseconds": 1_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
        },
        "hard_caps": {
            "max_wall_clock": {"microseconds": 2_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_repeat_attempts": 1,
        },
        "terminal_capability": 1,
    }
    return ChangeSetAuthoringPayloadV1(
        members=(
            ProcedureAuthoringPayloadV2(
                definition=definition,
                activation_policy="snapshot",
                owned_contracts=(
                    _carried_contract("empty-input", PropertySchema(type="json")),
                    _carried_contract("query-output", PropertySchema(type="json")),
                ),
            ),
            QueryDefinitionAuthoringPayloadV1(query_definition=query),
        )
    )


def _submit_approve_activate(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    owner: GeneratedKeyMaterial,
    payload: ChangeSetAuthoringPayloadV1,
    timestamp: str,
) -> None:
    created = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=timestamp,
    ).intent
    preflight = compute_preflight(coordinator.instance, intent=created, actor=actor)
    assert preflight.result.verdict == "passed", preflight.result.frontier
    submitted = coordinator.submit(created.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        coordinator.instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        coordinator.instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        coordinator.instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _world_holding_a_document(
    tmp_path: Path,
) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    """One instance whose accepted tree governs a single Document."""

    instance, owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Accepted design\n")
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Accepted design",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            "documents/design.json": render_document(shell),
        },
        timestamp=TIMESTAMP,
        proposal_name="seed-governed-document",
    )
    instance.refresh()
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: "4" * 32,
    )
    return (
        AuthoringIntentCoordinator(instance=instance, store=store),
        AuthenticatedActor(actor_id="owner"),
    )


def _list_contract_payload() -> ProcedureAuthoringPayloadV2:
    return ProcedureAuthoringPayloadV2(
        definition=_carried_definition(),
        activation_policy="snapshot",
        owned_contracts=(
            _carried_contract("empty-input", PropertySchema(type="json")),
            _carried_contract(
                "rows-output",
                PropertySchema(type="list", item_fields={"id": PropertySchema(type="string")}),
            ),
        ),
    )


def test_a_procedure_can_be_authored_in_a_world_that_governs_a_document(
    tmp_path: Path,
) -> None:
    """Procedure lowering reads the accepted tree, so it must read all of it.

    Resolving a Procedure's references parses every accepted artifact, and a
    Document is registered by its body's DIGEST alone -- parsing one asks the
    managed CAS for that body's metadata. Lowering asked without the resolver,
    so the parse refused on the first `documents/*.json` it met and every
    Procedure payload died with an untyped fault: an instance governing a
    single Document could not author a Procedure at all.
    """

    coordinator, actor = _world_holding_a_document(tmp_path)

    compiled = coordinator.compile(
        actor=actor,
        payload=_list_contract_payload(),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )

    assert compiled.verdict == "passed", [item.code for item in compiled.frontier.diagnostics]
    assert compiled.frontier.diagnostics == ()


def test_an_unreadable_accepted_tree_refuses_typed_rather_than_faulting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The author asked a question and is owed an answer with a repair.

    A tree this instance cannot parse is the instance's fault, not the
    payload's, but it reached the author as a bare internal error whose only
    diagnosis was the daemon log.
    """

    coordinator, actor = _world_holding_a_document(tmp_path)

    def unreadable(*_args: object, **_values: object) -> object:
        raise ProjectionFormatError("Document body is unavailable during projection")

    monkeypatch.setattr(lowering, "parse_projection_tree", unreadable)

    compiled = coordinator.compile(
        actor=actor,
        payload=_list_contract_payload(),
        canonical_timestamp="2026-08-21T12:03:00.000000Z",
    )

    assert compiled.verdict == "refused"
    (diagnostic,) = compiled.frontier.diagnostics
    assert diagnostic.code == "playbill.authoring.lowering_invalid"
    assert diagnostic.offending_element == "definition"
    assert diagnostic.repairs[0].kind == "restore_accepted_projection"


def test_max_items_requires_a_referenced_list_contract(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    opaque = ProcedureAuthoringPayloadV2(
        definition=_carried_definition(),
        activation_policy="snapshot",
        owned_contracts=(
            _carried_contract("empty-input", PropertySchema(type="json")),
            _carried_contract("rows-output", PropertySchema(type="json")),
        ),
    )
    refused = coordinator.compile(
        actor=actor,
        payload=opaque,
        canonical_timestamp=TIMESTAMP,
    )
    assert refused.verdict == "refused"
    diagnostic = refused.frontier.diagnostics[0]
    assert diagnostic.code == ("playbill.authoring.procedure_definition_invalid")
    assert diagnostic.offending_element == "definition.budget.max_items"
    assert "declare a list field" in diagnostic.repairs[0].description.lower()

    supported = opaque.model_copy(
        update={
            "owned_contracts": (
                _carried_contract("empty-input", PropertySchema(type="json")),
                _carried_contract(
                    "rows-output",
                    PropertySchema(
                        type="list",
                        item_fields={"id": PropertySchema(type="string")},
                    ),
                ),
            )
        }
    )
    supported_root = tmp_path / "supported"
    supported_root.mkdir()
    supported_coordinator, supported_actor = _coordinator(supported_root)
    passed = supported_coordinator.compile(
        actor=supported_actor,
        payload=supported,
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert passed.verdict == "passed"


def test_served_procedure_example_reaches_all_six_typed_specs(tmp_path: Path) -> None:
    example = procedure_example()
    tags = {
        node["spec"]["tag"]
        for node in example.definition["nodes"]
        if isinstance(node, dict) and isinstance(node.get("spec"), dict)
    }
    assert tags == {
        "playbill-transform-adapter-spec-v1",
        "playbill-transform-aggregate-items-spec-v1",
        "playbill-transform-dedupe-items-spec-v1",
        "playbill-transform-filter-items-spec-v1",
        "playbill-transform-join-items-spec-v1",
        "playbill-transform-shape-items-spec-v1",
    }

    coordinator, actor = _coordinator(tmp_path)
    result = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(example, tree={}),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "passed"


def test_change_set_successor_resolves_candidate_query_to_exact_new_digest(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    tokens = iter(f"{value:032x}" for value in range(1, 20))
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: next(tokens),
        ),
    )
    actor = AuthenticatedActor(actor_id="owner")
    query = _change_set_query()
    accepted_spelling_for_new_candidate = coordinator.compile(
        actor=actor,
        payload=_with_accepted_query_reference(_change_set_payload(query)),
        canonical_timestamp=TIMESTAMP,
    )
    assert accepted_spelling_for_new_candidate.verdict == "refused"
    assert accepted_spelling_for_new_candidate.frontier.diagnostics[0].code == (
        "playbill.authoring.artifact_reference_unresolved"
    )
    first = coordinator.compile(
        actor=actor,
        payload=_change_set_payload(query),
        canonical_timestamp=TIMESTAMP,
    )
    assert first.verdict == "passed", first.frontier
    initial_intent = coordinator.list_pending(actor=actor).intents[-1]
    initial = compute_preflight(
        coordinator.instance,
        intent=initial_intent,
        actor=actor,
    ).lowered
    assert initial is not None
    resolved_identities = [member["identity"] for member in initial.resolved_authoring["members"]]
    assert resolved_identities == sorted(resolved_identities, key=lambda item: item.encode())
    assert all(isinstance(identity, str) for identity in resolved_identities)
    _accept_tree(
        coordinator.instance,
        owner,
        initial.proposed_tree,
        timestamp=TIMESTAMP,
        proposal_name="initial-query-and-procedure",
    )

    successor_query = query.model_copy(
        update={
            "description": "A revised governed query.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    successor_tokens = iter(f"{value:032x}" for value in range(20, 40))
    successor = AuthoringIntentCoordinator(
        instance=coordinator.instance,
        store=AuthoringIntentStore(
            coordinator.instance.root / coordinator.instance.descriptor.storage.exhaust,
            token_factory=lambda: next(successor_tokens),
        ),
    )
    compiled = successor.compile(
        actor=actor,
        payload=_change_set_payload(successor_query),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert compiled.verdict == "passed", compiled.frontier
    intent = successor.list_pending(actor=actor).intents[-1]
    lowered = compute_preflight(
        coordinator.instance,
        intent=intent,
        actor=actor,
    ).lowered
    assert lowered is not None
    accepted_procedure = parse_procedure(
        lowered.proposed_tree[procedure_path("triage")],
        path=procedure_path("triage"),
    )
    query_pin = next(pin for pin in accepted_procedure.pins if pin.role == "query")
    assert query_pin.artifact_digest == query_definition_digest(successor_query).tagged

    accepted_payload = _with_accepted_query_reference(_change_set_payload(successor_query))
    accepted_payload_procedure = accepted_payload.members[0]
    assert isinstance(accepted_payload_procedure, ProcedureAuthoringPayloadV1)
    accepted_definition = dict(accepted_payload_procedure.definition)
    accepted_definition["description"] = "A semantic Procedure revision using the base query."
    accepted_payload = accepted_payload.model_copy(
        update={
            "members": (
                accepted_payload_procedure.model_copy(update={"definition": accepted_definition}),
                accepted_payload.members[1],
            )
        }
    )
    accepted_spelling = successor.compile(
        actor=actor,
        payload=accepted_payload,
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert accepted_spelling.verdict == "passed", accepted_spelling.frontier
    accepted_intent = successor.list_pending(actor=actor).intents[-1]
    accepted_lowered = compute_preflight(
        coordinator.instance,
        intent=accepted_intent,
        actor=actor,
    ).lowered
    assert accepted_lowered is not None
    accepted_spelling_procedure = parse_procedure(
        accepted_lowered.proposed_tree[procedure_path("triage")],
        path=procedure_path("triage"),
    )
    accepted_query_pin = next(
        pin for pin in accepted_spelling_procedure.pins if pin.role == "query"
    )
    assert accepted_query_pin.artifact_digest == query_definition_digest(successor_query).tagged


def test_change_set_submit_activate_closure_and_run_read_exact_successor_query(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    query_v1 = _change_set_query()
    _submit_approve_activate(
        coordinator,
        actor=actor,
        owner=owner,
        payload=_runnable_change_set_payload(query_v1, description="Read query v1."),
        timestamp=TIMESTAMP,
    )
    accepted_v1 = parse_procedure(
        instance.tree_at(instance.accepted_coordinate().git_oid)[
            procedure_path("candidate-query-run")
        ],
        path=procedure_path("candidate-query-run"),
    )
    query_v1_pin = next(pin for pin in accepted_v1.pins if pin.role == "query")
    assert query_v1_pin.artifact_digest == query_definition_digest(query_v1).tagged

    query_v2 = query_v1.model_copy(
        update={
            "description": "The accepted query v2.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query_v1).tagged
            ),
        }
    )
    incomplete = coordinator.compile(
        actor=actor,
        payload=QueryDefinitionAuthoringPayloadV1(query_definition=query_v2),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert incomplete.verdict == "refused"
    assert "playbill.change_set.incomplete_closure" in {
        item.code for item in incomplete.frontier.diagnostics
    }

    successor_payload = _with_accepted_query_reference(
        _runnable_change_set_payload(query_v2, description="Read query v2.")
    )
    _submit_approve_activate(
        coordinator,
        actor=actor,
        owner=owner,
        payload=successor_payload,
        timestamp="2026-08-21T12:02:00.000000Z",
    )
    query_v2_digest = query_definition_digest(query_v2).tagged
    accepted_v2 = parse_procedure(
        instance.tree_at(instance.accepted_coordinate().git_oid)[
            procedure_path("candidate-query-run")
        ],
        path=procedure_path("candidate-query-run"),
    )
    query_v2_pin = next(pin for pin in accepted_v2.pins if pin.role == "query")
    assert query_v2_pin.artifact_digest == query_v2_digest

    evaluation_time = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
    readiness = service_playbill_procedure_readiness(
        instance,
        name="candidate-query-run",
        request=ProcedureReadinessRequestV1(evaluation_time=evaluation_time),
    )
    assert readiness.state == "ready"
    assert readiness.definition_digest == accepted_v2.definition_digest
    result = service_run_playbill_procedure(
        instance,
        name="candidate-query-run",
        request=ProcedureRunRequestV2(evaluation_time=evaluation_time, input={}),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=instance.descriptor.instance_id,
            operation_id="candidate-query-v2-run",
            timestamp=evaluation_time,
        ),
    )

    assert result.status == "succeeded", result
    assert isinstance(result.receipt, ProcedureRunReceiptV3)
    validated_query = next(pin for pin in result.receipt.validated_pins if pin.role == "query")
    assert validated_query.artifact_digest == query_v2_digest
    assert [item["query_definition_digest"] for item in result.receipt.admitted_inputs] == [
        query_v2_digest
    ]


def test_approval_policy_is_a_real_singleton_authoring_scope(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    policy_payload = ApprovalPolicyAuthoringPayloadV1(
        approval_policy=ApprovalPolicyV1(mode="independent_approval_required")
    )
    singleton = coordinator.compile(
        actor=actor,
        payload=policy_payload,
        canonical_timestamp=TIMESTAMP,
    )
    assert singleton.verdict == "passed", singleton.frontier

    mixed_coordinator = AuthoringIntentCoordinator(
        instance=coordinator.instance,
        store=AuthoringIntentStore(
            coordinator.instance.root / coordinator.instance.descriptor.storage.exhaust,
            token_factory=lambda: "4" * 32,
        ),
    )
    mixed = mixed_coordinator.compile(
        actor=actor,
        payload=ChangeSetAuthoringPayloadV1(
            members=(
                policy_payload,
                QueryDefinitionAuthoringPayloadV1(query_definition=_change_set_query()),
            )
        ),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert mixed.verdict == "refused"
    diagnostic = mixed.frontier.diagnostics[0]
    assert diagnostic.code == "playbill.authoring.approval_policy_singleton_required"
    assert diagnostic.offending_element == "members"
    assert diagnostic.repairs[0].kind == "split_change_set"


def test_procedure_runtime_policy_is_a_real_singleton_authoring_scope(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    policy_payload = ProcedureRuntimePolicyAuthoringPayloadV1(
        procedure_runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=2_097_152)
    )
    singleton = coordinator.compile(
        actor=actor,
        payload=policy_payload,
        canonical_timestamp=TIMESTAMP,
    )
    assert singleton.verdict == "passed", singleton.frontier

    mixed_coordinator = AuthoringIntentCoordinator(
        instance=coordinator.instance,
        store=AuthoringIntentStore(
            coordinator.instance.root / coordinator.instance.descriptor.storage.exhaust,
            token_factory=lambda: "5" * 32,
        ),
    )
    mixed = mixed_coordinator.compile(
        actor=actor,
        payload=ChangeSetAuthoringPayloadV1(
            members=(
                policy_payload,
                QueryDefinitionAuthoringPayloadV1(query_definition=_change_set_query()),
            )
        ),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert mixed.verdict == "refused"
    assert mixed.frontier.diagnostics[0].code == (
        "playbill.authoring.procedure_runtime_policy_singleton_required"
    )


def test_change_set_membership_and_candidate_reference_refusals(tmp_path: Path) -> None:
    query = _change_set_query()
    # One authoring intent is one changeset at every size. The two-member floor
    # this line once pinned made the uniform builder refuse the smallest real
    # set an author writes, so a one-member set is admitted and keeps its own
    # ChangeSet identity rather than collapsing into a singleton payload.
    single = ChangeSetAuthoringPayloadV1(
        members=(QueryDefinitionAuthoringPayloadV1(query_definition=query),)
    )
    assert len(single.members) == 1
    with pytest.raises(ValueError, match="sorted by semantic identity"):
        ChangeSetAuthoringPayloadV1(members=tuple(reversed(_change_set_payload(query).members)))

    coordinator, actor = _coordinator(tmp_path)
    created = coordinator.create(
        actor=actor,
        payload=_change_set_payload(query),
        canonical_timestamp=TIMESTAMP,
    )
    renamed_values = query.model_dump(mode="json")
    renamed_values["identity"] = {"kind": "QueryDefinition", "name": "renamed"}
    renamed = QueryDefinitionV1.model_validate(renamed_values)
    with pytest.raises(ValueError, match="cannot change member identity"):
        coordinator.replace_payload(
            created.intent.intent_id,
            actor=actor,
            payload=_change_set_payload(renamed),
        )

    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    coordinator, actor = _coordinator(outside_root)
    outside = _change_set_payload(query)
    procedure = outside.members[0]
    assert isinstance(procedure, ProcedureAuthoringPayloadV1)
    definition = dict(procedure.definition)
    nodes = [dict(node) for node in definition["nodes"]]  # type: ignore[arg-type]
    nodes[0]["query"] = {
        "tag": "playbill-authoring-candidate-reference-v1",
        "role": "query",
        "target": {"kind": "QueryDefinition", "name": "outside"},
        "resolution": "candidate_in_change_set",
    }
    definition["nodes"] = nodes
    refused = coordinator.compile(
        actor=actor,
        payload=outside.model_copy(
            update={
                "members": (
                    procedure.model_copy(update={"definition": definition}),
                    outside.members[1],
                )
            }
        ),
        canonical_timestamp=TIMESTAMP,
    )
    assert refused.verdict == "refused"
    assert refused.frontier.diagnostics[0].code == (
        "playbill.authoring.candidate_reference_outside_change_set"
    )


@pytest.mark.parametrize(
    ("reference", "expected_code"),
    (
        (
            {
                "tag": "playbill-authoring-candidate-reference-v1",
                "role": "procedure",
                "target": {"kind": "Procedure", "name": "other"},
                "resolution": "candidate_in_change_set",
            },
            "playbill.authoring.candidate_procedure_reference_forbidden",
        ),
        (
            {
                "tag": "playbill-authoring-candidate-reference-v1",
                "target": {"kind": "QueryDefinition", "name": "claims-by-type"},
                "resolution": "candidate_in_change_set",
            },
            "playbill.authoring.candidate_reference_invalid",
        ),
    ),
)
def test_change_set_candidate_reference_shape_refusals_are_reachable(
    tmp_path: Path,
    reference: dict[str, object],
    expected_code: str,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    payload = _change_set_payload(_change_set_query())
    procedure = payload.members[0]
    assert isinstance(procedure, ProcedureAuthoringPayloadV1)
    definition = dict(procedure.definition)
    definition["contract_in"] = reference
    refused = coordinator.compile(
        actor=actor,
        payload=payload.model_copy(
            update={
                "members": (
                    procedure.model_copy(update={"definition": definition}),
                    payload.members[1],
                )
            }
        ),
        canonical_timestamp=TIMESTAMP,
    )
    assert refused.verdict == "refused"
    assert refused.frontier.diagnostics[0].code == expected_code


def test_invalid_definition_message_excludes_pydantic_metadata(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["nodes"][0]["kind"] = "not-a-node-kind"  # type: ignore[index]

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "refused"
    message = result.frontier.diagnostics[0].message
    assert "definition.nodes[0]" in message
    assert "not-a-node-kind" in message
    assert "Input tag 'not-a-node-kind'" in message
    assert "input_value=" not in message
    assert "input_type=" not in message
    assert "errors.pydantic.dev" not in message


@pytest.mark.parametrize(
    ("invalid_graph", "expected_cause"),
    (
        (
            "nonreturn_leaf",
            "Procedure leaf 'unused' neither halts, emits typed egress, nor returns",
        ),
        (
            "terminal_guard",
            "Procedure guard 'gate' with omitted on_true must name a forward true target",
        ),
    ),
)
def test_graph_law_failures_use_typed_definition_refusal(
    tmp_path: Path,
    invalid_graph: str,
    expected_cause: str,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    contract_out = definition["contract_out"]
    if invalid_graph == "nonreturn_leaf":
        definition["nodes"] = [
            definition["nodes"][0],  # type: ignore[index]
            {
                "kind": "guard",
                "node_id": "gate",
                "predicate": {
                    "left": {"kind": "literal", "value": True},
                    "operator": "eq",
                    "right": {"kind": "literal", "value": True},
                },
                "on_true": "result",
                "on_false": "unused",
                "refusal_code": "guard.false",
                "message": "The guard refused.",
            },
            {
                "kind": "project",
                "node_id": "result",
                "fields": {"rows": "$steps.rows"},
                "contract_out": contract_out,
                "as": "result",
            },
            {
                "kind": "project",
                "node_id": "unused",
                "fields": {"rows": "$steps.rows"},
                "contract_out": contract_out,
                "as": "unused",
            },
        ]
    else:
        definition["nodes"] = [
            {
                "kind": "project",
                "node_id": "result",
                "fields": {"rows": []},
                "contract_out": contract_out,
                "as": "result",
            },
            {
                "kind": "guard",
                "node_id": "gate",
                "predicate": {
                    "left": {"kind": "literal", "value": True},
                    "operator": "eq",
                    "right": {"kind": "literal", "value": True},
                },
                "on_false": "$abort",
                "refusal_code": "guard.false",
                "message": "The guard refused.",
            },
        ]

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    diagnostic = result.frontier.diagnostics[0]
    assert diagnostic.code == "playbill.authoring.procedure_definition_invalid"
    assert diagnostic.stage == "lowering"
    assert diagnostic.offending_element == "definition"
    assert expected_cause in diagnostic.message
    assert diagnostic.repairs[0].kind == "replace_definition"
    assert diagnostic.repairs[0].description == ("Repair the indicated graph-v3 definition field.")
    assert "errors.pydantic.dev" not in diagnostic.message


def test_graph_v4_authoring_lowers_into_the_accepted_procedure_shape(tmp_path: Path) -> None:
    """A v4 definition lowers; the artifact shape and its digest domain follow it."""

    instance, _owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["graph_format"] = 4

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed"
    intent = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=intent, actor=actor).lowered
    assert lowered is not None
    assert lowered.resolved_authoring["definition"]["graph_format"] == 4
    parsed = parse_procedure(
        lowered.proposed_tree[procedure_path("triage")],
        path=procedure_path("triage"),
    )
    assert isinstance(parsed.definition, ProcedureDefinitionV4)
    assert (
        parsed.definition_digest == compute_procedure_definition_digest_v4(parsed.definition).tagged
    )


def test_invalid_graph_v4_authoring_names_its_own_generation(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["graph_format"] = 4
    definition["returns"] = "absent"

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    diagnostic = result.frontier.diagnostics[0]
    assert diagnostic.code == "playbill.authoring.procedure_definition_invalid"
    assert diagnostic.offending_element == "definition"
    assert "graph-v4" in diagnostic.message
    assert diagnostic.repairs[0].description == "Repair the indicated graph-v4 definition field."


def test_layout_only_procedure_successor_refuses_in_coordinator(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    definition = _layout_slot_definition(halt_before_return=False)
    first = coordinator.compile(
        actor=actor,
        payload=_payload(definition.model_dump(mode="json", by_alias=True)),
        canonical_timestamp=TIMESTAMP,
    )
    assert first.verdict == "passed"
    seed = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=seed, actor=actor).lowered
    assert lowered is not None
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp=TIMESTAMP,
        proposal_name="seed-layout-procedure",
    )
    reordered = _layout_slot_definition(halt_before_return=True)

    result = coordinator.compile(
        actor=actor,
        payload=_payload(reordered.model_dump(mode="json", by_alias=True)),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )

    assert result.verdict == "refused"
    assert result.frontier.diagnostics[0].code == "playbill.proposal.non_singleton_scope"
    assert result.frontier.diagnostics[0].message == (
        "The proposal changes no registered semantic member."
    )


def test_invalid_artifact_reference_message_excludes_pydantic_metadata(
    tmp_path: Path,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["contract_in"] = {
        "tag": "playbill-authoring-artifact-reference-v1",
        "target": {"kind": "Contract"},
    }

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    diagnostic = result.frontier.diagnostics[0]
    assert diagnostic.code == "playbill.authoring.artifact_reference_invalid"
    assert "definition.contract_in.role" in diagnostic.message
    assert "definition.contract_in.target.name" in diagnostic.message
    assert "offending element" in diagnostic.message
    assert "input_value=" not in diagnostic.message
    assert "input_type=" not in diagnostic.message
    assert "errors.pydantic.dev" not in diagnostic.message


def test_pin_slot_procedure_compiles_without_writer_managed_envelope_fields(
    tmp_path: Path,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition()

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition.model_dump(mode="json", by_alias=True)),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed"
    assert result.certificate.resolved_authoring_digest.startswith("sha256:")
    resumed = coordinator.list_pending(actor=actor).intents[0]
    assert resumed.semantic_identity == "Procedure:triage"
    assert resumed.candidate_status.state == "ready_to_submit"
    assert not any(
        key in resumed.payload.definition
        for key in {"definition_digest", "identity", "lifecycle", "path", "pins"}
    )


def test_caller_originated_exact_procedure_pin_is_typed_refusal(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)
    definition["contract_in"] = ArtifactPin(
        role="contract-in",
        target=ArtifactIdentity(kind="Contract", name="caller-chosen"),
        artifact_digest=_digest("caller-chosen"),
    ).model_dump(mode="json")

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.caller_artifact_digest_forbidden"
    )
    assert diagnostic.offending_element == "definition.contract_in"
    assert diagnostic.repairs[0].kind == "replace_reference"


def test_a_procedure_revision_submits_without_reaching_the_claim_revision_marker(
    tmp_path: Path,
) -> None:
    """A Procedure amend must not raise on the terminal success path.

    The submit and the store transition land before the result is built, so a
    raise there means the write happened and the call reported failure. The
    Claim revision marker used to run for every payload kind and refuse a
    Procedure identity.
    """
    instance, owner = initialize_local(tmp_path)
    minted = iter(("4" * 32, "5" * 32))
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: next(minted),
    )
    coordinator = AuthoringIntentCoordinator(instance=instance, store=store)
    actor = AuthenticatedActor(actor_id="owner")

    # An accepted Procedure, so the next authoring of the same name lowers with
    # a predecessor_digest -- the exact state the marker mishandled.
    definition = _slot_definition()
    first = coordinator.compile(
        actor=actor,
        payload=_payload(definition.model_dump(mode="json", by_alias=True)),
        canonical_timestamp=TIMESTAMP,
    )
    assert first.verdict == "passed"
    pending = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp=TIMESTAMP,
        proposal_name="seed-procedure",
    )

    revised = definition.model_copy(update={"description": "A revised triage."})
    compiled = coordinator.compile(
        actor=actor,
        payload=_payload(revised.model_dump(mode="json", by_alias=True)),
        canonical_timestamp=TIMESTAMP,
    )
    assert compiled.verdict == "passed", compiled.frontier

    intent_id = coordinator.list_pending(actor=actor).intents[-1].intent_id
    submitted = coordinator.submit(intent_id, actor=actor)

    assert submitted.tag == "playbill-authoring-submit-result-v1"
    assert submitted.intent.semantic_identity == "Procedure:triage"
    # The marker is a Claim concept; a Procedure carries the default.
    assert submitted.identity_stable is False
    assert submitted.claim_revision is None


def test_coordinator_authored_halt_reason_reaches_terminal_and_receipt(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    reason = "No eligible work remains."
    payload = ProcedureAuthoringPayloadV2(
        definition={
            "graph_format": 3,
            "name": "halt-with-reason",
            "contract_in": {
                "kind": "carried_contract",
                "name": "empty-input",
                "role": "contract-in",
            },
            "contract_out": {
                "kind": "carried_contract",
                "name": "halt-output",
                "role": "contract-out",
            },
            "nodes": [
                {
                    "kind": "guard",
                    "node_id": "gate",
                    "predicate": {
                        "left": {"kind": "literal", "value": False},
                        "operator": "eq",
                        "right": {"kind": "literal", "value": True},
                    },
                    "on_true": "result",
                    "on_false": "stop",
                    "refusal_code": "work.exhausted",
                    "message": reason,
                },
                {
                    "kind": "project",
                    "node_id": "result",
                    "fields": {},
                    "contract_out": {
                        "kind": "carried_contract",
                        "name": "halt-output",
                        "role": "contract-out",
                    },
                    "as": "result",
                },
                {"kind": "halt", "node_id": "stop", "reason": reason},
            ],
            "returns": "result",
            "budget": {
                "wall_clock": {"microseconds": 1_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
            },
            "hard_caps": {
                "max_wall_clock": {"microseconds": 2_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 1,
                "max_repeat_attempts": 1,
            },
            "terminal_capability": 1,
        },
        activation_policy="snapshot",
        owned_contracts=(
            _carried_contract("empty-input", PropertySchema(type="json")),
            _carried_contract("halt-output", PropertySchema(type="json")),
        ),
    )
    created = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    preflight = compute_preflight(instance, intent=created, actor=actor)
    assert preflight.result.verdict == "passed", preflight.result.frontier
    submitted = coordinator.submit(created.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )

    result = service_run_playbill_procedure(
        instance,
        name="halt-with-reason",
        request=ProcedureRunRequestV2(
            evaluation_time=datetime(2026, 8, 21, 12, 5, tzinfo=UTC),
            input={},
        ),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=instance.descriptor.instance_id,
            operation_id="coordinator-halt-reason",
            timestamp=datetime(2026, 8, 21, 12, 5, tzinfo=UTC),
        ),
    )

    assert result.status == "halted"
    assert isinstance(result.terminal, ProcedureHaltTerminalV1)
    assert result.terminal.reason == reason
    assert isinstance(result.receipt, ProcedureRunReceiptV3)
    assert result.receipt.terminal == result.terminal
