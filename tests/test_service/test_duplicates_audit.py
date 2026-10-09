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
    distinct = identity_claim_type(
        "distinct_from",
        canonical_form=None,
        object_kind="subject",
        cardinality="many",
        object_kinds=(KIND, "semantic.subject"),
        predicate="semantic.distinct_from",
        subject_kinds=("semantic.subject",),
    )
    accept_tree(
        instance, {claim_type_path(distinct.predicate): render_claim_type(distinct)}, name="df"
    )
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
