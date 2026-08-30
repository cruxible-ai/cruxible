"""Ergonomic Procedure draft lowering onto the existing graph-v3 artifact."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.preflight import compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
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
    assert diagnostic.code == (
        "playbill.authoring.procedure_definition_invalid"
    )
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
