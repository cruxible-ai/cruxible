"""One Procedure authoring form: the typed spec. The Python source and graphs refuse."""

import pytest

from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimTypeAuthoringPayload,
    ProcedureAuthoringPayload,
    QueryDefinitionAuthoringPayload,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._support import initialize_local
from tests.support.procedures import ref, spec_payload

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _compile(instance, payload):
    return AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    )


@pytest.mark.parametrize(
    ("definition", "form"),
    [
        (
            {"name": "assessment", "source_request": {"text": "def assess(request): ..."}},
            "source_request",
        ),
        ({"name": "assessment", "nodes": [{"kind": "halt", "node_id": "stop"}]}, "graph"),
    ],
)
def test_a_retired_payload_form_refuses_typed_naming_the_spec(tmp_path, definition, form):
    """Even below the authoring input, only a ProcedureSpec lowers to a Procedure."""

    instance, _owner = initialize_local(tmp_path)
    before = instance.accepted_coordinate()
    result = _compile(
        instance,
        ProcedureAuthoringPayload(activation_policy="snapshot", definition=definition),
    )
    assert result.verdict == "refused"
    (refusal,) = result.frontier.diagnostics
    assert refusal.code == "cruxible.authoring.procedure_authoring_form_retired"
    assert "ProcedureSpec" in refusal.message and form in refusal.message
    assert instance.accepted_coordinate() == before


def _status_reader(query_name: str) -> dict[str, object]:
    return {
        "procedure": "assessment",
        "input": {},
        "output": {"positive": "bool"},
        "steps": [
            {"read": "rows", "query": query_name},
            {
                "read": "status",
                "claim_type": "project.work_item.status",
                "subject": "wi-42",
                "subject_kind": "project.work_item",
            },
            {
                "project": "result",
                "fields": {"positive": True},
                "output": {"positive": "bool"},
            },
        ],
        "returns": "result",
    }


def test_a_spec_prepares_against_the_claim_type_and_query_staged_beside_it(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_core.authoring.preflight import compute_preflight
    from tests.test_authoring.test_authoring_procedures import _change_set_query
    from tests.test_claims.test_claims import vocabulary_claim_type as _claim_type

    instance, _owner = initialize_local(tmp_path)
    claim_type, query = _claim_type(), _change_set_query(named=True)
    payload = ChangeSetAuthoringPayload(
        members=(
            ClaimTypeAuthoringPayload(claim_type=claim_type),
            spec_payload(_status_reader(query.identity.name)),
            QueryDefinitionAuthoringPayload(query_definition=query),
        )
    )
    before = instance.accepted_coordinate()
    result = _compile(instance, payload)
    assert result.verdict == "passed", result.frontier.model_dump_json()
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    pending = coordinator.get(result.certificate.intent_id, actor=actor).intent
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    assert instance.accepted_coordinate() == before
    path = procedure_path("assessment")
    procedure = parse_procedure(lowered.proposed_tree[path], path=path)
    reads = {node.as_: node for node in procedure.definition.nodes if node.kind == "read"}
    assert reads["rows"].query.target == query.identity
    assert reads["status"].claim_type.target.name == claim_type.predicate


def test_a_child_procedure_staged_in_the_same_change_set_stays_unresolved(tmp_path):
    from tests.test_procedures.test_nested_source_runs import blueprints

    instance, _ = initialize_local(tmp_path)
    child, parent = blueprints()
    result = _compile(
        instance,
        ChangeSetAuthoringPayload(members=(spec_payload(child), spec_payload(parent))),
    )
    assert result.verdict == "refused"
    assert any(
        "Procedure:child is not live" in diagnostic.message
        for diagnostic in result.frontier.diagnostics
    ), result.frontier.diagnostics


def test_a_spec_reference_is_typed_even_inside_a_change_set_member(tmp_path):
    instance, _ = initialize_local(tmp_path)
    spec = _status_reader("project.missing")
    spec["steps"][-1]["fields"] = {"positive": ref("$steps.status.value")}
    result = _compile(instance, spec_payload(spec))
    assert result.verdict == "refused"
    assert {diagnostic.code for diagnostic in result.frontier.diagnostics} == {
        "cruxible.authoring.artifact_reference_unresolved"
    }
