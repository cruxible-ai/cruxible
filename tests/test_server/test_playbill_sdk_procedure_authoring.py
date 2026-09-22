"""Procedure authoring must work beyond constructing an SDK draft."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Playbill
from cruxible_client.authoring.examples import procedure_example
from cruxible_client.authoring.inputs import AuthoringInputError, QueryDefinitionInput
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayloadV2
from cruxible_client.contracts.procedures.artifacts import procedure_owned_contract_digest
from cruxible_client.contracts.query.definitions import QueryDefinitionV1, QueryEvaluationPolicyV1
from cruxible_client.contracts.query.grammar import QueryBudgetsV1, QueryEntryV1
from cruxible_client.transport.http import CruxibleClient
from tests.core_support._pc_c_support import capture_contract
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate


@pytest.mark.parametrize("state_tap", [False, True])
def test_sdk_concrete_procedure_prepares_submits_and_runs(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path, state_tap: bool
) -> None:
    http, instance_id, reviewer_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=tmp_path)

    definition = procedure_example()
    if state_tap:
        nodes = definition.definition["nodes"]
        assert isinstance(nodes, list)
        nodes.insert(
            0,
            {
                "kind": "state_tap",
                "node_id": "read",
                "query": {
                    "kind": "accepted",
                    "role": "query",
                    "target": "QueryDefinition:inventory",
                },
                "parameters": {},
                "as": "inventory",
            },
        )
        # The builder must resolve at preparation, not fetch or guess a pin.
        draft = pb.procedure(definition=definition)
        missing = draft.prepare()
        assert missing.refused
        assert any(
            item.code == "playbill.authoring.artifact_reference_unresolved"
            for item in missing.diagnostics
        )
        query = QueryDefinitionV1(
            identity=ArtifactIdentity(kind="QueryDefinition", name="inventory"),
            entry=QueryEntryV1(binding="asset", subject_kinds=("asset",)),
            result_binding="asset",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            evaluation_policy=QueryEvaluationPolicyV1(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgetsV1(max_results=10, max_traversal_depth=0),
            maximum_budgets=QueryBudgetsV1(max_results=10, max_traversal_depth=0),
        )
        compiled = transport.compile_playbill_authoring_input(
            instance_id,
            input=QueryDefinitionInput(kind="query_definition", query_definition=query).model_dump(
                mode="json"
            ),
        )
        assert compiled.verdict == "passed", compiled.frontier
        intent_id = compiled.certificate["intent_id"]
        assert isinstance(intent_id, str)
        submitted = transport.submit_playbill_authoring_intent(instance_id, intent_id)
        proposal_id = submitted.status.proposal_id
        assert proposal_id is not None
        _approve_and_activate(http, instance_id, reviewer_key, proposal_id)
        # Retrying preserves the original intent base. Explicit rebasing makes
        # the newly accepted dependency available; no client pin substitution.
        assert missing.prepare().refused
        intent = missing.rebase().prepare()
    else:
        intent = pb.procedure(definition=definition).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit()
    proposal = intent.proposal
    assert proposal is not None
    proposal.review()
    _approve_and_activate(http, instance_id, reviewer_key, proposal.proposal_id)

    procedure = pb.accepted_procedure("replace-me")
    assert procedure.readiness().state == "ready"
    run = procedure.run()
    assert run.status == "succeeded"
    assert run.succeeded
    assert run.result.count == 1
    assert run.receipt is not None


@pytest.mark.parametrize("duplicate", [False, True])
def test_sdk_reports_missing_or_duplicate_carried_contracts(duplicate: bool) -> None:
    definition = procedure_example()
    contracts = definition.contracts
    invalid = definition.model_copy(
        update={"contracts": (*contracts, contracts[0]) if duplicate else contracts[1:]}
    )
    with pytest.raises(AuthoringInputError) as error:
        Playbill.procedure(object(), definition=invalid)
    assert error.value.code == (
        "playbill.authoring.carried_contract_duplicate"
        if duplicate
        else "playbill.authoring.carried_contract_unresolved"
    )


def test_sdk_program_identity_includes_carried_schemas() -> None:
    definition = procedure_example()
    contract = definition.contracts[0]
    revised = definition.model_copy(
        update={
            "contracts": (
                contract.model_copy(update={"allow_extra": True}),
                *definition.contracts[1:],
            )
        }
    )
    first = Playbill.procedure(object(), definition=definition)
    second = Playbill.procedure(object(), definition=revised)
    assert first.program_stamp != second.program_stamp


def test_sdk_never_reinterprets_exact_pins_as_authoring_references(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    http, instance_id, _reviewer_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    definition = procedure_example()
    draft = pb.procedure(definition=definition)
    assert isinstance(draft.payload, ProcedureAuthoringPayloadV2)
    contract = next(
        item for item in draft.payload.owned_contracts if item.identity.name == "empty-input"
    )
    pin = ArtifactPin(
        role="contract-in",
        target=ArtifactIdentity(kind="Contract", name="empty-input"),
        artifact_digest=procedure_owned_contract_digest(contract).tagged,
    )
    invalid = definition.model_copy(
        update={
            "definition": {
                **definition.definition,
                "contract_in": pin.model_dump(mode="json"),
            }
        }
    )
    intent = pb.procedure(definition=invalid).prepare()
    assert intent.refused
    assert any(
        item.code == "playbill.authoring.caller_artifact_digest_forbidden"
        for item in intent.diagnostics
    )


@pytest.mark.parametrize("graph_format", [4, 5])
@pytest.mark.parametrize("successor", [None, "explicit", "fallthrough"])
def test_sdk_capture_terminal_prepares_but_cannot_continue(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
    graph_format: int,
    successor: str | None,
) -> None:
    http, instance_id, reviewer_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    contract = capture_contract()
    prepared = pb.changes().capture_contract(contract).prepare()
    assert not prepared.refused, prepared.diagnostics
    submitted = prepared.submit().status()
    assert submitted.proposal_id is not None
    _approve_and_activate(http, instance_id, reviewer_key, submitted.proposal_id)
    pb.refresh()

    definition = procedure_example()
    definition.definition["graph_format"] = graph_format
    nodes = definition.definition["nodes"]
    assert isinstance(nodes, list)
    terminal = {
        "kind": "emit_capture",
        "node_id": "emit",
        "capture_contract": {
            "kind": "accepted",
            "role": "capture-contract",
            "target": contract.identity.qualified,
        },
        "input": "$steps." + str(definition.definition["returns"]),
    }
    nodes.append(terminal)
    if successor is not None:
        nodes.append({"kind": "halt", "node_id": "after"})
        if successor == "explicit":
            terminal["next"] = "after"
    intent = pb.procedure(definition=definition).prepare()
    if successor is None:
        assert not intent.refused, intent.diagnostics
    else:
        assert intent.refused
        assert any(
            item.code == "playbill.authoring.procedure_definition_invalid"
            for item in intent.diagnostics
        ), intent.diagnostics
