"""Procedure artifact envelope and graph-law tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    procedure_runnability,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
    compute_procedure_node_digests,
)
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    GuardPredicate,
    HaltNode,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProcedurePinSlot,
    ProcedurePinSlotRef,
    ProjectNode,
    ProposeChangeSetNode,
    StateTapNode,
)
from tests.support.procedures import owned_contract, owned_pin, procedure_artifact


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    if kind == "Contract":
        # Contracts ride in the envelope: pin the exact owned Contract digest.
        return owned_pin(role, owned_contract(name))
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _definition(
    *,
    query: ArtifactPin | ProcedurePinSlotRef | None = None,
    nodes: tuple[object, ...] | None = None,
    terminal_capability: int = 1,
) -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    query = query or _pin("query", "QueryDefinition", "claims-by-status")
    default_nodes = (
        StateTapNode(node_id="read", query=query, parameters={}, as_="rows"),
        ProjectNode(
            node_id="shape",
            fields={"rows": "$steps.rows"},
            contract_out=contract_out,
            as_="result",
        ),
    )
    return ProcedureDefinition(
        name="triage",
        description="Read accepted claims and shape a bounded result.",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=default_nodes if nodes is None else nodes,  # type: ignore[arg-type]
        returns="result",
        pin_slots=(
            (
                ProcedurePinSlot(
                    slot_name="query",
                    pin_role="query",
                    artifact_kind="QueryDefinition",
                    interface_digest=_digest("query-interface"),
                ),
            )
            if isinstance(query, ProcedurePinSlotRef)
            else ()
        ),
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=terminal_capability,  # type: ignore[arg-type]
    )


def _artifact(definition: ProcedureDefinition) -> ProcedureArtifact:
    return procedure_artifact(definition, activation_policy="drain")


def _layout_definition(*, halt_before_return: bool) -> ProcedureDefinition:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    read = StateTapNode(
        node_id="read",
        query=_pin("query", "QueryDefinition", "claims-by-status"),
        parameters={},
        as_="rows",
        next="gate",
    )
    gate = GuardNode(
        node_id="gate",
        predicate=GuardPredicate(
            left=PredicateOperand(kind="count", alias="rows"),
            operator="gt",
            right=PredicateOperand(kind="literal", value=0),
        ),
        on_true="result",
        on_false="stop",
        refusal_code="rows.empty",
        message="No rows are available.",
    )
    result = ProjectNode(
        node_id="result",
        fields={"rows": "$steps.rows"},
        contract_out=contract_out,
        as_="result",
    )
    stop = HaltNode(node_id="stop", reason="No rows are available.")
    tail = (stop, result) if halt_before_return else (result, stop)
    return _definition(nodes=(read, gate, *tail))


def test_procedure_round_trip_digest_and_node_golden() -> None:
    definition = _definition()
    procedure = _artifact(definition)

    assert definition.graph_format == 6
    assert procedure_runnability(definition) == ("direct", ())
    assert procedure.definition_digest == (
        "sha256:05dfe8ae33871cffa1fbe92826661ece478f2817cdd7e1da8e8e79d455c80ef6"
    )
    nodes = compute_procedure_node_digests(definition)
    assert nodes["read"].subtree_digest == (
        "sha256:98667c3be43ddf10c9416fa2abb0c80721d68f9e9c8de0eac74f55b685582da5"
    )

    content = render_procedure(procedure)
    assert parse_procedure(content, path=procedure_path("triage")) == procedure
    assert procedure_artifact_digest(procedure).tagged.startswith("sha256:")


def test_open_slot_procedure_is_refused_and_unsupported() -> None:
    definition = _definition(query=ProcedurePinSlotRef(slot_name="query"))
    procedure = _artifact(definition)

    runnable, rows = procedure_runnability(definition)
    assert runnable == "unsupported"
    assert [(row.kind, row.runs_on) for row in rows] == [("open_slot", "nowhere")]
    result = evaluate_procedure_law(
        procedure,
        path=procedure_path("triage"),
        predecessor=None,
    )
    assert result.verdict == "refused"
    assert result.diagnostics[0].code == "cruxible.procedure.open_slots"


def test_layout_only_successor_changes_no_registered_semantic_member() -> None:
    predecessor_procedure = _artifact(_layout_definition(halt_before_return=False))
    predecessor = AcceptedProcedure(
        path=procedure_path("triage"),
        procedure=predecessor_procedure,
        artifact_digest=procedure_artifact_digest(predecessor_procedure).tagged,
    )
    reordered_definition = _layout_definition(halt_before_return=True)
    assert compute_procedure_definition_digest(reordered_definition).tagged == (
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
    assert result.diagnostics[0].code == "cruxible.proposal.non_singleton_scope"
    assert result.diagnostics[0].message == ("The proposal changes no registered semantic member.")


def test_envelope_pin_only_successor_is_a_semantic_change() -> None:
    predecessor_procedure = _artifact(_definition())
    predecessor = AcceptedProcedure(
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
    predecessor = AcceptedProcedure(
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


def test_procedure_v2_closes_owned_contracts_and_refuses_an_open_query_slot() -> None:
    contracts = tuple(
        ProcedureOwnedContract(
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
    query_slot = ProcedurePinSlotRef(slot_name="query")
    definition = _definition(query=query_slot).model_copy(
        update={
            "contract_in": contract_in,
            "contract_out": contract_out,
            "nodes": (
                StateTapNode(
                    node_id="read",
                    query=query_slot,
                    parameters={},
                    as_="rows",
                ),
                ProjectNode(
                    node_id="shape",
                    fields={},
                    contract_out=contract_out,
                    as_="result",
                ),
            ),
        }
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name="triage"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=(contract_in, contract_out),
        owned_contracts=tuple(
            sorted(
                contracts,
                key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
            )
        ),
        activation_policy="drain",
    )

    assert procedure_runnability(definition)[0] == "unsupported"
    assert parse_procedure(render_procedure(procedure), path=procedure_path("triage")) == procedure
    refused = evaluate_procedure_law(
        procedure,
        path=procedure_path("triage"),
        predecessor=None,
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == "cruxible.procedure.open_slots"
    exact_query = _pin("query", "QueryDefinition", "claims-by-status")
    closed_definition = definition.model_copy(
        update={
            "pin_slots": (),
            "nodes": (
                StateTapNode(node_id="read", query=exact_query, parameters={}, as_="rows"),
                definition.nodes[1],
            ),
        }
    )
    closed = ProcedureArtifact(
        **{
            **procedure.model_dump(mode="python", exclude={"artifact_format"}),
            "definition": closed_definition,
            "definition_digest": compute_procedure_definition_digest(closed_definition).tagged,
            "pins": tuple(
                sorted(
                    (contract_in, contract_out, exact_query),
                    key=lambda pin: (
                        pin.role.encode(),
                        pin.target.qualified.encode(),
                        pin.artifact_digest.encode(),
                    ),
                )
            ),
        }
    )
    assert procedure_runnability(closed_definition) == ("direct", ())
    assert (
        evaluate_procedure_law(
            closed,
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
                ProjectNode(
                    node_id="shape",
                    fields={},
                    contract_out=wrong,
                    as_="result",
                ),
            ),
        }
    )
    with pytest.raises(ValidationError, match="does not resolve"):
        ProcedureArtifact(
            **{
                **procedure.model_dump(mode="python", exclude={"artifact_format"}),
                "definition": wrong_definition,
                "definition_digest": compute_procedure_definition_digest(wrong_definition).tagged,
                "pins": (contract_in, wrong),
            }
        )


def test_procedure_rejects_exact_node_pin_missing_from_envelope() -> None:
    definition = _definition()
    with pytest.raises(ValidationError, match="exact pins absent"):
        ProcedureArtifact(
            identity=ArtifactIdentity(kind="Procedure", name="triage"),
            definition=definition,
            definition_digest=compute_procedure_definition_digest(definition).tagged,
            pins=(),
            owned_contracts=_artifact(definition).owned_contracts,
            activation_policy="drain",
        )


def test_graph_refuses_backward_edge() -> None:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    with pytest.raises(ProcedureGraphFormatError, match="R2"):
        _definition(
            nodes=(
                ProjectNode(
                    node_id="first",
                    fields={},
                    contract_out=contract_out,
                    as_="intermediate",
                ),
                ProjectNode(
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
    terminal = ProposeChangeSetNode(
        node_id="propose",
        candidate_templates=({"artifact_kind": "Claim", "input": "$steps.result"},),
        result="$steps.result",
    )
    definition = _definition(
        nodes=(
            ProjectNode(
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
        "claim_types": [],
        "result": "$steps.result",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProposeChangeSetNode.model_validate(
            {
                **terminal.model_dump(mode="json"),
                "activate": True,
            }
        )
    payload = definition.model_dump(mode="json", by_alias=True)
    payload["nodes"][1] = {"kind": "apply_entities", "node_id": "write"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ProcedureDefinition.model_validate(payload)
