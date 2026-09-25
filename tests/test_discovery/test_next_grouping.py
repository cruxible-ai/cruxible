"""The queue reports each underlying fact once, with every finding about it inside."""

from __future__ import annotations

from cruxible_core.service.discovery.next import (
    PlaybillNextRepairV1,
    _group_items,
    _item,
)


def _row(reason, subject, *, severity="warning", **detail):  # type: ignore[no-untyped-def]
    return _item(
        severity=severity,
        reason=reason,
        subject_identity=subject,
        detail=detail,
        repair=PlaybillNextRepairV1(operation="hand_edit", target=subject, required_change="x"),
    )


def test_one_block_is_one_row_at_its_most_severe_finding() -> None:
    stale = _row("projection_backing_stale", "block:a", severity="blocking")
    dirty = _row("projection_dirty", "block:a")
    other = _row("projection_dirty", "block:b")

    rows = {row.subject_identity: row for row in _group_items((stale, dirty, other))}

    assert set(rows) == {"block:a", "block:b"}
    assert rows["block:a"].reason == "projection_backing_stale"
    assert rows["block:a"].severity == "blocking"
    assert [finding.reason for finding in rows["block:a"].findings] == ["projection_dirty"]
    # A row that stands alone is byte-identical to before grouping existed.
    assert rows["block:b"] == other
    assert "findings" not in rows["block:b"].model_dump(mode="json")


def test_an_unobserved_source_is_one_row_however_many_citations_point_at_it() -> None:
    rows = _group_items(
        (
            _row("citation_source_unobserved", "Claim:a", source_id="repo.docs"),
            _row("citation_source_unobserved", "Claim:b", source_id="repo.docs"),
            _row("citation_source_unobserved", "Claim:c", source_id="repo.other"),
        )
    )

    assert sorted(len(row.findings) for row in rows) == [0, 1]
    (grouped,) = [row for row in rows if row.findings]
    assert {grouped.subject_identity, *grouped.related_identities} >= {"Claim:a", "Claim:b"}


def test_an_edited_document_carries_the_citations_its_edit_moved() -> None:
    edited = _row("document_modified", "document:guide", source_id="repo.guide")
    moved = _row("citation_drifted", "Claim:a", severity="repair", source_id="repo.guide")
    elsewhere = _row("citation_drifted", "Claim:b", severity="repair", source_id="repo.notes")

    rows = {row.subject_identity: row for row in _group_items((edited, moved, elsewhere))}

    assert rows["document:guide"].reason == "document_modified"
    assert rows["document:guide"].severity == "repair"
    assert [finding.subject_identity for finding in rows["document:guide"].findings] == ["Claim:a"]
    # A drifted citation into a document nobody edited keeps its own row.
    assert rows["Claim:b"] == elsewhere


def test_one_captured_piece_of_evidence_is_one_row_headed_by_its_strongest_stance() -> None:
    support = _row("claim_new_evidence_supporting", "Claim:a", capture_digest="sha256:" + "1" * 64)
    contradict = _row(
        "claim_contradicting_evidence_available",
        "Claim:a",
        severity="repair",
        capture_digest="sha256:" + "1" * 64,
    )

    (row,) = _group_items((support, contradict))

    assert row.reason == "claim_contradicting_evidence_available"
    assert [finding.reason for finding in row.findings] == ["claim_new_evidence_supporting"]


def test_supporting_evidence_alone_is_not_work() -> None:
    support = _row("claim_new_evidence_supporting", "Claim:a", capture_digest="sha256:" + "1" * 64)
    other = _row("claim_stale_evidence", "Claim:b")

    assert _group_items((support, other)) == (other,)


def test_supporting_evidence_rides_inside_the_first_row_it_would_resolve() -> None:
    support = _row("claim_new_evidence_supporting", "Claim:a", capture_digest="sha256:" + "1" * 64)
    uncovered = _row("claim_uncovered", "Claim:a")
    expiring = _row("evidence_expiring", "Claim:a")
    elsewhere = _row("claim_uncovered", "Claim:b")

    rows = {
        (row.reason, row.subject_identity): row
        for row in _group_items((uncovered, support, expiring, elsewhere))
    }

    carrier = rows[("evidence_expiring", "Claim:a")]
    assert [finding.reason for finding in carrier.findings] == ["claim_new_evidence_supporting"]
    assert carrier.severity == expiring.severity
    # Carried once: the other row it would resolve stays as it was.
    assert rows[("claim_uncovered", "Claim:a")] == uncovered
    assert rows[("claim_uncovered", "Claim:b")] == elsewhere


def test_supporting_evidence_reaches_a_resolvable_row_nested_in_a_conflict() -> None:
    from cruxible_core.service.discovery.next import _with_findings

    conflict = _with_findings(
        _row("claim_conflicted", "subjects/wi.json", severity="blocking"),
        [_row("claim_uncovered", "Claim:a")],
    )
    support = _row("claim_new_evidence_supporting", "Claim:a", capture_digest="sha256:" + "1" * 64)

    (row,) = _group_items((conflict, support))

    assert row.reason == "claim_conflicted"
    assert [finding.reason for finding in row.findings] == [
        "claim_uncovered",
        "claim_new_evidence_supporting",
    ]
