"""A key Claim core writes when it creates a Subject is covered by the creation itself.

Claim law v2 revision 9: the key Claims a Subject creation writes (an authoring
subject member, a write verb's create-by-key, a Procedure Subject item, a settle
creation) are supported on their origin, whatever their key ClaimType's
evidence rules; a key ClaimType shaped like ``authoring example subject-kind``
(no evidence rule at all) no longer leaves them ``uncovered``. Only those key
Claims, only at creation: a later revision of a key Claim is judged by its
ClaimType, and revision 8 keeps judging the same creation exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    ClaimTypeInput,
    SubjectInput,
    SubjectKindInput,
    lower_authoring_input,
)
from cruxible_client.contracts.claim_reads import ClaimValuesRequest
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.claims.claim_reads import service_read_claim_values
from tests.core_support._identity_support import OWNER, identity_claim_type
from tests.core_support._subject_kind_support import bare_slug_claim_type
from tests.core_support._support import initialize_local
from tests.test_service.test_subject_identity import accept, change_set

KIND = "security.vulnerability"
SLUG = f"{KIND}.slug"
TITLE = f"{KIND}.title"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    """A kind keyed by a slug ClaimType with no evidence rules, as the example spells it."""

    instance, _owner = initialize_local(tmp_path)
    slug = bare_slug_claim_type(KIND)
    assert not slug.evidence_admission_policy.rules  # type: ignore[attr-defined]
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=slug),  # type: ignore[arg-type]
            ClaimTypeInput(
                kind="claim_type",
                claim_type=identity_claim_type(
                    "title",
                    canonical_form=None,
                    literal_schema={"type": "string"},
                    predicate=TITLE,
                    subject_kinds=(KIND,),
                ),
            ),
            SubjectKindInput(kind="subject_kind", name=KIND, keys=(SLUG,), uniqueness="unique"),
        ),
    )
    return instance


def verdicts(instance: PlaybillInstance, subject_id: str, predicate: str) -> list[str]:
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequest(
            subject_paths=(subject_path(KIND, subject_id),), predicates=(predicate,)
        ),
    )
    return [row.verdict for row in result.values]


def write(instance: PlaybillInstance, *changes: dict[str, Any]) -> Any:
    return service_playbill_write(
        instance,
        request=WriteRequest.model_validate({"because": "Triaged.", "changes": list(changes)}),
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )


def test_an_authoring_subject_member_writes_a_supported_key_claim(
    instance: PlaybillInstance,
) -> None:
    accept(
        instance,
        lower_authoring_input(
            SubjectInput(kind="subject", subject_kind=KIND, key="CVE-2021-44228")
        ),
    )

    assert verdicts(instance, "cve-2021-44228", SLUG) == ["supported"]


def test_a_write_verb_create_by_key_writes_a_supported_key_claim(
    instance: PlaybillInstance,
) -> None:
    outcome = write(
        instance,
        {"op": "set", "subject": f"{KIND}/CVE-2021-44228", "field": "title", "value": "Log4Shell"},
    )

    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == (f"{KIND}/cve-2021-44228",)
    assert verdicts(instance, "cve-2021-44228", SLUG) == ["supported"]
    assert verdicts(instance, "cve-2021-44228", TITLE) == ["supported"]


def test_a_write_create_change_writes_a_supported_key_claim(
    instance: PlaybillInstance,
) -> None:
    outcome = write(
        instance,
        {"op": "create", "label": "cve", "subject_kind": KIND, "key": "CVE-2021-44228"},
        {"op": "set", "subject": {"created": "cve"}, "field": "title", "value": "Log4Shell"},
    )

    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].subject == f"{KIND}/cve-2021-44228"
    assert verdicts(instance, "cve-2021-44228", SLUG) == ["supported"]
    assert verdicts(instance, "cve-2021-44228", TITLE) == ["supported"]


def test_a_later_revision_of_a_key_claim_still_needs_admitted_evidence(
    instance: PlaybillInstance,
) -> None:
    created = write(
        instance, {"op": "set", "subject": f"{KIND}/CVE-2021-44228", "field": "title", "value": "L"}
    )
    assert created.status == "accepted", created

    revised = write(
        instance,
        {"op": "set", "subject": f"{KIND}/cve-2021-44228", "field": "slug", "value": "log4shell"},
    )

    assert revised.status == "accepted", revised
    assert revised.changes[0].revises is not None
    # The creation covered only the first key Claim: its revision cites the
    # writer's words, which the key ClaimType admits by no rule.
    assert revised.changes[0].verdict == "uncovered"
    assert verdicts(instance, "cve-2021-44228", SLUG) == ["uncovered"]


def _creation_candidate(instance: PlaybillInstance) -> tuple[Any, Any]:
    """The candidate tree a creation lowers to, over the head, as preflight builds it."""

    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator

    payload = lower_authoring_input(
        ChangeSetInput(
            kind="change_set",
            members=(SubjectInput(kind="subject", subject_kind=KIND, key="CVE-2014-0160"),),
        )
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )
    with instance.capture_verification_request():
        computed, _bound = coordinator._compute_and_bind_preflight(  # noqa: SLF001
            created.intent.intent_id, actor=OWNER
        )
    assert computed.lowered is not None, computed.result.frontier
    parent = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parent, {**parent, **dict(computed.lowered.changed_members)}


def _judged_verdicts(
    instance: PlaybillInstance, trees: Any, revision: Any | None
) -> dict[str, tuple[str, str]]:
    """Each Claim member's (law digest, initial verdict), judged at ``revision`` (or current)."""

    from cruxible_core.proposals.proposals import evaluate_proposal_tree

    parent, candidate = trees

    def judge(coordinates: dict[str, tuple[str, str]] | None) -> Any:
        judged = evaluate_proposal_tree(
            base_tree=parent,
            current_tree=parent,
            proposed_tree=candidate,
            current=instance.accepted_coordinate(),
            bodies=instance.body_store(),
            timestamp="2026-10-09T12:00:00.000000Z",
            actor_id="owner",
            rebased=False,
            historical_law_coordinates=coordinates,
        )
        assert judged.candidate is not None, judged.diagnostics
        return judged.candidate

    current = judge(None)
    if revision is not None:
        recorded = {
            member.path: (member.law_identifier, current.law_digests[member.law_identifier])
            for member in current.members
        }
        current = judge(
            {
                path: (revision.identifier, revision.digest)
                if law[0] == revision.identifier
                else law
                for path, law in recorded.items()
            }
        )
    found: dict[str, tuple[str, str]] = {}
    for evaluation in current.law_evidence:
        evidence = evaluation.result.get("claim_evidence")
        if evaluation.path.startswith("claims/") and isinstance(evidence, dict):
            found[evaluation.path] = (evidence["law_digest"], evidence["initial_verdict"])
    return found


