"""Retired source candidates cannot select evidence, lineage or self-source bytes."""

import importlib.util

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.procedures.claims import ClaimsNode
from tests.test_procedures.test_claims_terminal import claims_spec


def test_public_proposal_item_models_and_source_binder_are_removed():
    assert importlib.util.find_spec("cruxible_client.contracts.procedures.proposal_items") is None
    assert importlib.util.find_spec("cruxible_core.procedures.source_candidates") is None


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "citation_role",
        "revises",
        "retires",
        "existing_claim_dispositions",
        "derivation",
        "procedure_evidence",
    ],
)
def test_a_claim_mapping_cannot_carry_a_command(field):
    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    raw = claims_spec().model_dump(mode="json", by_alias=True)
    raw["steps"][-1]["claims"][0][field] = "provider supplied"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProcedureSpec.model_validate(raw)


@pytest.mark.parametrize("field", ["candidate_templates", "items", "source", "activate"])
def test_the_claims_node_cannot_consume_candidate_items(field):
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef

    with pytest.raises(ValidationError, match="Extra inputs"):
        ClaimsNode.model_validate(
            {
                "node_id": "write",
                "for_each": "$steps.rows.records",
                "subject": {"kind": "test.item", "key": {}},
                "claims": [
                    {
                        "type": ArtifactNameRef(
                            target=ArtifactIdentity(kind="ClaimType", name="test.item.status")
                        ),
                        "value": "open",
                    }
                ],
                field: [{"tag": "playbill-procedure-claim-proposal-item-v2"}],
            }
        )
