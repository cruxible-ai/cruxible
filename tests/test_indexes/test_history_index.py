"""Narrow C: fixed history boundaries, sparse occurrences and disposable storage."""

import sqlite3
from dataclasses import replace

import pytest

from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.compiler.projection_artifacts import ArtifactEnvelopeRow
from cruxible_core.indexes.history.history_index import AcceptedHistoryIndex
from tests.core_support._knowledge_loop_support import seed_claims


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

    def crash(n):
        raise RuntimeError("crash during synchronization")

    with pytest.raises(RuntimeError, match="crash"):
        with index.read(state, crash):
            pytest.fail("failed synchronization must not yield a reader")
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

    from cruxible_core.compiler.compiler import (
        artifact_codec_for_compiler,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

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
    monkeypatch.setattr("cruxible_core.runtime.instance.bind_projection", no_source)
    with instance.accepted_history_reader() as reader:
        assert reader.artifact(expected[-1][1]) is not None
    from cruxible_core.service.proposals.proposals import service_list_playbill_proposals

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


def test_proposal_commit_preserves_only_previously_verified_history(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "working.sqlite3")
    state = prefix(seeded, 2)
    calls = []

    def source(sequence):
        calls.append(sequence)
        return [envelope(str(sequence))]

    with index.read(state, source):
        pass
    with index._lock, sqlite3.connect(index.path) as db:
        db.execute("BEGIN IMMEDIATE")
        before = index._file_stamp()
        db.execute("CREATE TABLE proposals (proposal_id TEXT PRIMARY KEY) STRICT")
        db.execute("INSERT INTO proposals VALUES ('p')")
        db.commit()
        index.proposal_committed(before)
    with index.read(state, source) as reader:
        assert reader.artifact("1") is not None
    assert calls == [0, 1]

    # An unexplained mutation before a known proposal update must not be
    # laundered into readiness by its post-commit callback.
    with sqlite3.connect(index.path) as db:
        db.execute("DELETE FROM artifact_versions")
    with index._lock, sqlite3.connect(index.path) as db:
        db.execute("BEGIN IMMEDIATE")
        before = index._file_stamp()
        db.execute("INSERT INTO proposals VALUES ('q')")
        db.commit()
        index.proposal_committed(before)
    with index.read(state, source) as reader:
        assert reader.artifact("1") is not None
    assert calls == [0, 1, 0, 1]


def test_proposal_trigger_cannot_mutate_history_through_shared_database(tmp_path, seeded):
    from cruxible_client.contracts.errors import ProjectionIntegrityError

    index = AcceptedHistoryIndex(tmp_path / "working.sqlite3")
    state = prefix(seeded, 1)
    with index.read(state, lambda n: [envelope()]):
        pass
    with sqlite3.connect(index.path) as db:
        db.execute("CREATE TABLE proposals (proposal_id TEXT PRIMARY KEY) STRICT")
        db.execute(
            "CREATE TRIGGER sabotage AFTER INSERT ON proposals "
            "BEGIN DELETE FROM artifact_versions; END"
        )
    with pytest.raises(ProjectionIntegrityError, match="schema differs"):
        with index.read(state, lambda n: [envelope()]):
            pytest.fail("proposal triggers must not bypass the shared schema check")


def test_member_locations_read_exact_evidence_without_history_scan(seeded, monkeypatch):
    from cruxible_client.contracts.errors import ProjectionIntegrityError

    instance = seeded
    with instance.accepted_history_reader() as reader:
        generations = instance.accepted_history()
        generation = generations[2]
        member = generation.record.members[0]
        version = reader.artifact(member.candidate_artifact_digest)
        location = reader.claim_law_evidence(
            version.identity, artifact_digest=version.artifact_digest, path=version.path
        )
        assert location.sequence == 2
        assert reader.member_history(version.path) == (location,)
        requested = []

        def load(oid, path):
            requested.append((oid, path))
            return instance.blob_at(oid, path)

        result = reader.read_claim_law_evidence(
            version.identity,
            artifact_digest=version.artifact_digest,
            path=version.path,
            load_record=load,
        )
        assert result == generation.record.law_evidence[0]
        assert requested == [(generation.oid, "changesets/cs-00000000000000000002.json")]
        with pytest.raises(ProjectionIntegrityError, match="requested version"):
            reader.claim_law_evidence(version.identity, artifact_digest="wrong", path=version.path)
        with pytest.raises(ProjectionIntegrityError, match="unavailable"):
            reader.read_member_record(location, lambda oid, path: None)
        with pytest.raises(ProjectionIntegrityError, match="binding differs"):
            reader.read_member_record(
                location,
                lambda oid, path: instance.blob_at(
                    generations[3].oid, "changesets/cs-00000000000000000003.json"
                ),
            )
    checked = instance._accepted_history_index.generations_checked

    def no_scan(*args, **kwargs):
        pytest.fail("selected evidence read must not traverse accepted history")

    monkeypatch.setattr(instance, "accepted_history", no_scan)
    with instance.accepted_history_reader() as reader:
        assert (
            reader.read_claim_law_evidence(
                version.identity,
                artifact_digest=version.artifact_digest,
                path=version.path,
                load_record=load,
            )
            == result
        )
    assert instance._accepted_history_index.generations_checked == checked


def test_unchanged_member_evaluation_gets_its_own_location_and_cutoff(tmp_path, seeded):
    from cruxible_core.proposals.settlement import change_set_digest, render_change_set

    source = seeded._recovered
    original = source.history[2]
    member = original.record.members[0]
    with seeded.accepted_history_reader() as reader:
        version = reader.artifact(member.candidate_artifact_digest)
    row = ArtifactEnvelopeRow(
        version.identity, "claim", "test", version.path, version.artifact_digest, None, 1
    )
    record = original.record.model_copy(update={"sequence": 4})
    record = record.model_copy(update={"changeset_digest": change_set_digest(record).tagged})
    repeat = replace(original, sequence=4, oid="f" * len(original.oid), record=record)
    state = replace(source, history=(*source.history, repeat), head=repeat)
    index = AcceptedHistoryIndex(tmp_path / "working.sqlite3")
    with index.read(state, lambda n: [row] if n == 2 else []) as reader:
        assert [v.occurrence_sequence for v in reader.occurrences(version.identity)] == [2]
        assert [m.sequence for m in reader.member_history(version.path)] == [2, 4]
        latest = reader.claim_law_evidence(
            version.identity, artifact_digest=version.artifact_digest, path=version.path
        )
        assert latest.sequence == 4
        assert (
            reader.read_member_record(latest, lambda oid, path: render_change_set(record)) == record
        )
    with index.read(
        state, lambda n: [row] if n == 2 else [], at=coordinate(original, seeded)
    ) as reader:
        assert (
            reader.claim_law_evidence(
                version.identity, artifact_digest=version.artifact_digest, path=version.path
            ).sequence
            == 2
        )


def test_old_narrow_history_schema_upgrades_and_rebuilds_member_locations(tmp_path, seeded):
    from cruxible_core.indexes.history.history_index import _HISTORY_SCHEMA

    path = tmp_path / "working.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(_HISTORY_SCHEMA)
    index = AcceptedHistoryIndex(path)
    with index.read(seeded._recovered, lambda n: []) as reader:
        path = seeded.accepted_history()[2].record.members[0].path
        assert reader.member_history(path)[0].sequence == 2


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
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import TIMESTAMP, activate, authoring

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
    with sqlite3.connect(replacement) as destination, sqlite3.connect(index.path) as source:
        source.backup(destination)
    with pytest.raises(ProjectionIntegrityError, match="replaced during read"):
        with index.read(state, lambda n: [envelope()]) as reader:
            assert reader.artifact("old") is not None
            replacement.replace(index.path)
    assert index._ready is None
    with index.read(state, lambda n: [envelope()]) as reader:
        assert reader.artifact("old") is not None


def test_held_reader_allows_publication_and_keeps_snapshot(tmp_path, seeded):
    from concurrent.futures import ThreadPoolExecutor

    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")

    def source(n):
        return [envelope(str(n))]

    with ThreadPoolExecutor(max_workers=1) as workers:
        with index.read(prefix(seeded, 2), source) as old_reader:

            def publish():
                with index.read(prefix(seeded, 3), source) as new_reader:
                    return new_reader.artifact("2")

            assert workers.submit(publish).result(timeout=2).occurrence_sequence == 2
            # Check the actual snapshot, not just the public cutoff filter.
            assert old_reader._connection.execute(
                "SELECT MAX(sequence) FROM accepted_generations"
            ).fetchone() == (1,)
            assert old_reader.artifact("2") is None
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                old_reader._connection.execute("DELETE FROM artifact_versions")
    with index.read(prefix(seeded, 3), source) as reader:
        assert reader.artifact("2") is not None


def test_held_reader_does_not_block_other_process_writer(tmp_path, seeded):
    import subprocess
    import sys

    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    with index.read(prefix(seeded, 2), lambda n: [envelope(str(n))]):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0.2) as db:
    assert db.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    db.execute('BEGIN IMMEDIATE')
    db.execute('UPDATE history_progress SET sequence=sequence WHERE singleton=1')
