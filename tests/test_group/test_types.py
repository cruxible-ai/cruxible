"""Tests for group runtime model helpers."""

from __future__ import annotations

from cruxible_core.group.types import CandidateMember, CandidateSignal, QuerySourceEvidence


def test_candidate_member_as_relationship_strips_governance_fields() -> None:
    member = CandidateMember(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        properties={"verified": True},
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        source_query_evidence=[
            QuerySourceEvidence(query_receipt_id="RCT-1", row_index=0),
        ],
        evidence_rationale="candidate rationale",
    )

    relationship = member.as_relationship()

    assert type(relationship).__name__ == "RelationshipInstance"
    assert relationship.identity_tuple() == member.identity_tuple()
    assert relationship.properties == {"verified": True}
    assert relationship.properties is not member.properties
    dumped = relationship.model_dump()
    assert "signals" not in dumped
    assert "source_query_evidence" not in dumped
    assert "evidence_refs" not in dumped
    assert "evidence_rationale" not in dumped
