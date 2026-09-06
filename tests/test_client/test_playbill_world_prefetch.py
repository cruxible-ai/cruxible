"""Explicit prefetch avoids request fan-out without caching incomplete selections."""

from datetime import UTC, datetime
from typing import Any

import pytest

from cruxible_client import Playbill
from cruxible_client.authoring.world import WorldStructureError
from cruxible_client.contracts.claim_reads import ClaimReadBatchResultV1
from tests.test_client.test_playbill_sdk_world import (
    _COORDINATE,
    _MOVED_COORDINATE,
    SEVERITY,
    _workspace,
    _WorldClient,
)


@pytest.fixture
def connection(tmp_path):
    _workspace(tmp_path)
    client = _WorldClient()
    pb = Playbill._from_client(
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    return pb, client


def install(client: _WorldClient, monkeypatch, *, paged=False, invalid=False):
    calls = []

    def batch(_instance_id: str, *, request: Any):
        calls.append(request)
        view = client.get_playbill_claim(
            _instance_id, "Claim:CLM-" + ("8" if request.cursor else "9") * 32
        )
        client.claim_reads.pop()  # fixture construction is not a transport request
        return ClaimReadBatchResultV1(
            coordinate=_MOVED_COORDINATE if invalid else _COORDINATE,
            claims=(view,),
            truncated=paged and request.cursor is None,
            cursor="page-two" if paged and request.cursor is None else None,
        )

    monkeypatch.setattr(client, "read_playbill_claim_batch", batch, raising=False)
    return calls


def test_prefetch_keeps_contenders_and_avoids_claim_and_search_calls(connection, monkeypatch):
    pb, client = connection
    world = pb.world()
    subject = world.sec.vulnerability["cve-2026-69247"]
    calls = install(client, monkeypatch, paged=True)
    previous_searches = len(client.searches)
    views = world.prefetch(subjects=[subject], predicates=[world.sec.vuln.severity], page_size=1)
    assert subject.severity == views
    assert len(subject.severity) == 2  # no arbitrary contender selection
    assert len(calls) == 2
    assert len(client.searches) == previous_searches and client.claim_reads == []
    assert calls[0].at == calls[1].at == _COORDINATE
    assert calls[0].evaluation_time == calls[1].evaluation_time


def test_prefetch_budget_or_coordinate_refusal_installs_no_cache(connection, monkeypatch):
    pb, client = connection
    world = pb.world()
    install(client, monkeypatch, paged=True)
    with pytest.raises(WorldStructureError, match="max_claims"):
        world.prefetch(
            subjects=["sec.vulnerability/cve-2026-69247"], predicates=[SEVERITY], max_claims=1
        )
    assert world._claim_cache == {} and world._view_cache == {}
    install(client, monkeypatch, invalid=True)
    with pytest.raises(WorldStructureError, match="coordinate"):
        world.prefetch(subjects=["sec.vulnerability/cve-2026-69247"])
    assert world._claim_cache == {}


def test_full_subject_prefetch_also_caches_empty_attributes(connection, monkeypatch):
    pb, client = connection
    world = pb.world()
    subject = world.sec.vulnerability["cve-2026-69247"]
    install(client, monkeypatch)
    views = world.prefetch(subjects=[subject])
    assert subject.claims == subject.severity == views
    assert subject.affects_package == ()
    assert client.claim_reads == []


def test_typed_claim_batch_is_one_request(connection, monkeypatch):
    pb, client = connection
    calls = install(client, monkeypatch)
    views = pb.claim_views(["Claim:CLM-" + "9" * 32])
    assert len(calls) == 1 and len(views) == 1 and client.claim_reads == []


def test_prefetch_preserves_json_suffix_in_bare_subject_id(connection, monkeypatch):
    pb, client = connection
    world = pb.world()
    captured = []

    def empty(_instance_id, *, request):
        captured.append(request)
        return ClaimReadBatchResultV1(coordinate=_COORDINATE, claims=())

    monkeypatch.setattr(client, "read_playbill_claim_batch", empty, raising=False)
    world.prefetch(subjects=["sec.package/report.json"])
    assert captured[0].subject_paths == ("subjects/sec.package/report.json.json",)
    assert ("sec.package/report.json", None) in world._claim_cache


def test_claim_batch_refuses_returned_identity_mismatch(connection, monkeypatch):
    pb, client = connection
    install(client, monkeypatch)
    with pytest.raises(ValueError, match="position"):
        pb.claim_views(["Claim:CLM-" + "8" * 32])
