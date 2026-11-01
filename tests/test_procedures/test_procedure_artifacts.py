"""Procedure artifact envelope and graph-law tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
    procedure_runnability,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
    compute_procedure_node_digests,
)
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    HaltNode,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    ProposeChangeSetNode,
    ReadNode,
)
from cruxible_client.contracts.refs import step_ref
from cruxible_client.contracts.shapes import Comparison, CountOperand, LiteralOperand
from tests.support.procedures import boundary_contract, named, procedure_artifact


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactNameRef:
    """The graph reference a field names; Contracts ride in the envelope."""

    del role
    return ArtifactNameRef(target=ArtifactIdentity(kind=kind, name=name))


#: The governed boundaries ``_definition`` names (accepted beside it in trees).
ARTIFACT_CONTRACTS = (boundary_contract("empty-input"), boundary_contract("claim-rows"))


def _definition(
    *,
    query: ArtifactNameRef | None = None,
    nodes: tuple[object, ...] | None = None,
    terminal_capability: int = 1,
) -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "empty-input/v1")
    contract_out = _pin("contract-out", "Contract", "claim-rows/v1")
    query = query or _pin("query", "QueryDefinition", "claims-by-status")
    default_nodes = (
        ReadNode(node_id="read", query=named(query), parameters={}, as_="rows"),
        ProjectNode(
            node_id="shape",
            fields={"rows": "$steps.rows"},
            as_="result",
        ),
    )
    return ProcedureDefinition(
        name="triage",
        description="Read accepted claims and shape a bounded result.",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        nodes=default_nodes if nodes is None else nodes,  # type: ignore[arg-type]
        returns="result",
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
    read = ReadNode(
        node_id="read",
        query=named(_pin("query", "QueryDefinition", "claims-by-status")),
        parameters={},
        as_="rows",
        next="gate",
    )
    gate = GuardNode(
        node_id="gate",
        predicate=Comparison(
            op="gt", left=CountOperand(count=step_ref("rows")), right=LiteralOperand(literal=0)
        ),
        on_true="result",
        on_false="stop",
    )
    result = ProjectNode(
        node_id="result",
        fields={"rows": "$steps.rows"},
        as_="result",
    )
    stop = HaltNode(node_id="stop", reason="No rows are available.")
    tail = (stop, result) if halt_before_return else (result, stop)
    return _definition(nodes=(read, gate, *tail))


def test_procedure_round_trip_digest_and_node_golden() -> None:
    definition = _definition()
    procedure = _artifact(definition)

    assert definition.graph_format == 8
    assert procedure_runnability(definition) == ("direct", ())
    assert procedure.definition_digest == (
        "sha256:3fb2f634a990b7f14b82c47989f0fbe6a214cf95b65ddba89dbe269ef3e4d1f7"
    )
    nodes = compute_procedure_node_digests(definition)
    assert nodes["read"].subtree_digest == (
        "sha256:713d082f1179a88cc08ca475ecfa358f56699bcf706661c4623bea3e1217be6f"
    )

    content = render_procedure(procedure)
    assert parse_procedure(content, path=procedure_path("triage")) == procedure
    assert procedure_artifact_digest(procedure).tagged.startswith("sha256:")


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


def test_procedure_names_governed_boundary_contracts_and_carries_none() -> None:
    procedure = _artifact(_definition())
    assert parse_procedure(render_procedure(procedure), path=procedure_path("triage")) == procedure
    assert procedure_runnability(procedure.definition) == ("direct", ())
    assert (
        evaluate_procedure_law(procedure, path=procedure_path("triage"), predecessor=None).verdict
        == "accepted"
    )
    assert "owned_contracts" not in type(procedure).model_fields
    # A Procedure names its boundaries as governed Contracts, never carries them.
    assert procedure.definition.contract_in.target == ArtifactIdentity(
        kind="Contract", name="empty-input/v1"
    )
    payload = procedure.model_dump(mode="json", by_alias=True)
    payload["owned_contracts"] = []
    with pytest.raises(ValidationError, match="procedure_owned_contracts_retired"):
        ProcedureArtifact.model_validate(payload)


def test_graph_refuses_backward_edge() -> None:
    with pytest.raises(ProcedureGraphFormatError, match="R2"):
        _definition(
            nodes=(
                ProjectNode(
                    node_id="first",
                    fields={},
                    as_="intermediate",
                ),
                ProjectNode(
                    node_id="second",
                    fields={},
                    as_="result",
                    next="first",
                ),
            )
        )


def test_proposal_terminal_has_no_activation_or_direct_write_capability() -> None:
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


@pytest.mark.parametrize(
    ("kind", "replacement"),
    [
        ("source", "'call'"),
        ("state_tap", "'read' naming its 'query'"),
        ("state_claim", "'read' naming its 'claim_type'"),
        ("exhaust_tap", "nothing: no run lane serves exhaust reads"),
        ("return", "'project' with 'leaf: true'"),
        ("constant", "'project' with a 'literal' record"),
    ],
)
def test_a_retired_node_kind_is_refused_by_name_with_its_replacement(
    kind: str, replacement: str
) -> None:
    """Graph format 7 no longer has these kinds; a stored graph naming one is refused."""

    payload = _definition().model_dump(mode="json", by_alias=True)
    payload["nodes"][0] = {"kind": kind, "node_id": "retired", "as": "rows"}
    with pytest.raises(ValidationError) as caught:
        ProcedureDefinition.model_validate(payload)
    message = str(caught.value)
    assert "procedure_node_kind_retired" in message and kind in message
    assert replacement in message


def test_the_adapter_transform_is_refused_by_name_with_its_replacement() -> None:
    payload = _definition().model_dump(mode="json", by_alias=True)
    payload["nodes"][0] = {
        "kind": "transform",
        "node_id": "adapt",
        "transform_kind": "adapter",
        "as": "rows",
    }
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*'adapter'.*'project'"):
        ProcedureDefinition.model_validate(payload)


def test_procedure_has_no_cross_reference_digests() -> None:
    procedure = _artifact(_definition())
    payload = procedure.model_dump(mode="json", by_alias=True)
    assert payload["artifact_format"] == "cruxible-procedure-v4"
    assert payload["definition"]["graph_format"] == 8
    assert "pins" not in payload
    assert "artifact_digest" not in str(payload["definition"])
    assert "role" not in str(payload["definition"])
    assert ProcedureArtifact.model_validate_json(render_procedure(procedure)) == procedure
    assert parse_procedure(render_procedure(procedure), path=procedure_path("triage")) == procedure


def test_procedure_refuses_pin_reference_fields() -> None:
    payload = _artifact(_definition()).model_dump(mode="json", by_alias=True)
    payload["pins"] = []
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProcedureArtifact.model_validate(payload)
    payload.pop("pins")
    payload["definition"]["nodes"][0]["query"]["role"] = "query"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProcedureArtifact.model_validate(payload)


def test_graph_keeps_reference_shaped_payloads_as_data() -> None:
    payload = {
        "interface_digest": _digest("literal-interface"),
        "implementation_digest": _digest("literal-implementation"),
        "role": "literal-role",
        "target": {"kind": "Contract", "name": "literal-contract"},
        "artifact_digest": _digest("literal-artifact"),
    }
    base = _artifact(_definition())
    nodes = tuple(
        node.model_copy(update={"parameters": payload})
        if isinstance(node, ReadNode)
        else node.model_copy(update={"fields": payload})
        for node in base.definition.nodes
    )
    literal_definition = base.definition.model_copy(update={"nodes": nodes, "annotations": payload})
    procedure = ProcedureArtifact.model_validate(
        {
            **base.model_dump(mode="json", by_alias=True),
            "definition": literal_definition,
            "definition_digest": compute_procedure_definition_digest(literal_definition).tagged,
        }
    )
    restored = parse_procedure(render_procedure(procedure), path=procedure_path("triage"))
    assert restored.definition.annotations == payload
    assert restored.definition.nodes[0].parameters == payload
    assert restored.definition.nodes[1].fields == payload
