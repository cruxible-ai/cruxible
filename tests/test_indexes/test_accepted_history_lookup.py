"""Accepted membership acceleration changes neither proof nor epoch semantics."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillFormatError, ProjectionIntegrityError
from cruxible_core.runtime import instance as module
from tests.core_support._knowledge_loop_support import seed_claims


@pytest.fixture
def instance(tmp_path):
    return seed_claims(tmp_path)[0]


class CountedHistory(tuple):
    def __new__(cls, entries):
        result = super().__new__(cls, entries)
        result.walks = 0
        return result

    def __iter__(self):
        self.walks += 1
        return super().__iter__()


def test_indexed_membership_preserves_old_current_and_missing_without_history_walk(instance):
    expected = {g.oid: instance.coordinate_for_oid(g.oid) for g in instance.accepted_history()}
    history = CountedHistory(instance.accepted_history())
    instance._recovered = replace(instance._recovered, history=history)
    for _ in range(3):
        for oid, coordinate in expected.items():
            assert instance.coordinate_for_oid(oid) == coordinate
            assert instance.accepted_evaluation_time(oid).tzinfo is not None
        with pytest.raises(PlaybillFormatError, match="Git OID is not one accepted generation"):
            instance.coordinate_for_oid("0" * 64)
        with pytest.raises(PlaybillFormatError, match="evaluation coordinate is outside"):
            instance.accepted_evaluation_time("0" * 64)
    assert history.walks == 0


def test_duplicate_oid_refuses_both_surfaces_and_recovered_replacement_invalidates(instance):
    recovered = instance._recovered
    head = recovered.head
    instance.coordinate_for_oid(head.oid)
    instance._recovered = replace(recovered, history=(*recovered.history, head))
    with pytest.raises(ProjectionIntegrityError, match="sequence is not contiguous"):
        instance.coordinate_for_oid(head.oid)
    with pytest.raises(ProjectionIntegrityError, match="sequence is not contiguous"):
        instance.accepted_evaluation_time(head.oid)
    previous = recovered.history[-2]
    instance._recovered = replace(
        recovered,
        history=recovered.history[:-1],
        head=previous,
        coordinate=recovered.coordinate.model_copy(
            update={
                "git_oid": previous.oid,
                "semantic_root": previous.semantic_root.tagged,
                "generation_root": previous.generation_root.tagged,
            }
        ),
    )
    # A warmed positive result does not authorize an OID outside the new epoch.
    with pytest.raises(PlaybillFormatError):
        instance.blobs_at(head.oid, ())
    instance._recovered = recovered
    assert instance.coordinate_for_oid(head.oid).git_oid == head.oid


def test_unsigned_location_cannot_replace_replayed_generation(instance, monkeypatch):
    from cruxible_core.indexes.history.history_index import HistoryReader

    original = HistoryReader.generation_for_oid

    def replaced(reader, oid):
        location = original(reader, oid)
        return replace(location, semantic_root="sha256:" + "0" * 64)

    monkeypatch.setattr(HistoryReader, "generation_for_oid", replaced)
    with pytest.raises(ProjectionIntegrityError, match="differs from captured replay"):
        instance.coordinate_for_oid(instance.accepted_coordinate().git_oid)


def test_warm_lookup_still_checks_repository_path_and_coordinate_members(instance, monkeypatch):
    head = instance.accepted_coordinate()
    instance.coordinate_for_oid(head.git_oid)
    with pytest.raises(PlaybillFormatError, match="mixed members"):
        instance.resolve_accepted_coordinate(
            git_oid=head.git_oid,
            semantic_root="sha256:" + "0" * 64,
            generation_root=head.generation_root,
        )
    with pytest.raises(PlaybillFormatError, match="compiler digest"):
        instance.resolve_accepted_coordinate(
            git_oid=head.git_oid,
            semantic_root=head.semantic_root,
            generation_root=head.generation_root,
            compiler_digest="sha256:" + "0" * 64,
        )
    original = Path.resolve

    def missing(path, *, strict=False):
        if path == instance._ledger.path and strict:
            raise FileNotFoundError("ledger directory disappeared")
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", missing)
    with pytest.raises(PlaybillFormatError, match="missing: ledger"):
        instance.coordinate_for_oid(head.git_oid)
    with pytest.raises(PlaybillFormatError, match="missing: ledger"):
        instance.blobs_at(head.git_oid, ())


def test_interleaved_refresh_cannot_mix_epoch_membership(instance, monkeypatch):
    original = instance._recovered
    head = original.head
    previous = original.history[-2]
    replacement = replace(
        original,
        history=original.history[:-1],
        head=previous,
        coordinate=original.coordinate.model_copy(
            update={
                "git_oid": previous.oid,
                "semantic_root": previous.semantic_root.tagged,
                "generation_root": previous.generation_root.tagged,
            }
        ),
    )
    acquire = instance._history_reader_for_epoch

    @contextmanager
    def switching(recovered, **kwargs):
        with acquire(recovered, **kwargs) as reader:
            instance._recovered = replacement
            yield reader

    monkeypatch.setattr(instance, "_history_reader_for_epoch", switching)
    # This read began in the old immutable epoch and may finish there.
    assert instance.coordinate_for_oid(head.oid).git_oid == head.oid
    # The next read must not accept the now-stale index publication.
    with pytest.raises(PlaybillFormatError):
        instance.coordinate_for_oid(head.oid)


def test_successful_refresh_releases_index_and_failed_recovery_adds_no_authority(
    instance, monkeypatch
):
    head = instance.accepted_coordinate()
    instance.coordinate_for_oid(head.git_oid)
    instance.refresh()
    assert not hasattr(instance, "_history_lookup")
    assert instance.coordinate_for_oid(head.git_oid) == head
    original = instance._recovered

    def fail(*args, **kwargs):
        raise PlaybillFormatError("injected recovery refusal")

    monkeypatch.setattr(module, "recover_instance", fail)
    with pytest.raises(PlaybillFormatError, match="recovery refusal"):
        instance.refresh()
    assert instance._recovered is original
    with pytest.raises(PlaybillFormatError):
        instance.coordinate_for_oid("0" * 64)


def test_cold_membership_does_not_require_unrelated_document_body(tmp_path):
    from cruxible_client.contracts.documents import (
        DocumentAuthority,
        DocumentLifecycle,
        DocumentShell,
        render_document,
    )
    from cruxible_core.compiler.assembler import ProjectionAssembler
    from cruxible_core.indexes.projection import projection_manifest_name
    from tests.core_support._support import initialize_local
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"historical unrelated body")
    document = DocumentShell(
        identity="document:review",
        document_kind="note",
        title="Review",
        media_type="text/plain",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:test",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    path = "documents/review.json"
    source = render_document(document)
    _accept_tree(
        instance,
        owner,
        {**instance.tree_at(instance.accepted_coordinate().git_oid), path: source},
        timestamp="2026-08-17T14:00:00.000000Z",
        proposal_name="review-document",
    )
    coordinate = instance.accepted_coordinate()
    publication = instance.root / instance.descriptor.storage.projections
    assembler = ProjectionAssembler(
        instance._ledger,
        accepted=coordinate,
        publication_directory=publication,
        bodies=instance.body_store(),
    )
    request = assembler.request(output_staging_directory=publication / ".unused")
    (publication / projection_manifest_name(request)).unlink()
    instance.body_store()._path(body.digest).unlink()
    instance._accepted_history_index.invalidate()

    assert instance.coordinate_for_oid(coordinate.git_oid) == coordinate
    assert instance.accepted_evaluation_time(coordinate.git_oid).tzinfo is not None
    assert instance.blob_at(coordinate.git_oid, path) == source
    with instance.accepted_history_reader() as history:
        members = history.member_history(path)
        assert history.latest_member(path) == members[-1]
        assert history.latest_member("documents/absent.json") is None
