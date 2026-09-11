"""Selected source reads scale with their inputs, not inputs times shared history."""

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.errors import ProjectionIntegrityError, SettlementIntegrityError
from cruxible_core.indexes.sqlite import ProjectionHandle
from cruxible_core.proposals import settlement
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query import build_accepted_query_facts
from cruxible_core.service.floor.floor import service_export_playbill_floor
from tests.core_support._adoption_fixture import AdoptionFixtureProfile, build_fixture


@pytest.fixture(scope="module")
def instance(tmp_path_factory):
    fixture = build_fixture(
        tmp_path_factory.mktemp("batched-sources"),
        AdoptionFixtureProfile(
            name="batched",
            subjects=8,
            claim_types=2,
            documents=1,
            query_definitions=1,
            seed_claims=8,
            generations=1,
            claims_per_generation=1,
        ),
    )
    instance = PlaybillInstance.open(fixture.managed_root, trust_root=fixture.instance.trust_root)
    with instance.bind_accepted_projection(instance.accepted_coordinate()):
        pass
    with instance.accepted_history_reader():
        pass
    return instance


def test_bulk_claims_share_sources_and_records_preserving_selected_order(instance, monkeypatch):
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.typed.envelopes(kind="claim")
        identities = tuple(row.identity for row in rows)
        expected = tuple(projection.claim(identity) for identity in reversed(identities))
    batches = []
    original = instance._ledger.read_blobs

    def read(oids):
        batches.append(tuple(oids))
        return original(oids)

    monkeypatch.setattr(instance._ledger, "read_blobs", read)
    with instance.bind_accepted_projection(coordinate) as projection:
        claim_oids = {
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT m.git_blob_oid FROM members m JOIN claims c ON c.path=m.path"
            )
        }
        actual = projection.claims(tuple(reversed(identities)))
    assert actual == expected
    assert sum(bool(set(batch) & claim_oids) for batch in batches) == 1
    # One source batch and the two generation records, irrespective of Claim count.
    assert len(batches) == 3


def test_query_facts_match_source_replay_and_read_each_record_once(instance, monkeypatch):
    coordinate = instance.accepted_coordinate()
    source = SimpleNamespace(
        accepted_history=instance.accepted_history,
        tree_at=instance.tree_at,
        body_store=instance.body_store,
    )
    expected = build_accepted_query_facts(source, coordinate=coordinate)
    reads = Counter()
    original = instance.blob_at

    def read(oid, path):
        if path.startswith("changesets/"):
            reads[path] += 1
        return original(oid, path)

    monkeypatch.setattr(instance, "blob_at", read)
    assert build_accepted_query_facts(instance, coordinate=coordinate) == expected
    assert len(reads) == 2
    assert set(reads.values()) == {1}


def test_floor_reuses_query_claims_instead_of_materializing_another_list(instance, monkeypatch):
    instance.floor_export_memo.clear()
    instance.floor_structure_memo.clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("floor already parsed these Claims for its query facts")

    monkeypatch.setattr(ProjectionHandle, "list_claims", forbidden)
    files = service_export_playbill_floor(instance)
    assert "manifest.json" in files
    assert any(path.endswith(".profile.json") for path in files)


def test_record_reuse_keeps_fresh_source_checks_and_detached_models(instance, monkeypatch):
    instance._accepted_history_index._canonical_records.clear()
    rendered = []
    original = settlement.render_change_set

    def render(record):
        rendered.append(record.sequence)
        return original(record)

    monkeypatch.setattr(settlement, "render_change_set", render)
    reads = []

    def load(oid, path):
        reads.append(path)
        return instance.blob_at(oid, path)

    with instance.accepted_history_reader() as history:
        location = history.claim_law_locations()[0]
        record = history.read_member_record(location, load)
        expected = record.model_copy(deep=True)
        assert history.read_member_record(location, load) == expected
        assert len(reads) == len(rendered) == 1
        # Reusing the generation must still check each member's locator.
        with pytest.raises(ProjectionIntegrityError):
            history.read_member_record(replace(location, artifact_digest="wrong"), load)
        record.law_evidence[0].result.clear()
    with instance.accepted_history_reader() as history:
        assert history.read_member_record(location, load) == expected
    assert len(reads) == 2
    assert len(rendered) == 1
    with instance.accepted_history_reader() as history:
        with pytest.raises(ProjectionIntegrityError, match="unavailable"):
            history.read_member_record(location, lambda oid, path: None)
        with pytest.raises(SettlementIntegrityError, match="not canonical"):
            history.read_member_record(location, lambda oid, path: load(oid, path) + b" ")
