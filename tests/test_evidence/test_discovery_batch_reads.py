"""Discovery consumes immutable Claim inputs without materializing unused facts."""

from datetime import UTC, datetime

import pytest

from cruxible_core.indexes.sqlite import ProjectionHandle
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

    monkeypatch.setattr(ProjectionHandle, "list_claims", unused)
    monkeypatch.setattr(instance, "tree_at", unused)
    monkeypatch.setattr(instance, "immutable_tree_at", unused)
    allowed_sources = set(context._source_bytes)
    read_blob = instance.blob_at

    def selected_history(oid, path):
        assert path.startswith("changesets/") or path in allowed_sources
        return read_blob(oid, path)

    monkeypatch.setattr(instance, "blob_at", selected_history)
    calls = []
    providers = playbill_evidence.accepted_claim_providers

    def counted(instance, *, coordinate):
        calls.append(coordinate)
        return providers(instance, coordinate=coordinate)

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


def test_verdicts_verify_each_retained_record_once_per_instance(tmp_path, monkeypatch):
    from cruxible_core.indexes.history import history_index

    instance, _ = seed_claims(tmp_path)
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    parsed: list[str] = []
    parse = history_index.parse_change_set_record

    def counted(raw, *, path, **kwargs):
        parsed.append(path)
        return parse(raw, path=path, **kwargs)

    monkeypatch.setattr(history_index, "parse_change_set_record", counted)

    def statuses():
        context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
        assert context.history() is context.history()
        playbill_search.reset_claim_resolution_memo()
        return playbill_search.claim_resolution_statuses(
            instance,
            claims=context.claims(),
            at=at,
            evaluation_time=EVALUATION_TIME,
            read_context=context,
        )

    instance.verified_change_set_records.clear()
    first = statuses()
    assert parsed and len(parsed) == len(set(parsed))
    parsed.clear()
    # A new request at a new memo key reuses the verified records.
    assert statuses() == first
    assert parsed == []


def test_retained_records_answer_only_their_exact_location(tmp_path):
    from dataclasses import replace

    from cruxible_client.contracts.errors import ProjectionIntegrityError

    instance, _ = seed_claims(tmp_path)
    with instance.accepted_history_reader() as history:
        location = history.generation(history.sequence)
    record = instance.retained_record_reader().read(location)
    expected = record.model_copy(deep=True)
    # Each request gets a detached copy: mutating one cannot reach the next.
    record.law_evidence[0].result.clear()
    again = instance.retained_record_reader().read(location)
    assert again == expected and again is not record
    again.law_evidence[0].result.clear()
    assert instance.retained_record_reader().read(location) == expected
    retained = len(instance.verified_change_set_records)
    forged = replace(location, source_record_digest="sha256:" + "0" * 64)
    with pytest.raises(ProjectionIntegrityError, match="binding differs"):
        instance.retained_record_reader().read(forged)
    assert len(instance.verified_change_set_records) == retained


def test_subject_search_evaluates_only_that_subjects_claims(tmp_path, monkeypatch):
    instance, _ = seed_claims(tmp_path)
    everything = playbill_search.service_search_playbill(
        instance, request=_request(instance, mode="list", kinds=("claim",))
    )
    subject = next(
        r.subject for r in everything.rows if r.subject.artifact_path.endswith("wi-42.json")
    )
    evaluated: list[str] = []
    resolve = playbill_search.resolve_playbill_claim_group

    def counted(instance, *, claims, **kwargs):
        evaluated.extend(claim.statement.subject.artifact_path for claim in claims)
        return resolve(instance, claims=claims, **kwargs)

    monkeypatch.setattr(playbill_search, "resolve_playbill_claim_group", counted)
    from cruxible_core.indexes.typed_state import TypedStateReader

    selections: list[tuple[tuple[str, str], ...] | None] = []
    attestations = TypedStateReader.claim_attestations

    def selected(reader, *args, **kwargs):
        selections.append(kwargs.get("claim_versions"))
        return attestations(reader, *args, **kwargs)

    monkeypatch.setattr(TypedStateReader, "claim_attestations", selected)
    playbill_search.reset_claim_resolution_memo()
    scoped = playbill_search.service_search_playbill(
        instance, request=_request(instance, mode="list", kinds=("claim",), subject=subject)
    )
    assert scoped.rows == tuple(r for r in everything.rows if r.subject == subject)
    assert evaluated and set(evaluated) == {subject.artifact_path}
    # Attestations are selected for this subject's exact Claim versions only,
    # never read for the whole population.
    if playbill_evidence._serves_attestations(instance.accepted_coordinate()):
        scoped_versions = {(f"Claim:{row.identity}", row.subject) for row in scoped.rows}
        assert selections and None not in selections
        assert {pair[0] for chunk in selections for pair in chunk} == {
            identity for identity, _ in scoped_versions
        }
