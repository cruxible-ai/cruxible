"""Narrow C: fixed history boundaries, sparse occurrences and disposable storage."""

import sqlite3
from dataclasses import replace

import pytest

from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.history_index import AcceptedHistoryIndex
from cruxible_core.playbill.projection_artifacts import ArtifactEnvelopeRow
from tests.test_playbill._knowledge_loop_support import seed_claims


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    return seed_claims(tmp_path_factory.mktemp("history-source"))[0]


def prefix(seeded, length):
    recovered = seeded._recovered
    head = recovered.history[length - 1]
    return replace(recovered, history=recovered.history[:length], head=head)


def coordinate(generation, seeded):
    return AcceptedCoordinate(
        git_oid=generation.oid,
        semantic_root=generation.semantic_root.tagged,
        generation_root=generation.generation_root.tagged,
        compiler_digest=seeded.descriptor.compiler.rule_digest,
    )


def envelope(digest="old", path="claims/a.json", identity="Claim:a"):
    return ArtifactEnvelopeRow(identity, "claim", "claim-test", path, digest, None, 1)


def test_sparse_occurrences_reinstatement_rename_cutoff_and_queries(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = seeded._recovered
    assert len(state.history) >= 4
    # Deletion emits no artifact occurrence. Reinstatement, then rename, do.
    changes = {0: [envelope()], 1: [], 2: [envelope()], 3: [envelope(path="claims/b.json")]}
    calls = []

    def source(sequence):
        calls.append(sequence)
        return changes.get(sequence, [])

    with index.read(state, source) as reader:
        assert reader.artifact("missing") is None
        assert [r.occurrence_sequence for r in reader.occurrences("Claim:a")] == [0, 2, 3]
        assert reader.artifact("old").path == "claims/b.json"
        latest = reader.generation(state.head.sequence)
        assert latest.actor_id == state.head.record.actor_binding.actor_id
        assert latest.source_record_digest == state.head.record.changeset_digest
        assert reader.candidate_accepted(latest.candidate_digest)
    checked = index.generations_checked
    written = index.artifact_rows_written
    with index.read(state, source, at=coordinate(state.history[1], seeded)) as reader:
        assert reader.sequence == 1
        assert reader.artifact("old").occurrence_sequence == 0
        assert not reader.candidate_accepted(latest.candidate_digest)
        with pytest.raises(PlaybillFormatError):
            reader.generation(2)
        with pytest.raises(PlaybillFormatError):
            reader.resolve(coordinate(state.history[2], seeded))
    assert index.generations_checked == checked
    assert index.artifact_rows_written == written == 3
    assert calls == list(range(len(state.history)))
    with sqlite3.connect(index.path) as db:
        for query, params, expected in (
            (
                "SELECT 1 FROM accepted_generations WHERE candidate_digest=? AND sequence<=?",
                (latest.candidate_digest, latest.sequence),
                "generations_by_candidate",
            ),
            (
                "SELECT * FROM artifact_versions "
                "WHERE artifact_digest=? AND occurrence_sequence<=?",
                ("old", latest.sequence),
                "versions_by_digest",
            ),
        ):
            plan = repr(db.execute("EXPLAIN QUERY PLAN " + query, params).fetchall())
            assert "SEARCH" in plan and expected in plan


def test_incremental_publication_rollback_restart_and_delete_rebuild(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = prefix(seeded, 3)
    with index.read(prefix(seeded, 2), lambda n: [envelope(str(n))]):
        pass
    initial_writes = index.generations_written
    with pytest.raises(RuntimeError, match="crash"):
        with index.read(state, lambda n: [envelope(str(n))]) as reader:
            assert reader.artifact("2") is not None
            raise RuntimeError("crash before transaction commit")
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT sequence FROM history_progress").fetchone() == (1,)
        assert db.execute("SELECT MAX(sequence) FROM accepted_generations").fetchone() == (1,)
    calls = []

    def source(n):
        calls.append(n)
        return [envelope(str(n))]

    with index.read(state, source):
        pass
    assert calls == [2]
    assert initial_writes == 2
    reopened = AcceptedHistoryIndex(index.path)
    calls.clear()
    with reopened.read(state, source) as reader:
        expected = reader.occurrences("Claim:a")
    assert calls == [0, 1, 2]  # Restart validates persisted rows, not cursor-only trust.
    assert reopened.generations_written == 0
    index.path.unlink()
    with reopened.read(state, source) as reader:
        assert reader.occurrences("Claim:a") == expected
    assert reopened.generations_written == 3


def test_external_changes_reconciled_and_failed_source_does_not_publish(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = prefix(seeded, 2)
    with index.read(state, lambda n: [envelope(str(n))]):
        pass
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE artifact_versions SET identity='Claim:tampered'")
        db.execute("UPDATE accepted_generations SET candidate_digest='fake' WHERE sequence=1")
    with index.read(state, lambda n: [envelope(str(n))]) as reader:
        assert reader.artifact("1").identity == "Claim:a"
        assert not reader.candidate_accepted("fake")
    index.invalidate()

    def fail(n):
        if n == 1:
            raise RuntimeError("unavailable verified source")
        return [envelope("different")]

    with pytest.raises(RuntimeError):
        with index.read(state, fail):
            pytest.fail("incomplete source must not yield a reader")
    with sqlite3.connect(index.path) as db:
        assert db.execute(
            "SELECT artifact_digest FROM artifact_versions ORDER BY occurrence_sequence"
        ).fetchall() == [("0",), ("1",)]


def test_mixed_coordinates_duplicate_oid_and_ambiguous_digest(tmp_path, seeded):
    state = prefix(seeded, 2)
    # The same OID can only be resolved with the complete coordinate, not an
    # arbitrary choice of the first occurrence. Synthetic adapter input tests it.
    second = replace(state.history[1], oid=state.history[0].oid)
    state = replace(state, history=(state.history[0], second), head=second)
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    with index.read(state, lambda n: [envelope(identity=f"Claim:{n}")]) as reader:
        assert reader.resolve(coordinate(second, seeded)).sequence == 1
        with pytest.raises(PlaybillFormatError):
            reader.resolve(coordinate(second, seeded).model_copy(update={"semantic_root": "wrong"}))
        with pytest.raises(PlaybillFormatError, match="multiple identities"):
            reader.artifact("old")
        assert reader.artifact("old", identity="Claim:0").occurrence_sequence == 0
    with index.read(prefix(seeded, 1), lambda n: [envelope(identity="Claim:0")]) as reader:
        assert reader.artifact("old").identity == "Claim:0"


def test_instance_projection_parity_and_warm_no_history_source_work(seeded, monkeypatch):
    instance = seeded
    with instance.accepted_history_reader():
        pass  # Includes metadata fallback for missing historical publications.
    # Cold oracle uses frozen compiler row derivation directly from retained Git.
    from types import SimpleNamespace

    from cruxible_core.playbill.compiler import (
        artifact_codec_for_compiler,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.playbill.projection_artifacts import parse_projection_tree

    compiler = instance.descriptor.compiler
    expected = []
    for n, generation in enumerate(instance.accepted_history()):
        at = instance.coordinate_for_oid(generation.oid)
        parsed = parse_projection_tree(
            instance.tree_at(generation.oid),
            registry=projection_registry_for_compiler(compiler),
            artifact_kinds=artifact_kinds_for_compiler(compiler),
            artifact_codec=artifact_codec_for_compiler(compiler),
            bodies=instance.body_store(),
            coordinate=SimpleNamespace(
                **AcceptedCoordinate.from_internal(at).model_dump(),
                git_object_format=at.git_object_format,
                instance_id=at.instance_id,
            ),
        )
        changed = (
            None
            if n == 0
            else instance._ledger.changed_tree_paths(
                instance.accepted_history()[n - 1].oid, generation.oid
            )
        )
        expected.extend(
            (row.identity, row.artifact_digest, n, row.path)
            for row in parsed.envelopes
            if changed is None or row.path in changed
        )
    with instance.accepted_history_reader() as reader:
        actual = [
            (r.identity, r.artifact_digest, r.occurrence_sequence, r.path)
            for identity in sorted({r[0] for r in expected})
            for r in reader.occurrences(identity)
        ]
    assert sorted(actual) == sorted(expected)

    def no_source(*args, **kwargs):
        pytest.fail("warm lookup must not scan history, diff Git, or open historical projections")

    monkeypatch.setattr(instance, "accepted_history", no_source)
    monkeypatch.setattr(instance._ledger, "changed_tree_paths", no_source)
    monkeypatch.setattr("cruxible_core.playbill.instance.bind_projection", no_source)
    with instance.accepted_history_reader() as reader:
        assert reader.artifact(expected[-1][1]) is not None
    from cruxible_core.service.playbill_proposals import service_list_playbill_proposals

    assert service_list_playbill_proposals(instance).entries


def test_schema_tampering_and_symlinks_refuse(tmp_path, seeded):
    from cruxible_client.contracts.errors import ProjectionIntegrityError

    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = prefix(seeded, 1)
    with index.read(state, lambda n: [envelope()]):
        pass
    with sqlite3.connect(index.path) as db:
        db.execute(
            "CREATE TRIGGER sabotage AFTER UPDATE ON history_progress "
            "BEGIN DELETE FROM artifact_versions; END"
        )
    with pytest.raises(ProjectionIntegrityError, match="schema differs"):
        with index.read(state, lambda n: [envelope()]):
            pytest.fail("unexpected schema must not be executed")
    link = tmp_path / "link.sqlite3"
    link.symlink_to(index.path)
    with pytest.raises(ProjectionIntegrityError, match="symlink"):
        with AcceptedHistoryIndex(link).read(state, lambda n: []):
            pass


def test_binding_includes_compiler_and_read_handle_expires(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = prefix(seeded, 1)
    with index.read(state, lambda n: [envelope()]) as reader:
        assert reader.sequence == 0
        with pytest.raises(AttributeError):
            reader.sequence = 100
    with pytest.raises(sqlite3.ProgrammingError):
        reader.artifact("old")
    changed = replace(
        state,
        coordinate=state.coordinate.model_copy(
            update={
                "compiler": state.coordinate.compiler.model_copy(update={"schema_version": 999})
            }
        ),
    )
    with index.read(changed, lambda n: [envelope()]) as reader:
        assert reader.generation(0).schema_version == 999
    assert index.generations_checked == 2


def test_real_successor_uses_only_changed_paths(tmp_path, monkeypatch):
    from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
    from tests.test_playbill._knowledge_loop_support import TIMESTAMP, activate, authoring

    instance, owner = seed_claims(tmp_path)
    with instance.accepted_history_reader():
        pass
    index = instance._accepted_history_index
    checked = index.generations_checked
    old = instance.accepted_coordinate()
    proposal = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-new", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="history-successor",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, proposal)
    changed = instance._ledger.changed_tree_paths(
        old.git_oid, instance.accepted_coordinate().git_oid
    )
    calls = []
    original = instance._ledger.changed_tree_paths

    def diff(before, after):
        calls.append((before, after))
        return original(before, after)

    def no_cold_tree(*args, **kwargs):
        pytest.fail("published successor must not reconstruct a full historical tree")

    monkeypatch.setattr(instance._ledger, "changed_tree_paths", diff)
    monkeypatch.setattr(instance._ledger, "read_tree", no_cold_tree)
    with instance.accepted_history_reader() as reader:
        assert reader.candidate_accepted(proposal.proposal.proposal.candidate.candidate_digest)
    assert calls == [(old.git_oid, instance.accepted_coordinate().git_oid)]
    assert index.generations_checked == checked + 1
    with sqlite3.connect(index.path) as db:
        indexed_paths = {
            row[0]
            for row in db.execute(
                "SELECT path FROM artifact_versions WHERE occurrence_sequence=?",
                (instance._recovered.head.sequence,),
            )
        }
    assert indexed_paths <= set(changed)
    assert indexed_paths  # New Claim and Subject, without untouched artifact copies.


def test_file_replacement_during_reader_revokes_readiness(tmp_path, seeded):
    from cruxible_client.contracts.errors import ProjectionIntegrityError

    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    state = prefix(seeded, 1)
    with index.read(state, lambda n: [envelope()]):
        pass
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(index.path.read_bytes())
    with pytest.raises(ProjectionIntegrityError, match="replaced during read"):
        with index.read(state, lambda n: [envelope()]) as reader:
            assert reader.artifact("old") is not None
            replacement.replace(index.path)
    assert index._ready is None
    with index.read(state, lambda n: [envelope()]) as reader:
        assert reader.artifact("old") is not None
