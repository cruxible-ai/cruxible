"""A Procedure's mirror retirement never retires a judgment that pins the retired Claim.

A KEV import mirrors the catalog: a record the source stops listing retires its
``listed`` Claim. A judgment authored ``based-on`` that Claim (compiler revision
74) is provenance, not a dependent: it stays live, and ``next`` lists it as
``claim_dependency_stale``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import BASED_ON_CLAIM_PIN_ROLE, claim_path, parse_claim
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from tests.core_support._identity_support import OWNER, accept_tree, identity_claim_type
from tests.test_integration.test_next_closed_loop import _access
from tests.test_pilot.support import KEV, KIND, pilot_world
from tests.test_procedures.test_claims_absence import absence_spec, delist, proposed_claims
from tests.test_procedures.test_claims_terminal import activate, run

TRIAGE = KIND + ".triage_note"
AT = datetime.fromisoformat("2026-11-08T00:00:00+00:00")


def _claims(instance, predicate):
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return [
        parse_claim(content, path=path)
        for path, content in tree.items()
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate == predicate
    ]


def test_mirror_retirement_leaves_a_based_on_judgment_live_and_stale(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec("mirror"))
    triage = identity_claim_type(
        "triage_note", canonical_form=None, predicate=TRIAGE, subject_kinds=(KIND,)
    )
    accept_tree(
        instance,
        {claim_type_path(TRIAGE): render_claim_type(triage)},
        name="triage-type",
    )
    first = run(instance, root)
    assert first.status == "succeeded"
    activate(instance, first)
    listed = sorted(
        (claim for claim in _claims(instance, KEV + "listed") if claim.lifecycle.state == "live"),
        key=lambda claim: claim.identity.name,
    )
    assert listed
    target = listed[0]
    subject = target.statement.subject.artifact_path.removeprefix("subjects/").removesuffix(".json")
    judged = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {
                "because": "Triaged from the KEV listing.",
                "changes": [
                    {
                        "op": "set",
                        "subject": subject,
                        "field": TRIAGE,
                        "value": "patch now",
                        "based_on": [target.identity.name],
                    }
                ],
            }
        ),
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )
    assert judged.status == "accepted", judged.refusal
    (judgment,) = _claims(instance, TRIAGE)
    assert any(
        pin.role == BASED_ON_CLAIM_PIN_ROLE and pin.target == target.identity
        for pin in judgment.pins
    )

    delist(root)
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    retiring = proposed_claims(instance, second)
    assert target.identity in {claim.identity for claim in retiring}
    # The mirror proposal retires only the listed Claims; the judgment is not swept in.
    assert judgment.identity not in {claim.identity for claim in retiring}
    activate(instance, second)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    target_path = claim_path(target.identity.name)
    retired = parse_claim(tree[target_path], path=target_path)
    assert retired.lifecycle.state == "retired"
    judgment_path = claim_path(judgment.identity.name)
    after = parse_claim(tree[judgment_path], path=judgment_path)
    assert after.lifecycle.state == "live"
    found = service_playbill_next(
        instance, request=NextRequest(evaluation_time=AT, access_profile=_access())
    )
    rows = [row for row in found.items if row.reason == "claim_dependency_stale"]
    assert [row.subject_identity for row in rows] == [judgment.identity.qualified]
    (entry,) = rows[0].detail["stale_inputs"]
    assert entry["source_claim_identity"] == target.identity.qualified
    assert entry["pin_role"] == BASED_ON_CLAIM_PIN_ROLE