def test_only_revisions_9_and_10_cover_a_key_claim_by_its_subjects_creation(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Differential over identical inputs: revision 8 keeps its meaning (uncovered).

    Revision 10 (re-bind, compiler revision 35) is the current Claim law v2 and
    keeps revision 9's creation coverage; an instance on compiler 33 or 34 judges
    a new Claim by revision 9, and one on an earlier compiler by revision 8.
    """

    from cruxible_client.contracts.laws import (
        CLAIM_LAW_V2_REVISION_8,
        CLAIM_LAW_V2_REVISION_9,
        CLAIM_LAW_V2_REVISION_10,
    )

    trees = _creation_candidate(instance)

    (current,) = _judged_verdicts(instance, trees, None).values()
    assert current == (CLAIM_LAW_V2_REVISION_10.digest, "supported")
    (nine,) = _judged_verdicts(instance, trees, CLAIM_LAW_V2_REVISION_9).values()
    assert nine == (CLAIM_LAW_V2_REVISION_9.digest, "supported")
    (eight,) = _judged_verdicts(instance, trees, CLAIM_LAW_V2_REVISION_8).values()
    assert eight == (CLAIM_LAW_V2_REVISION_8.digest, "uncovered")


def test_a_key_claim_about_an_existing_subject_is_not_covered(
    instance: PlaybillInstance,
) -> None:
    """Coverage needs the Subject's creation: the same Claim shape about a live Subject is not."""

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.claims.key_claims import is_subject_creation_key_claim

    parent, candidate = _creation_candidate(instance)
    (path,) = [item for item in candidate if item.startswith("claims/") and item not in parent]
    claim = parse_claim(candidate[path], path=path)

    assert is_subject_creation_key_claim(parent, candidate, claim, new_claim=True)
    # A revision (not new), or the same Subject already in the parent, is not a creation.
    assert not is_subject_creation_key_claim(parent, candidate, claim, new_claim=False)
    subject = claim.statement.subject.artifact_path
    held = {**parent, subject: candidate[subject]}
    assert not is_subject_creation_key_claim(held, candidate, claim, new_claim=True)