""",
                str(index.path),
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0, result.stderr


def test_instance_reader_releases_state_lock_and_accepts_second_reader(seeded):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as workers:
        with seeded.accepted_history_reader() as first:

            def acquire():
                with seeded._state_lock:
                    pass
                with seeded.accepted_history_reader() as second:
                    return second.sequence

            assert workers.submit(acquire).result(timeout=2) == first.sequence


def test_caller_exception_does_not_undo_completed_sync(tmp_path, seeded):
    index = AcceptedHistoryIndex(tmp_path / "history.sqlite3")
    with pytest.raises(RuntimeError, match="caller"):
        with index.read(prefix(seeded, 2), lambda n: [envelope(str(n))]):
            raise RuntimeError("caller failed after acquiring a snapshot")
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT sequence FROM history_progress").fetchone() == (1,)


def test_proposal_file_reads_are_outside_history_snapshot(seeded, monkeypatch):
    from contextlib import contextmanager

    from cruxible_core.service.proposals.proposals import service_list_playbill_proposals

    original_reader = seeded.accepted_history_reader
    active = False

    @contextmanager
    def reader_scope(**kwargs):
        nonlocal active
        with original_reader(**kwargs) as reader:
            active = True
            try:
                yield reader
            finally:
                active = False

    evidence = seeded.proposal_evidence()
    monkeypatch.setattr(seeded, "proposal_evidence", lambda: evidence)
    monkeypatch.setattr(seeded, "accepted_history_reader", reader_scope)
    for name in ("list_admissions", "read_evaluation", "read_candidate"):
        original = getattr(evidence, name)

        def read_file(*args, _original=original, **kwargs):
            assert not active, "proposal file IO held the history snapshot open"
            return _original(*args, **kwargs)

        monkeypatch.setattr(evidence, name, read_file)
    assert service_list_playbill_proposals(seeded).entries
