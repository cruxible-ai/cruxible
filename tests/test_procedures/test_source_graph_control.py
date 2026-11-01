"""Source control flow remains a statically checked, replayable governed graph."""

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contracts import AcceptedProcedureContractValidator
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
)
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    ProcedureDefinition,
    ProjectNode,
    SelectNode,
)
from cruxible_client.contracts.refs import input_ref
from cruxible_client.contracts.shapes import Comparison, LiteralOperand
from cruxible_core.procedures.execution import ProcedureExecutor
from tests.support.procedures import (
    bind_fixture,
    boundary_contract,
    contract_resolver,
    named,
    procedure_definition,
)
from tests.test_procedures.test_procedure_execution import (
    _Authority,
    _fixture,
    _hard_caps,
    _owned_contract,
    _owned_pin,
    _prepare,
    _StateReader,
)

_CHOICE = (
    boundary_contract("choice-input", {"choice": "bool"}),
    boundary_contract("choice-output", {"value": "int"}),
)


def definition(*, early=False):
    ci = _owned_contract("choice-input", {"choice": "bool"})
    co = _owned_contract("choice-output", {"value": "int"})
    pi, po = named(_owned_pin("contract-in", ci)), named(_owned_pin("contract-out", co))
    assert (ci, co) == _CHOICE
    branch = GuardNode(
        node_id="choose",
        predicate=Comparison(op="eq", left=input_ref("choice"), right=LiteralOperand(literal=True)),
        on_true="yes",
        on_false="no",
    )
    factory = ProjectNode
    nodes = [
        branch,
        factory(
            node_id="yes",
            fields={"value": 1},
            as_="yes",
            leaf=early,
            next=None if early else "join",
        ),
        factory(
            node_id="no",
            fields={"value": 2},
            as_="no",
            leaf=early,
            next=None if early else "join",
        ),
    ]
    if not early:
        nodes += [
            SelectNode(node_id="join", sources=("yes", "no"), as_="joined"),
            ProjectNode(
                node_id="return",
                fields="$steps.joined",
                as_="result",
                leaf=True,
            ),
        ]
    graph = procedure_definition(
        name="choice",
        contract_in=named(pi),
        contract_out=named(po),
        nodes=tuple(nodes),
        returns="yes" if early else "result",
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    artifact = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name="choice"),
        definition=graph,
        definition_digest=compute_procedure_definition_digest(graph).tagged,
        activation_policy="snapshot",
    )
    return bind_fixture(
        AcceptedProcedure(
            path=procedure_path("choice"),
            procedure=artifact,
            artifact_digest=procedure_artifact_digest(artifact).tagged,
        )
    )


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("choice,expected", [(True, 1), (False, 2)])
def test_join_and_early_returns_execute_selected_branch(tmp_path, early, choice, expected):
    accepted = definition(early=early)
    fixture = _fixture(tmp_path)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=AcceptedProcedureContractValidator(contract_resolver(*_CHOICE)),
    )
    prepared = _prepare(accepted, fixture, _StateReader(), invocation_input={"choice": choice})
    result = executor.execute(prepared, accepted)
    assert result.status == "succeeded", result
    assert result.output == {"value": expected}
    replayed = executor.execute(prepared, accepted)
    assert replayed.output == result.output


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("both", "exactly one"),
        ("missing", "exactly one"),
        ("unknown", "outside"),
        # A select pins no Contract: its value is typed as it flows.
        ("schema", "Extra inputs"),
    ],
)
def test_unsafe_joins_fail_before_execution(mutation, message):
    graph = definition().procedure.definition.model_dump(mode="json", by_alias=True)
    if mutation == "both":
        graph["nodes"][1]["next"] = "no"
    elif mutation == "missing":
        graph["nodes"][0]["on_false"] = "join"
        graph["nodes"][1]["next"] = "no"
    elif mutation == "unknown":
        graph["nodes"][3]["sources"] = ["yes", "typo"]
    else:
        graph["nodes"][3]["contract_out"] = {"target": {"kind": "Contract", "name": "out/v1"}}
    with pytest.raises((ValueError, ProcedureGraphFormatError), match=message):
        ProcedureDefinition.model_validate(graph)
