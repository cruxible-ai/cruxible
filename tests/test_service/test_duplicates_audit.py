"""The post-hoc duplicates audit: equal under a broad comparison, distinct as declared."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.duplicates import DuplicateSubjectGroup, DuplicateValueGroup
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.duplicates import broad_key, service_duplicates_audit
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._identity_support import (
    KIND,
    OWNER,
    accept_tree,
    identity_claim_type,
    seed_identity_surface,
)

ALPHA, BETA = f"{KIND}/alpha", f"{KIND}/beta"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_identity_surface(tmp_path)


def write(instance: PlaybillInstance, *changes: dict[str, Any]) -> None:
    request = WriteRequest.model_validate({"because": "Seen.", "changes": list(changes)})
    outcome = service_playbill_write(
        instance, request=request, caller=WriteCaller(actor=OWNER, may_activate=True)
    )
    assert outcome.status == "accepted", outcome


def audit(instance: PlaybillInstance, **request: Any) -> Any:
    return service_duplicates_audit(instance, request={"kind": KIND, **request})


def test_the_broad_comparison_folds_case_punctuation_and_space() -> None:
    assert broad_key(" Lodash.JS ") == broad_key("lodash-js") == "lodashjs"
    assert broad_key("web app") != broad_key("webapp")


def test_subjects_whose_names_meet_and_values_that_meet_are_listed(
    instance: PlaybillInstance,
) -> None:
    write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "Lodash.JS"},
        {"op": "add", "subject": BETA, "field": "semantic.alias", "value": "lodash-js"},
        {"op": "add", "subject": ALPHA, "field": "tags", "value": "Web.App"},
        {"op": "add", "subject": BETA, "field": "tags", "value": "webapp"},
    )
    result = audit(instance)
    subjects = [group for group in result.groups if isinstance(group, DuplicateSubjectGroup)]
    values = [group for group in result.groups if isinstance(group, DuplicateValueGroup)]
    assert [(group.key, [item.subject for item in group.subjects]) for group in subjects] == [
        ("lodashjs", [ALPHA, BETA])
    ]
    assert subjects[0].subjects[0].names == ("lodash.js",)
    assert subjects[0].resolutions == ("alias_and_retire", "distinct_from")
    (tags,) = values
    assert (tags.predicate, tags.key, tags.canonical_form) == (
        f"{KIND}.tags",
        "webapp",
        ("trim", "casefold"),
    )
    assert sorted(item.value for item in tags.values) == ["web.app", "webapp"]
    assert (result.scanned.subjects, result.scanned.aliases) == (2, 2)
    assert result.scanned.values == 2 and not result.budget_exhausted


def test_asserting_distinct_from_suppresses_the_pair(instance: PlaybillInstance) -> None:
    _distinct_from(instance)
    write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "lodash.js"},
        {"op": "add", "subject": BETA, "field": "semantic.alias", "value": "lodash-js"},
    )
    assert audit(instance).groups
    write(
        instance, {"op": "add", "subject": ALPHA, "field": "semantic.distinct_from", "value": BETA}
    )
    assert audit(instance).groups == ()


def test_the_audit_pages_with_a_cursor(instance: PlaybillInstance) -> None:
    write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "one.x"},
        {"op": "add", "subject": BETA, "field": "semantic.alias", "value": "onex"},
        {"op": "add", "subject": ALPHA, "field": "tags", "value": "a.b"},
        {"op": "add", "subject": BETA, "field": "tags", "value": "ab"},
    )
    first = audit(instance, limit=1)
    assert len(first.groups) == 1 and first.truncated and first.next_cursor is not None
    second = audit(instance, limit=1, cursor=first.next_cursor)
    assert len(second.groups) == 1 and not second.truncated
    assert second.groups[0] != first.groups[0]


def test_an_unknown_kind_refuses_with_the_nearest(instance: PlaybillInstance) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        service_duplicates_audit(instance, request={"kind": "inv.packag"})
    assert refused.value.error_code == "cruxible.query.unknown_kind"


def test_the_duplicate_statement_detector_compares_canonical_statements(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.curation.curation_detectors import _duplicate_statements

    write(instance, {"op": "set", "subject": ALPHA, "field": "plain", "value": "Foo"})
    write(
        instance,
        {"op": "set", "subject": ALPHA, "field": "plain", "value": "foo", "contend": True},
    )
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    detections, _coverage = _duplicate_statements(tree=tree, generation=1)
    assert detections == ()
    # Under a form that folds case the two statements are one.
    folded = identity_claim_type(
        "plain", canonical_form=("casefold",), literal_schema={"type": "string"}
    )
    tree[claim_type_path(folded.predicate)] = render_claim_type(folded)
    (detection,) = _duplicate_statements(tree=tree, generation=1)[0]
    assert len(detection.evidence_refs) == 2


# -- review F-008 and F-009 -------------------------------------------------------


def _distinct_from(instance: PlaybillInstance) -> None:
    """A new instance seeds the builtin semantic.distinct_from: nothing to accept."""

    assert claim_type_path("semantic.distinct_from") in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )


def test_a_third_subject_never_rejoins_an_asserted_distinct_pair(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.subjects import render_subject, subject_path
    from tests.core_support._identity_support import shell

    _distinct_from(instance)
    accept_tree(
        instance, {subject_path(KIND, "gamma"): render_subject(shell("gamma"))}, name="gamma"
    )
    write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "one.x"},
        {"op": "add", "subject": BETA, "field": "semantic.alias", "value": "onex"},
        {"op": "add", "subject": f"{KIND}/gamma", "field": "semantic.alias", "value": "one-x"},
        {"op": "add", "subject": ALPHA, "field": "semantic.distinct_from", "value": BETA},
    )
    groups = [group for group in audit(instance).groups if isinstance(group, DuplicateSubjectGroup)]
    assert groups, "alpha and gamma still meet"
    for group in groups:
        members = {item.subject for item in group.subjects}
        assert not {ALPHA, BETA} <= members, members
    assert [[item.subject for item in group.subjects] for group in groups] == [
        [ALPHA, f"{KIND}/gamma"]
    ]


def test_grouping_never_enumerates_all_pairs_for_one_sparse_distinct_pair() -> None:
    from cruxible_core.service.discovery.duplicates import _groups_apart

    holders = [f"subjects/k/{index:05d}.json" for index in range(5000)]
    apart = {holders[0]: {holders[1]}, holders[1]: {holders[0]}}
    groups = _groups_apart(holders, apart)
    # The second holder opens its own group, alone, so it is not a duplicate group.
    assert groups == [[holders[0], *holders[2:]]]


def test_the_budget_is_applied_in_each_read_with_one_overflow_sentinel(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.discovery import duplicates

    for tag in ("a.x", "ax", "b.y", "by"):
        write(instance, {"op": "add", "subject": ALPHA, "field": "tags", "value": tag})
    full = audit(instance)
    assert full.scanned.values == 4 and not full.budget_exhausted
    reads: list[int] = []
    original = duplicates._Budget.read

    def counted(self, connection, sql, parameters):  # type: ignore[no-untyped-def]
        rows = original(self, connection, sql, parameters)
        reads.append(len(rows))
        return rows

    monkeypatch.setattr(duplicates._Budget, "read", counted)
    monkeypatch.setattr(duplicates, "DUPLICATES_AUDIT_MAX_SCANNED", 3)
    bounded = audit(instance)
    assert bounded.budget_exhausted
    scanned = bounded.scanned
    # Every row the audit read is counted, and together they never pass the bound.
    assert sum(reads) == scanned.subjects + scanned.aliases + scanned.relations + scanned.values
    assert sum(reads) <= 3


# -- key-hint groups (identity D4) ------------------------------------------------

PART = "dev.part"


def _author(instance: PlaybillInstance, *members: Any) -> None:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput, lower_authoring_input
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER,
        payload=lower_authoring_input(ChangeSetInput(kind="change_set", members=members)),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt


def _two_holders_of_one_hint_key(instance: PlaybillInstance) -> tuple[str, str]:
    from cruxible_client.contracts.authoring.inputs import (
        ClaimTypeInput,
        SubjectInput,
        SubjectKindInput,
    )
    from tests.core_support._subject_kind_support import slug_claim_type

    _author(
        instance,
        ClaimTypeInput(kind="claim_type", claim_type=slug_claim_type(PART)),  # type: ignore[arg-type]
        SubjectKindInput(kind="subject_kind", name=PART, keys=(f"{PART}.slug",), uniqueness="hint"),
        SubjectInput(kind="subject", subject_kind=PART, key="Widget", rationale="A part."),
    )
    _author(
        instance,
        SubjectInput(
            kind="subject",
            subject_kind=PART,
            key="widget",
            rationale="Another part of that name.",
            create_new=True,
        ),
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    first, second = sorted(
        path.removeprefix("subjects/").removesuffix(".json")
        for path in tree
        if path.startswith(f"subjects/{PART}/")
    )
    return first, second


def test_subjects_of_a_hint_kind_holding_one_key_form_a_group(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.duplicates import DuplicateKeyGroup

    first, second = _two_holders_of_one_hint_key(instance)
    assert first == f"{PART}/widget" and second.startswith(f"{PART}/widget-")

    result = service_duplicates_audit(instance, request={"kind": PART})
    (group,) = [item for item in result.groups if isinstance(item, DuplicateKeyGroup)]
    assert [(part.predicate, part.value) for part in group.key] == [(f"{PART}.slug", "widget")]
    assert set(group.subjects) == {first, second}
    assert group.resolutions == ("alias_and_retire", "distinct_from")
    assert result.scanned.keys == 2

    # Asserting them distinct suppresses the group.
    write(
        instance,
        {"op": "add", "subject": first, "field": "semantic.distinct_from", "value": second},
    )
    result = service_duplicates_audit(instance, request={"kind": PART})
    assert not [item for item in result.groups if isinstance(item, DuplicateKeyGroup)]


def test_a_unique_kind_lists_no_key_group(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.duplicates import DuplicateKeyGroup

    result = audit(instance)
    assert not [item for item in result.groups if isinstance(item, DuplicateKeyGroup)]
    assert result.scanned.keys == 0
