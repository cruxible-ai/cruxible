"""Playbill-native Procedure artifact, graph-v3, and frozen-reader tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest_v3,
    compute_procedure_node_digests_v3,
)
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    StateTapNodeV3,
)


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _definition(
    *,
    query: ArtifactPin | ProcedurePinSlotRefV1 | None = None,
    nodes: tuple[object, ...] | None = None,
    terminal_capability: int = 1,
) -> ProcedureDefinitionV3:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    query = query or _pin("query", "QueryDefinition", "claims-by-status")
    default_nodes = (
        StateTapNodeV3(node_id="read", query=query, parameters={}, as_="rows"),
        ProjectNodeV3(
            node_id="shape",
            fields={"rows": "$steps.rows"},
            contract_out=contract_out,
            as_="result",
        ),
    )
    return ProcedureDefinitionV3(
        name="triage",
        description="Read accepted claims and shape a bounded result.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=default_nodes if nodes is None else nodes,  # type: ignore[arg-type]
        returns="result",
        pin_slots=(
            (
                ProcedurePinSlotV1(
                    slot_name="query",
                    pin_role="query",
                    artifact_kind="QueryDefinition",
                    interface_digest=_digest("query-interface"),
                ),
            )
            if isinstance(query, ProcedurePinSlotRefV1)
            else ()
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
        terminal_capability=terminal_capability,  # type: ignore[arg-type]
    )


def _artifact(definition: ProcedureDefinitionV3) -> ProcedureArtifactV1:
    pins = tuple(
        sorted(
            {
                pin
                for pin in (
                    definition.contract_in,
                    definition.contract_out,
                    getattr(definition.nodes[0], "query", None),
                )
                if isinstance(pin, ArtifactPin)
            },
            key=lambda pin: (
                pin.role.encode(),
                pin.target.qualified.encode(),
                pin.artifact_digest.encode(),
            ),
        )
    )
    return ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name="triage"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=pins,
        activation_policy="drain",
    )


def _layout_definition(*, halt_before_return: bool) -> ProcedureDefinitionV3:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    read = StateTapNodeV3(
        node_id="read",
        query=_pin("query", "QueryDefinition", "claims-by-status"),
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
    return _definition(nodes=(read, gate, *tail))


def test_procedure_v3_round_trip_digest_and_node_golden() -> None:
    definition = _definition()
    procedure = _artifact(definition)

    assert definition.graph_format == 3
    assert procedure.directly_runnable is True
    assert procedure.definition_digest == (
        "sha256:be3b104bf50e7f958bc468cf3ac089dfdf57a36982027569bbc63baf06086001"
    )
    nodes = compute_procedure_node_digests_v3(definition)
    assert nodes["read"].subtree_digest == (
        "sha256:a7c033a9af056822015078993714074fb569e074823d728c40e8fa84499a17d5"
    )

    content = render_procedure(procedure)
    assert parse_procedure(content, path=procedure_path("triage")) == procedure
    assert procedure_artifact_digest(procedure).tagged.startswith("sha256:")


def test_open_slot_procedure_is_acceptable_but_not_directly_runnable() -> None:
    definition = _definition(query=ProcedurePinSlotRefV1(slot_name="query"))
    procedure = _artifact(definition)

    assert procedure.directly_runnable is False
    result = evaluate_procedure_law(
        procedure,
        path=procedure_path("triage"),
        predecessor=None,
    )
    assert result.verdict == "accepted"


def test_layout_only_successor_changes_no_registered_semantic_member() -> None:
    predecessor_procedure = _artifact(_layout_definition(halt_before_return=False))
    predecessor = AcceptedProcedureV1(
        path=procedure_path("triage"),
        procedure=predecessor_procedure,
        artifact_digest=procedure_artifact_digest(predecessor_procedure).tagged,
    )
    reordered_definition = _layout_definition(halt_before_return=True)
    assert compute_procedure_definition_digest_v3(reordered_definition).tagged == (
        predecessor_procedure.definition_digest
    )
    successor = _artifact(reordered_definition).model_copy(
        update={"lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest)}
    )

    result = evaluate_procedure_law(
        successor,
        path=predecessor.path,
        predecessor=predecessor,
    )

    assert result.verdict == "refused"
    assert result.diagnostics[0].code == "playbill.proposal.non_singleton_scope"
    assert result.diagnostics[0].message == ("The proposal changes no registered semantic member.")


def test_envelope_pin_only_successor_is_a_semantic_change() -> None:
    predecessor_procedure = _artifact(_definition())
    predecessor = AcceptedProcedureV1(
        path=procedure_path("triage"),
        procedure=predecessor_procedure,
        artifact_digest=procedure_artifact_digest(predecessor_procedure).tagged,
    )
    added_pin = _pin("source", "Source", "governed-input")
    successor = predecessor_procedure.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (*predecessor_procedure.pins, added_pin),
                    key=lambda pin: (
                        pin.role.encode(),
                        pin.target.qualified.encode(),
                        pin.artifact_digest.encode(),
                    ),
                )
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest),
        }
    )

    result = evaluate_procedure_law(
        successor,
        path=predecessor.path,
        predecessor=predecessor,
    )

    assert result.verdict == "accepted"


@pytest.mark.parametrize("change", ("activation", "retirement", "definition"))
def test_procedure_semantic_successors_remain_accepted(change: str) -> None:
    predecessor_procedure = _artifact(_definition())
    predecessor = AcceptedProcedureV1(
        path=procedure_path("triage"),
        procedure=predecessor_procedure,
        artifact_digest=procedure_artifact_digest(predecessor_procedure).tagged,
    )
    definition = predecessor_procedure.definition
    activation_policy = predecessor_procedure.activation_policy
    lifecycle = ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest)
    if change == "activation":
        activation_policy = "snapshot"
    elif change == "retirement":
        lifecycle = ArtifactLifecycle(
            state="retired",
            predecessor_digest=predecessor.artifact_digest,
        )
    else:
        definition = definition.model_copy(update={"description": "A semantic revision."})
    successor = _artifact(definition).model_copy(
        update={"activation_policy": activation_policy, "lifecycle": lifecycle}
    )

    assert (
        evaluate_procedure_law(
            successor,
            path=predecessor.path,
            predecessor=predecessor,
        ).verdict
        == "accepted"
    )


def test_procedure_v2_closes_owned_contracts_but_keeps_query_slot_open() -> None:
    contracts = tuple(
        ProcedureOwnedContractV1(
            identity=ArtifactIdentity(kind="Contract", name=name),
            schema=ContractSchema(fields={}),
        )
        for name in ("claim-rows", "empty-input")
    )
    by_name = {contract.identity.name: contract for contract in contracts}
    contract_in = ArtifactPin(
        role="contract-in",
        target=by_name["empty-input"].identity,
        artifact_digest=procedure_owned_contract_digest(by_name["empty-input"]).tagged,
    )
    contract_out = ArtifactPin(
        role="contract-out",
        target=by_name["claim-rows"].identity,
        artifact_digest=procedure_owned_contract_digest(by_name["claim-rows"]).tagged,
    )
    query_slot = ProcedurePinSlotRefV1(slot_name="query")
    definition = _definition(query=query_slot).model_copy(
        update={
            "contract_in": contract_in,
            "contract_out": contract_out,
            "nodes": (
                StateTapNodeV3(
                    node_id="read",
                    query=query_slot,
                    parameters={},
                    as_="rows",
                ),
                ProjectNodeV3(
                    node_id="shape",
                    fields={},
                    contract_out=contract_out,
                    as_="result",
                ),
            ),
        }
    )
    procedure = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name="triage"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=(contract_in, contract_out),
        owned_contracts=tuple(
            sorted(
                contracts,
                key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
            )
        ),
        activation_policy="drain",
    )

    assert procedure.directly_runnable is False
    assert parse_procedure(render_procedure(procedure), path=procedure_path("triage")) == procedure
    assert (
        evaluate_procedure_law(
            procedure,
            path=procedure_path("triage"),
            predecessor=None,
        ).verdict
        == "accepted"
    )

    wrong = contract_out.model_copy(update={"artifact_digest": _digest("forged")})
    wrong_definition = definition.model_copy(
        update={
            "contract_out": wrong,
            "nodes": (
                definition.nodes[0],
                ProjectNodeV3(
                    node_id="shape",
                    fields={},
                    contract_out=wrong,
                    as_="result",
                ),
            ),
        }
    )
    with pytest.raises(ValidationError, match="does not resolve"):
        ProcedureArtifactV2(
            **{
                **procedure.model_dump(mode="python", exclude={"artifact_format"}),
                "definition": wrong_definition,
                "definition_digest": compute_procedure_definition_digest_v3(
                    wrong_definition
                ).tagged,
                "pins": (contract_in, wrong),
            }
        )


def test_procedure_rejects_exact_node_pin_missing_from_envelope() -> None:
    definition = _definition()
    with pytest.raises(ValidationError, match="exact pins absent"):
        ProcedureArtifactV1(
            identity=ArtifactIdentity(kind="Procedure", name="triage"),
            definition=definition,
            definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
            pins=(),
            activation_policy="drain",
        )


def test_v3_graph_refuses_backward_edge() -> None:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    with pytest.raises(ProcedureGraphFormatError, match="R2"):
        _definition(
            nodes=(
                ProjectNodeV3(
                    node_id="first",
                    fields={},
                    contract_out=contract_out,
                    as_="intermediate",
                ),
                ProjectNodeV3(
                    node_id="second",
                    fields={},
                    contract_out=contract_out,
                    as_="result",
                    next="first",
                ),
            )
        )


def test_proposal_terminal_has_no_activation_or_direct_write_capability() -> None:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    terminal = ProposeChangeSetNodeV3(
        node_id="propose",
        candidate_templates=({"artifact_kind": "Claim", "input": "$steps.result"},),
    )
    definition = _definition(
        nodes=(
            ProjectNodeV3(
                node_id="shape",
                fields={"status": "ready"},
                contract_out=contract_out,
                as_="result",
            ),
            terminal,
        ),
        terminal_capability=2,
    )

    assert definition.nodes[-1].model_dump(mode="json") == {
        "kind": "propose_change_set",
        "node_id": "propose",
        "candidate_templates": [{"artifact_kind": "Claim", "input": "$steps.result"}],
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProposeChangeSetNodeV3.model_validate(
            {
                **terminal.model_dump(mode="json"),
                "activate": True,
            }
        )
    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][1] = {"kind": "apply_entities", "node_id": "write"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ProcedureDefinitionV3.model_validate(payload)
