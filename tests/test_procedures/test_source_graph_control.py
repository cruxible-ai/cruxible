"""Source control flow remains a statically checked, replayable governed graph."""

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
)
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureDefinitionV6,
    ProjectNodeV3,
    ReturnNodeV6,
    SelectNodeV6,
)
from cruxible_core.procedures.execution import ProcedureExecutor
from tests.test_procedures.test_procedure_execution import (
    _Authority,
    _budget,
    _fixture,
    _hard_caps,
    _owned_contract,
    _owned_pin,
    _prepare,
    _StateReader,
)


def definition(*, early=False):
    ci = _owned_contract("choice-input", {"choice": PropertySchema(type="bool")})
    co = _owned_contract("choice-output", {"value": PropertySchema(type="int")})
    pi, po = _owned_pin("contract-in", ci), _owned_pin("contract-out", co)
    branch = GuardNodeV3(
        node_id="choose",
        predicate=GuardPredicateV1(
            left=PredicateOperandV1(kind="input", input_name="choice"),
            operator="eq",
            right=PredicateOperandV1(kind="literal", value=True),
        ),
        on_true="yes",
        on_false="no",
        refusal_code="choice",
        message="Choose a branch",
    )
    factory = ReturnNodeV6 if early else ProjectNodeV3
    nodes = [
        branch,
        factory(
            node_id="yes",
            fields={"value": 1},
            contract_out=po,
            as_="yes",
            next=None if early else "join",
        ),
        factory(
            node_id="no",
            fields={"value": 2},
            contract_out=po,
            as_="no",
            next=None if early else "join",
        ),
    ]
    if not early:
        nodes += [
            SelectNodeV6(node_id="join", sources=("yes", "no"), contract_out=po, as_="joined"),
            ReturnNodeV6(node_id="return", fields="$steps.joined", contract_out=po, as_="result"),
        ]
    graph = ProcedureDefinitionV6(
        name="choice",
        contract_in=pi,
        contract_out=po,
        nodes=tuple(nodes),
        returns="yes" if early else "result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    artifact = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name="choice"),
        definition=graph,
        definition_digest=compute_procedure_definition_digest(graph).tagged,
        pins=(pi, po),
        owned_contracts=tuple(
            sorted((ci, co), key=lambda c: canonical_bytes(c.model_dump(mode="json")))
        ),
        activation_policy="snapshot",
    )
    return AcceptedProcedureV1(
        path=procedure_path("choice"),
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
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
        contract_validator=OwnedProcedureContractValidator(accepted),
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
        ("schema", "exact output contract"),
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
        graph["nodes"][1]["contract_out"]["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises((ValueError, ProcedureGraphFormatError), match=message):
        ProcedureDefinitionV6.model_validate(graph)
