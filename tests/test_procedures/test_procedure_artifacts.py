"""Procedure artifact envelope and graph-law tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
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
    ProjectNode,
    ProposeChangeSetNode,
    StateTapNode,
)
from tests.support.procedures import named, procedure_artifact


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactNameRef:
    """The graph reference a field names; Contracts ride in the envelope."""

    del role
    return ArtifactNameRef(target=ArtifactIdentity(kind=kind, name=name))


def _definition(
    *,
    query: ArtifactNameRef | None = None,
    nodes: tuple[object, ...] | None = None,
    terminal_capability: int = 1,
) -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    query = query or _pin("query", "QueryDefinition", "claims-by-status")
    default_nodes = (
        StateTapNode(node_id="read", query=named(query), parameters={}, as_="rows"),
        ProjectNode(
            node_id="shape",
            fields={"rows": "$steps.rows"},
            contract_out=named(contract_out),
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
        query=named(_pin("query", "QueryDefinition", "claims-by-status")),
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
        contract_out=named(contract_out),
        as_="result",
    )
    stop = HaltNode(node_id="stop", reason="No rows are available.")
    tail = (stop, result) if halt_before_return else (result, stop)
    return _definition(nodes=(read, gate, *tail))


def test_procedure_round_trip_digest_and_node_golden() -> None:
    definition = _definition()
    procedure = _artifact(definition)

    assert definition.graph_format == 7
    assert procedure_runnability(definition) == ("direct", ())
    assert procedure.definition_digest == (
        "sha256:87e3602861388e1ea1780c54c2a43255cbd0233b9cbb2dda2bc290735fb6444d"
    )
    nodes = compute_procedure_node_digests(definition)
    assert nodes["read"].subtree_digest == (
        "sha256:acb3c1cff183a4824487c65f3bf6dee35389b9c10ecec062d21e4ba983f099d9"
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


def test_procedure_closes_owned_contracts_by_name() -> None:
    procedure = _artifact(_definition())
    assert parse_procedure(render_procedure(procedure), path=procedure_path("triage")) == procedure
    assert procedure_runnability(procedure.definition) == ("direct", ())
    assert (
        evaluate_procedure_law(procedure, path=procedure_path("triage"), predecessor=None).verdict
        == "accepted"
    )
    payload = procedure.model_dump(mode="json", by_alias=True)
    extra = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name="unreferenced"),
        schema=ContractSchema(fields={}),
    )
    payload["owned_contracts"] = sorted(
        [*payload["owned_contracts"], extra.model_dump(mode="json", by_alias=True)],
        key=canonical_bytes,
    )
    with pytest.raises(ValidationError, match="every owned Contract"):
        ProcedureArtifact.model_validate(payload)
    payload = procedure.model_dump(mode="json", by_alias=True)
    payload["owned_contracts"] = payload["owned_contracts"][:1]
    with pytest.raises(ValidationError, match="every owned Contract"):
        ProcedureArtifact.model_validate(payload)


def test_graph_refuses_backward_edge() -> None:
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    with pytest.raises(ProcedureGraphFormatError, match="R2"):
        _definition(
            nodes=(
                ProjectNode(
                    node_id="first",
                    fields={},
                    contract_out=named(contract_out),
                    as_="intermediate",
                ),
                ProjectNode(
                    node_id="second",
                    fields={},
                    contract_out=named(contract_out),
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
                contract_out=named(contract_out),
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


def test_retained_exhaust_tap_artifacts_still_parse_digest_and_verify() -> None:
    from cruxible_client.contracts.procedures.models import ExhaustTapNode

    base = _definition()
    definition = _definition(
        nodes=(
            ExhaustTapNode(
                node_id="exhaust",
                reducer_or_query=named(_pin("query", "QueryDefinition", "journal-query")),
                journal_identity="retained-journal",
                as_="rows",
            ),
            base.nodes[1],
        )
    )
    artifact = _artifact(definition)
    content = render_procedure(artifact)
    parsed = parse_procedure(content, path=procedure_path("triage"))
    assert render_procedure(parsed) == content
    assert procedure_artifact_digest(parsed) == procedure_artifact_digest(artifact)
    assert (
        evaluate_procedure_law(parsed, path=procedure_path("triage"), predecessor=None).verdict
        == "accepted"
    )
    runnable, (node,) = procedure_runnability(parsed.definition)
    assert runnable == "unsupported"
    assert node.kind == "exhaust_tap" and node.runs_on == "nowhere"


def test_procedure_has_no_cross_reference_digests() -> None:
    procedure = _artifact(_definition())
    payload = procedure.model_dump(mode="json", by_alias=True)
    assert payload["artifact_format"] == "cruxible-procedure-v3"
    assert payload["definition"]["graph_format"] == 7
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


def test_procedure_checks_owned_contract_names() -> None:
    payload = _artifact(_definition()).model_dump(mode="json", by_alias=True)
    payload["owned_contracts"] = []
    with pytest.raises(ValidationError, match="every owned Contract"):
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
        if isinstance(node, StateTapNode)
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
