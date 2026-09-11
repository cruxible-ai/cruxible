"""Uncommitted evaluation records are not pending or resolving proposals."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cruxible_core.service.discovery import curation as playbill_curation
from cruxible_core.service.evidence import source_catalog


@pytest.mark.parametrize(
    ("admitted", "settled", "expected"),
    [(False, False, {}), (True, False, {"guide": {"body-digest"}}), (True, True, {})],
)
def test_pending_documents_require_an_unsettled_admission(
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
    settled: bool,
    expected: dict[str, set[str]],
) -> None:
    evaluation = SimpleNamespace(
        proposal_id="proposal",
        verdict="candidate",
        evaluated_tree_oid="tree",
        candidate_digest="candidate",
    )
    evidence = Mock()
    evidence.list_admissions.return_value = (
        (SimpleNamespace(proposal_id="proposal"),) if admitted else ()
    )
    evidence.list_evaluations.return_value = (evaluation,)
    evidence.read_candidate.return_value = SimpleNamespace(
        members=(SimpleNamespace(artifact_kind="document", path="documents/guide.json"),)
    )
    instance = Mock()
    instance.proposal_evidence.return_value = evidence
    instance.accepted_history.return_value = (
        (SimpleNamespace(record=SimpleNamespace(candidate_digest="candidate")),) if settled else ()
    )
    instance.proposal_tree.return_value = {"documents/guide.json": b"document"}
    parse = Mock(return_value=SimpleNamespace(document_id="guide", body_digest="body-digest"))
    monkeypatch.setattr(source_catalog, "parse_document", parse)

    assert source_catalog._pending_body_digests(instance) == expected

    if expected:
        evidence.read_candidate.assert_called_once_with("candidate")
        instance.proposal_tree.assert_called_once_with("tree")
        parse.assert_called_once_with(b"document", path="documents/guide.json")
    else:
        evidence.read_candidate.assert_not_called()
        instance.proposal_tree.assert_not_called()
        parse.assert_not_called()


@pytest.mark.parametrize("admitted", [False, True])
def test_curation_never_names_an_orphan_evaluation_as_the_resolving_proposal(
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
) -> None:
    # Both evaluations name the accepted candidate. The orphan sorts first,
    # so selecting from raw evaluations would misattribute this resolution.
    evidence = Mock()
    evidence.list_admissions.return_value = (
        (SimpleNamespace(proposal_id="b-admitted"),) if admitted else ()
    )
    evidence.list_evaluations.return_value = tuple(
        SimpleNamespace(proposal_id=name, verdict="candidate", candidate_digest="candidate")
        for name in ("a-orphan", "b-admitted")
    )
    accepted_record = SimpleNamespace(candidate_digest="candidate")
    instance = Mock()
    instance.proposal_evidence.return_value = evidence
    instance.accepted_history.return_value = (
        SimpleNamespace(oid="parent", record=None, sequence=1),
        SimpleNamespace(oid="accepted", record=accepted_record, sequence=2),
    )
    instance.tree_at.side_effect = [{"parent": b"tree"}, {"accepted": b"tree"}]
    item = SimpleNamespace(
        item_id="item",
        first_proposed_generation=1,
        subject="Claim:retired",
        latest_evidence_refs=(SimpleNamespace(path="claims/retired.json"),),
    )
    affected = (SimpleNamespace(path="claims/retired.json", disposition="retire"),)
    members = Mock(return_value=affected)
    monkeypatch.setattr(playbill_curation, "_affected_members", members)
    monkeypatch.setattr(playbill_curation, "dependency_artifacts", Mock(return_value=()))

    resolved = playbill_curation._accepted_retirements_for_items(instance, (item,))

    if admitted:
        assert resolved == {"item": (2, "b-admitted", accepted_record, affected)}
        assert instance.tree_at.call_count == 2
    else:
        assert resolved == {}
        instance.tree_at.assert_not_called()
        members.assert_not_called()
