"""Retirement commands cannot arrive as Procedure data; absence belongs to B4b."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.procedures.claims import ClaimTemplate
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_procedures import test_procedure_proposal_delivery as delivery

LATER = delivery.NOW + timedelta(hours=1)
GONE = "The source no longer lists this record."


def _proposed_world(tmp_path: Path, *, claim_type=None):
    instance, owner, root, line, _procedure = delivery.proposal_world(
        tmp_path, claim_type=claim_type
    )
    state = delivery.run_line(instance, root, line)
    egress = state.terminal_egress[0]
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    return instance, owner, root, line, Path(egress.children[0].path).stem


@pytest.mark.parametrize(
    "field",
    ["retires", "reason", "revises", "source", "citation_role", "existing_claim_dispositions"],
)
def test_a_mapping_cannot_supply_retirement_or_lineage_commands(field):
    with pytest.raises(ValueError, match="Extra inputs"):
        ClaimTemplate.model_validate(
            {
                "type": ArtifactNameRef(
                    target=ArtifactIdentity(kind="ClaimType", name=delivery.PREDICATE)
                ),
                "value": "high",
                field: "provider-made command",
            }
        )


@pytest.mark.parametrize("policy", ["surface", "mirror", "inverse"])
def test_absence_policies_are_typed_template_choices(policy):
    mapping = ClaimTemplate.model_validate(
        {
            "type": ArtifactNameRef(
                target=ArtifactIdentity(kind="ClaimType", name=delivery.PREDICATE)
            ),
            "value": "high",
            "when_absent": policy,
        }
    )
    assert mapping.when_absent == policy
