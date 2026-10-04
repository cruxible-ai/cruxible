"""A write refused by a ClaimType's literal schema names the bound it broke and what it got."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.write import PlaybillWriteRequest
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, OWNER, _claim_type, caller, seed_write_surface

WI1 = f"{KIND}/wi-1"

BOUNDED = (
    _claim_type("headline", literal_schema={"type": "string", "minLength": 3, "maxLength": 200}),
    _claim_type("code", literal_schema={"type": "string", "pattern": "^[A-Z]{2}-[0-9]+$"}),
    _claim_type("grade", literal_schema={"type": "integer", "enum": [1, 2, 3]}),
    _claim_type("score", literal_schema={"type": "integer", "minimum": 1, "maximum": 10}),
)


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    seeded, _owner = seed_write_surface(tmp_path_factory.mktemp("bounds"))
    base = seeded.accepted_coordinate()
    tree = seeded.tree_at(base.git_oid)
    for claim_type in BOUNDED:
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    proposed = seeded.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/bounded-types", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:45.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        seeded, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    return seeded


def _refused(instance: PlaybillInstance, field: str, value: object) -> Any:
    request = PlaybillWriteRequest.model_validate(
        {
            "because": "The writer checked it.",
            "changes": [{"op": "set", "subject": WI1, "field": field, "value": value}],
        }
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.refusal is not None, outcome
    assert outcome.refusal.code == "playbill.write.value_type_mismatch"
    assert outcome.refusal.field_path == "changes[0].value"
    return outcome.refusal


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("headline", "x" * 230, "maxLength 200, got 230"),
        ("headline", "ab", "minLength 3, got 2"),
        ("code", "ab-12", 'pattern "^[A-Z]{2}-[0-9]+$", got "ab-12"'),
        ("grade", 7, "enum [1, 2, 3], got 7"),
        ("score", 0, "minimum 1, got 0"),
        ("score", 11, "maximum 10, got 11"),
        ("headline", 5, "type string, got integer 5"),
    ],
)
def test_the_refusal_names_the_violated_bound_and_the_value(
    instance: PlaybillInstance, field: str, value: object, named: str
) -> None:
    refusal = _refused(instance, field, value)
    assert refusal.message.endswith(f"for {field}: {named}"), refusal.message
    assert named in refusal.repair


def test_a_long_refused_value_is_quoted_short(instance: PlaybillInstance) -> None:
    refusal = _refused(instance, "headline", "y" * 230)
    assert "y" * 100 not in refusal.message
    assert refusal.message.startswith("'yyy") and "…" in refusal.message


def test_a_value_within_every_bound_is_written(instance: PlaybillInstance) -> None:
    request = PlaybillWriteRequest.model_validate(
        {
            "because": "The writer checked it.",
            "changes": [
                {"op": "set", "subject": WI1, "field": "headline", "value": "x" * 200},
                {"op": "set", "subject": WI1, "field": "score", "value": 10},
                {"op": "set", "subject": WI1, "field": "code", "value": "AB-12"},
                {"op": "set", "subject": WI1, "field": "grade", "value": 2},
            ],
        }
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.refusal is None, outcome
