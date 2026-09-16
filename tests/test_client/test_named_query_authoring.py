"""Named query drafts use the coordinator and share declarative input semantics."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from cruxible_client.authoring.examples import (
    query_claims_by_type_example,
    query_ontology_example,
    query_procedures_example,
)
from cruxible_client.authoring.sdk import Playbill
from cruxible_client.authoring.sdk_types import ClaimTypeRef, PendingClaimTypeRef
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputV1,
    lower_authoring_input,
)
from tests.test_client.test_playbill_sdk import _Client, _workspace


@pytest.mark.parametrize(
    "example", [query_claims_by_type_example, query_ontology_example, query_procedures_example]
)
def test_typed_and_declarative_query_drafts_prepare_through_coordinator(tmp_path: Path, example):
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(client, instance_id="inst_test", workspace=tmp_path)
    definition = example()
    draft = pb.query_definition(definition=definition)
    assert draft.payload == lower_authoring_input(
        TypeAdapter(AuthoringInputV1).validate_json(definition.model_dump_json())
    )
    assert draft.payload.query_definition.pins == ()
    intent = draft.prepare()
    assert intent.intent_id.startswith("AIT-")
    assert client.compiled["payload"] == draft.payload.model_dump(mode="json")
    changes = pb.changes(rationale="Define a reusable query")
    changes.query_definition(definition)
    assert changes._compiled().payload.members == (draft.payload,)


def test_query_refs_preserve_staleness_expectations_and_allow_same_set_refs(tmp_path):
    _workspace(tmp_path)
    pb = Playbill._from_client(_Client(), instance_id="inst_test", workspace=tmp_path)
    definition = query_claims_by_type_example()
    ref = ClaimTypeRef("project.work_item.status", pb.coordinate)
    draft = pb.query_definition(definition=definition, vocabulary=(ref,))
    assert (
        draft.reference_expectations[0].payload_path
        == "query_definition.projection.fields[1].value.predicate"
    )
    assert draft.reference_expectations[0].minted_coordinate == pb.coordinate
    pending = PendingClaimTypeRef(ref.address, ref.coordinate, object_kind="literal")
    assert (
        pb.query_definition(definition=definition, vocabulary=(pending,)).reference_expectations
        == ()
    )
    with pytest.raises(ValueError, match="not used"):
        pb.query_definition(
            definition=definition, vocabulary=(ClaimTypeRef("other.unused", pb.coordinate),)
        )


def test_typed_definition_listing_rejects_partial_or_misbound_results():
    from cruxible_client.contracts import PlaybillQueryRun
    from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
    from tests.test_authoring.test_authoring_change_set_intents import _predicate_type

    claim_type = _predicate_type("security.asset.owner")
    artifact = {
        "identity": claim_type.identity.qualified,
        "path": claim_type_path(claim_type.predicate),
        "artifact_digest": claim_type_digest(claim_type).tagged,
        "definition": claim_type.model_dump(mode="json"),
    }
    body = {
        "result_shape": "artifact_definition",
        "verdict": "completed",
        "truncation": {"clipped_budgets": []},
        "rows": [{"artifact": artifact}],
    }
    # Only the accessor is under test; transport envelope validation is exercised
    # in test_named_query_workflow through all three public interfaces.
    result = PlaybillQueryRun.model_construct(result=body)
    assert result.artifact_definitions[0].definition == claim_type
    for change in (
        {"verdict": "refused"},
        {"truncation": {"clipped_budgets": ["max_results"]}},
        {"result_shape": "subject"},
        {"rows": [{"artifact": {**artifact, "artifact_digest": "sha256:" + "0" * 64}}]},
    ):
        with pytest.raises(ValueError):
            PlaybillQueryRun.model_construct(result={**body, **change}).artifact_definitions
