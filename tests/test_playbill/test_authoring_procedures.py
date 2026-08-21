"""Ergonomic Procedure draft lowering onto the existing graph-v3 artifact."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.models import ProcedureAuthoringPayloadV1
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.canonical import ArtifactDigest, typed_digest
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-21T12:00:00.000000Z"
AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))


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
        authority=AUTHORITY,
        activation_policy="drain",
    )


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
