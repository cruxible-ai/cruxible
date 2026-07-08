"""Tests for support-signal evidence guardrails."""

from __future__ import annotations

from datetime import datetime, timezone

from cruxible_core.config.schema import ProposalPolicySchema, SignalPolicySchema
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.group.governance import check_auto_resolve_signals, derive_review_priority
from cruxible_core.group.types import (
    CandidateMember,
    CandidateSignal,
    GroupResolution,
    QuerySourceEvidence,
)


def _trusted_resolution() -> GroupResolution:
    return GroupResolution(
        resolution_id="RES-test",
        relationship_type="fits",
        group_signature="sig",
        action="approve",
        trust_status="trusted",
        resolved_at=datetime.now(timezone.utc),
    )


def _member(
    *,
    signal: CandidateSignal | None = None,
    source_query_evidence: list[QuerySourceEvidence] | None = None,
) -> CandidateMember:
    return CandidateMember(
        from_type="Shoe",
        from_id="shoe-1",
        to_type="Outfit",
        to_id="outfit-1",
        relationship_type="fits",
        signals=[signal or CandidateSignal(signal_source="source_v1", signal="support")],
        source_query_evidence=source_query_evidence or [],
    )


def _policy(*, require_evidence_on_support: bool) -> ProposalPolicySchema:
    return ProposalPolicySchema(
        signals={
            "source_v1": SignalPolicySchema(
                role="required",
                require_evidence_on_support=require_evidence_on_support,
            )
        }
    )


def _flagged_policy() -> ProposalPolicySchema:
    return _policy(require_evidence_on_support=True)


def test_unevidenced_support_requires_review_when_flagged() -> None:
    members = [_member()]

    assert (
        derive_review_priority(
            members,
            _flagged_policy(),
            _trusted_resolution(),
        )
        == "review"
    )


def test_whitespace_only_evidence_requires_review_when_flagged() -> None:
    members = [
        _member(
            signal=CandidateSignal(
                signal_source="source_v1",
                signal="support",
                evidence="  \t  ",
            )
        )
    ]

    assert (
        derive_review_priority(
            members,
            _flagged_policy(),
            _trusted_resolution(),
        )
        == "review"
    )


def test_support_evidenced_by_refs_passes_both_gates() -> None:
    members = [
        _member(
            signal=CandidateSignal(
                signal_source="source_v1",
                signal="support",
                evidence_refs=[EvidenceRef(source="scanner", source_record_id="finding-1")],
            )
        )
    ]
    policy = _flagged_policy()

    assert derive_review_priority(members, policy, _trusted_resolution()) == "normal"
    assert check_auto_resolve_signals(members, policy) is True


def test_support_evidenced_by_text_passes_both_gates() -> None:
    members = [
        _member(
            signal=CandidateSignal(
                signal_source="source_v1",
                signal="support",
                evidence="catalog row matched make/model/year",
            )
        )
    ]
    policy = _flagged_policy()

    assert derive_review_priority(members, policy, _trusted_resolution()) == "normal"
    assert check_auto_resolve_signals(members, policy) is True


def test_source_query_evidence_does_not_satisfy_review_priority_guard() -> None:
    members = [
        _member(
            source_query_evidence=[
                QuerySourceEvidence(
                    query_receipt_id="RCP-query000001",
                    row_index=0,
                    source_step="source_v1",
                )
            ]
        )
    ]

    assert (
        derive_review_priority(
            members,
            _flagged_policy(),
            _trusted_resolution(),
        )
        == "review"
    )


def test_source_query_evidence_does_not_satisfy_auto_resolve_guard() -> None:
    members = [
        _member(
            source_query_evidence=[
                QuerySourceEvidence(
                    query_receipt_id="RCP-query000001",
                    row_index=0,
                    source_step="source_v1",
                )
            ]
        )
    ]

    assert check_auto_resolve_signals(members, _flagged_policy()) is False


def test_unevidenced_support_blocks_auto_resolve_when_flagged() -> None:
    members = [_member()]

    assert check_auto_resolve_signals(members, _flagged_policy()) is False


def test_flag_off_preserves_current_support_behavior() -> None:
    members = [_member()]
    policy = _policy(require_evidence_on_support=False)

    assert derive_review_priority(members, policy, _trusted_resolution()) == "normal"
    assert check_auto_resolve_signals(members, policy) is True
