"""Discovery consumes immutable Claim inputs without materializing unused facts."""

from datetime import UTC, datetime

import pytest

from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.claims import claims as playbill_claims
from cruxible_core.service.discovery import search as playbill_search
from cruxible_core.service.evidence import evidence as playbill_evidence
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from tests.core_support._knowledge_loop_support import seed_claims
from tests.test_evidence.test_evidence_freshness import _fresh_world
from tests.test_integration.test_playbill_search import EVALUATION_TIME, _request


def test_discovery_matches_full_view_inputs_without_building_them(tmp_path, monkeypatch):
    instance, _ = seed_claims(tmp_path)
    expected = tuple(
        playbill_claims._claim_from_view(v)
        for v in playbill_claims.service_list_playbill_claims(instance, include_retired=True).claims
    )
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    assert sorted(context.claims(), key=lambda c: c.identity.name) == sorted(
        expected, key=lambda c: c.identity.name
    )
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    for claim in expected:
        args = dict(claim_identity=claim.identity.qualified, evaluation_time=EVALUATION_TIME, at=at)
        assert playbill_evidence.service_evaluate_playbill_claim_verdict(
            instance, **args
        ) == playbill_evidence.service_evaluate_playbill_claim_verdict(
            instance, read_context=context, **args
        )

    def unused(*args, **kwargs):
        pytest.fail("discovery must not materialize full Claim fact views")

    monkeypatch.setattr(playbill_claims, "projected_playbill_claim_views", unused)
    monkeypatch.setattr(instance, "blob_at", unused)
    calls = []
    providers = playbill_evidence.accepted_claim_providers

    def counted(tree):
        calls.append(len(tree))
        return providers(tree)

    monkeypatch.setattr(playbill_evidence, "accepted_claim_providers", counted)
    playbill_search.reset_claim_resolution_memo()
    result = playbill_search.service_search_playbill(
        instance, request=_request(instance, mode="list", kinds=("claim",))
    )
    assert {r.identity for r in result.rows} == {c.identity.name for c in expected}
    assert calls and len(calls) == 1
    playbill_search.reset_claim_resolution_memo()
    calls.clear()
    result = playbill_search.service_search_playbill(
        instance, request=_request(instance, mode="orient", kinds=("procedure",))
    )
    assert not calls
    assert result.orientation.counts_by_kind[0].count == 0


def test_batch_context_rechecks_time_and_replay_and_rejects_wrong_coordinate(tmp_path, monkeypatch):
    instance, identity = _fresh_world(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    before = datetime(2026, 8, 16, 20, 0, 9, tzinfo=UTC)
    expired = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    calls = []
    replay = playbill_evidence._current_replay_available

    def counted(*args, **kwargs):
        calls.append(args[1])
        return replay(*args, **kwargs)

    monkeypatch.setattr(playbill_evidence, "_current_replay_available", counted)
    for instant in (before, expired):
        args = dict(claim_identity=identity, evaluation_time=instant)
        expected = playbill_evidence.service_evaluate_playbill_claim_verdict(instance, **args)
        boundaries = set()
        result = playbill_evidence.service_evaluate_playbill_claim_verdict(
            instance, read_context=context, time_boundaries=boundaries, **args
        )
        assert result == expected
        assert expired in boundaries
    assert len(calls) == 4
    monkeypatch.setattr(playbill_evidence, "_current_replay_available", lambda *a, **k: False)
    args = dict(claim_identity=identity, evaluation_time=before)
    assert playbill_evidence.service_evaluate_playbill_claim_verdict(
        instance, read_context=context, **args
    ) == playbill_evidence.service_evaluate_playbill_claim_verdict(instance, **args)
    old = instance.coordinate_for_oid(instance.accepted_history()[-2].oid)
    with pytest.raises(playbill_evidence.ProposalIntegrityError, match="read context differs"):
        playbill_evidence.service_evaluate_playbill_claim_verdict(
            instance, read_context=context, at=PlaybillAcceptedCoordinate.from_internal(old), **args
        )
