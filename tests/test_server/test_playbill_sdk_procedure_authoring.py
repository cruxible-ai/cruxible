"""Procedure authoring must work beyond constructing an SDK draft."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.authoring.examples import procedure_example
from cruxible_client.authoring.inputs import ProcedureInput, QueryDefinitionInput
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.get_reads import GetProcedureCard
from cruxible_client.contracts.procedures.authored import ReadStep
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryEvaluationPolicy
from cruxible_client.contracts.query.grammar import QueryBudgets, QueryEntry
from cruxible_client.transport.http import CruxibleClient
from tests.core_support._light_capture_support import record_contract as capture_contract
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only


@pytest.mark.parametrize("state_tap", [False, True])
def test_sdk_concrete_procedure_prepares_submits_and_runs(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path, state_tap: bool
) -> None:
    http, instance_id, reviewer_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)

    definition = procedure_example()
    if state_tap:
        steps = list(definition.spec.steps)
        steps.insert(0, ReadStep(read="inventory", query="inventory"))
        definition = definition.model_copy(
            update={"spec": definition.spec.model_copy(update={"steps": tuple(steps)})}
        )
        # The builder must resolve at preparation, not fetch or guess a pin.
        draft = pb.procedure(definition=definition)
        missing = draft.prepare()
        assert missing.refused
        assert any(
            item.code == "cruxible.authoring.artifact_reference_unresolved"
            for item in missing.diagnostics
        )
        query = QueryDefinition(
            identity=ArtifactIdentity(kind="QueryDefinition", name="inventory"),
            entry=QueryEntry(binding="asset", subject_kinds=("asset",)),
            result_binding="asset",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
            maximum_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
        )
        compiled = transport.compile_authoring_input(
            instance_id,
            input=QueryDefinitionInput(kind="query_definition", query_definition=query).model_dump(
                mode="json"
            ),
        )
        assert compiled.verdict == "passed", compiled.frontier
        intent_id = compiled.certificate.intent_id
        assert isinstance(intent_id, str)
        submitted = transport.submit_authoring_intent(instance_id, intent_id, accept="never")
        proposal_id = submitted.status.proposal_id
        assert proposal_id is not None
        _approve_and_activate(http, instance_id, reviewer_key, proposal_id)
        # Retrying re-checks the refused intent at the head, where the newly
        # accepted dependency resolves, and advances it; no client pin substitution.
        intent = missing.prepare()
    else:
        intent = pb.procedure(definition=definition).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit(accept="never")
    proposal = intent.proposal
    assert proposal is not None
    proposal.review()
    _approve_and_activate(http, instance_id, reviewer_key, proposal.proposal_id)

    procedure = pb.accepted_procedure("replace-me")
    card = pb.get(procedure.ref).value
    assert isinstance(card, GetProcedureCard)
    assert card.runnable == "direct"
    run = procedure.run()
    assert run.status == "succeeded"
    assert run.succeeded
    assert run.result.count == 1
    assert run.receipt is not None


def test_sdk_program_identity_follows_the_authored_spec() -> None:
    definition = procedure_example()
    revised = definition.model_copy(
        update={"spec": definition.spec.model_copy(update={"description": "Revised."})}
    )
    first = Cruxible.procedure(object(), definition=definition)
    second = Cruxible.procedure(object(), definition=revised)
    assert first.program_stamp != second.program_stamp


@pytest.mark.parametrize("successor", [None, "explicit", "fallthrough"])
def test_sdk_capture_terminal_prepares_but_cannot_continue(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
    successor: str | None,
) -> None:
    http, instance_id, reviewer_key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)
    contract = capture_contract()
    prepared = pb.changes().capture_contract(contract).prepare()
    assert not prepared.refused, prepared.diagnostics
    submitted = prepared.submit(accept="never").status()
    assert submitted.proposal_id is not None
    _approve_and_activate(http, instance_id, reviewer_key, submitted.proposal_id)
    pb.refresh()

    definition = procedure_example()
    result = {"$ref": "step", "step": str(definition.spec.returns)}
    steps: list[dict[str, object]] = [
        step.model_dump(mode="json", by_alias=True) for step in definition.spec.steps
    ]
    terminal: dict[str, object] = {
        "emit": contract.identity.name,
        "id": "emit",
        "input": result,
        "result": result,
    }
    steps.append(terminal)
    if successor is not None:
        steps.append({"halt": None, "id": "after"})
        if successor == "explicit":
            terminal["next"] = "after"
    definition = ProcedureInput.model_validate(
        {**definition.spec.model_dump(mode="json", by_alias=True), "steps": steps}
    )
    intent = pb.procedure(definition=definition).prepare()
    if successor is None:
        assert not intent.refused, intent.diagnostics
    else:
        assert intent.refused
        assert any(
            item.code == "cruxible.authoring.procedure_definition_invalid"
            for item in intent.diagnostics
        ), intent.diagnostics
