"""Accepted Git authenticates the complete static selection, not a rewritten manifest."""

import os
import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.subjects import render_subject
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.compiler.compiler import P2_B5_COMPILER
from cruxible_core.indexes import sqlite as storage
from cruxible_core.indexes.projection import render_projection_manifest
from cruxible_core.indexes.typed_sqlite import row_counts
from tests.core_support._projection_support import MemoryLedger, accepted_coordinate
from tests.test_claims.test_incremental_closure import _path, _pin_to, _subject


def _tree():
    anchor = _subject("anchor")
    return {
        _path("anchor"): render_subject(anchor),
        _path("dependent"): render_subject(_subject("dependent", pins=(_pin_to(anchor),))),
        _path("unrelated"): render_subject(_subject("unrelated")),
    }


def _publication(tmp_path):
    repository = MemoryLedger(
        tmp_path / "repository", {**_tree(), "cards/documents/review.md": b"review card"}
    )
    coordinate = accepted_coordinate(repository).model_copy(update={"compiler": P2_B5_COMPILER})
    directory = tmp_path / "projection"
    directory.mkdir()
    assembler = ProjectionAssembler(
        repository, accepted=coordinate, publication_directory=directory
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=directory / ".stage-source")
    )
    return repository, coordinate, result


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM pins",
        "UPDATE subjects SET revision=100",
        "DELETE FROM subjects WHERE identity='Subject:test/dependent'",
        "UPDATE members SET byte_length=byte_length+1",
    ],
)
def test_self_consistent_forged_rows_and_manifest_refuse_source_authentication(tmp_path, mutation):
    repository, coordinate, result = _publication(tmp_path)
    manifest_path = Path(result.manifest_path)
    piece_path = manifest_path.parent / result.manifest.pieces[0].name
    os.chmod(piece_path, 0o600)
    with sqlite3.connect(piece_path) as connection:
        if mutation.startswith("DELETE FROM subjects"):
            connection.execute("DELETE FROM subjects WHERE path LIKE '%dependent.json'")
        else:
            connection.execute(mutation)
        counts = row_counts(connection)
    os.chmod(piece_path, 0o400)
    piece = result.manifest.pieces[0].model_copy(
        update={
            "physical_digest": storage.physical_file_digest(piece_path).tagged,
            "byte_length": piece_path.stat().st_size,
        }
    )
    manifest = result.manifest.model_copy(
        update={
            "pieces": (piece,),
            "row_counts": counts,
            "logical_digest": storage.projection_logical_digest(piece_path).tagged,
        }
    )
    os.chmod(manifest_path, 0o600)
    manifest_path.write_bytes(render_projection_manifest(manifest))
    os.chmod(manifest_path, 0o400)
    with storage.bind_projection(manifest_path, expected=coordinate) as projection:
        with pytest.raises(ProjectionIntegrityError, match="differ from accepted source"):
            projection.require_source_authentication(repository=repository)


def test_full_build_is_ready_and_unknown_persisted_piece_authenticates_once(tmp_path, monkeypatch):
    repository, coordinate, result = _publication(tmp_path)
    manifest = Path(result.manifest_path)
    repository.list_calls = repository.read_calls = 0
    with storage.bind_projection(manifest, expected=coordinate) as projection:
        projection.require_source_authentication(repository=repository)
    assert repository.list_calls == repository.read_calls == 0
    # Process restart loses only the bounded verified-piece memo.
    storage._VERIFIED_PIECES.clear()
    with storage.bind_projection(manifest, expected=coordinate) as projection:
        projection.require_source_authentication(repository=repository)
    assert repository.list_calls == 1
    assert repository.read_calls == 1  # One bulk source read covers the complete inventory.
    repository.list_calls = repository.read_calls = 0
    with storage.bind_projection(manifest, expected=coordinate) as projection:
        projection.attach_sources(repository, bodies=None, history=None)
        projection.require_source_authentication(repository=repository)
    assert repository.list_calls == repository.read_calls == 0
    with pytest.raises(ProjectionIntegrityError, match="bound typed projection"):
        projection.require_source_authentication(repository=repository)


def test_bound_file_identity_change_invalidates_source_authentication(tmp_path):
    repository, coordinate, result = _publication(tmp_path)
    with storage.bind_projection(Path(result.manifest_path), expected=coordinate) as projection:
        projection.require_source_authentication(repository=repository)
        os.chmod(projection.index_path, 0o600)
        with pytest.raises(ProjectionIntegrityError, match="file identity changed"):
            projection.require_source_authentication(repository=repository)


@pytest.mark.parametrize("descriptor_namespace", ["native", "canonicalized", "absent"])
@pytest.mark.parametrize("swap_count", [1, 3])
def test_warm_bind_cannot_open_a_swapped_directory_then_certify_restored_path(
    tmp_path, monkeypatch, descriptor_namespace, swap_count
):
    import shutil

    repository, coordinate, result = _publication(tmp_path)
    manifest = Path(result.manifest_path)
    publication = manifest.parent
    replacement = tmp_path / "forged-publication"
    original = tmp_path / "original-publication"
    shutil.copytree(publication, replacement)
    forged_piece = replacement / result.manifest.pieces[0].name
    os.chmod(forged_piece, 0o600)
    with sqlite3.connect(forged_piece) as connection:
        connection.execute("DELETE FROM pins")
    connect = sqlite3.connect
    opened = []
    connections = []
    descriptors = []
    original_open = os.open

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def swapped(database, *args, **kwargs):
        if database == ":memory:":
            return connect(database, *args, **kwargs)
        if len(opened) >= swap_count:
            return connect(database, *args, **kwargs)
        publication.rename(original)
        replacement.rename(publication)
        try:
            connection = connect(database, *args, **kwargs)
            connections.append(connection)
            opened.append(connection.execute("SELECT COUNT(*) FROM pins").fetchone()[0])
            return connection
        finally:
            publication.rename(replacement)
            original.rename(publication)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", swapped)
        patch.setattr(os, "open", capture_open)
        if descriptor_namespace == "absent":
            patch.setattr(storage, "_descriptor_uri", lambda descriptor: None)
        elif descriptor_namespace == "canonicalized":
            # Linux's unix VFS can resolve a descriptor symlink back to the
            # published path. Simulate that resolution without assuming Darwin's
            # descriptor namespace proves behavior on other platforms.
            patch.setattr(
                storage,
                "_descriptor_uri",
                lambda descriptor: (
                    f"{(publication / result.manifest.pieces[0].name).as_uri()}?mode=ro&immutable=1"
                ),
            )
        if swap_count == 3:
            with pytest.raises(ProjectionIntegrityError, match="namespace changed"):
                storage.bind_projection(manifest, expected=coordinate)
        else:
            with storage.bind_projection(manifest, expected=coordinate) as projection:
                projection.require_source_authentication(repository=repository)
                descriptor = projection._source_descriptor
                assert descriptor is not None
                os.fstat(descriptor)
                assert (
                    projection._connection.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 1
                )
    assert len(opened) == swap_count
    if descriptor_namespace != "native":
        assert opened == [0] * swap_count
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_descriptor_fallback_verifies_opened_snapshot_and_failure_closes_fd(tmp_path, monkeypatch):
    _repository, coordinate, result = _publication(tmp_path)
    manifest = Path(result.manifest_path)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_descriptor_uri", lambda descriptor: None)
        with storage.bind_projection(manifest, expected=coordinate) as projection:
            assert projection._connection.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 1
    descriptors = []
    original_open = os.open

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("forced open failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", capture_open)
        patch.setattr(sqlite3, "connect", fail_connect)
        with pytest.raises(ProjectionIntegrityError, match="valid PB-B SQLite"):
            storage.bind_projection(manifest, expected=coordinate)
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_cold_binder_exports_only_the_opened_snapshot(tmp_path, monkeypatch):
    _repository, coordinate, result = _publication(tmp_path)
    manifest = Path(result.manifest_path)
    storage.reset_projection_verification_memo()
    connect = sqlite3.connect
    opens = []

    def record_open(database, *args, **kwargs):
        opens.append(str(database))
        return connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", record_open)
        with storage.bind_projection(manifest, expected=coordinate) as projection:
            assert (
                storage.projection_logical_digest(projection._connection).tagged
                == result.logical_digest
            )
    assert len([path for path in opens if path != ":memory:"]) == 1


def test_cold_logical_scan_does_not_hold_namespace_acquisition_guard(tmp_path, monkeypatch):
    _repository, coordinate, result = _publication(tmp_path)
    manifest = Path(result.manifest_path)
    storage.reset_projection_verification_memo()
    logical_digest = storage.projection_logical_digest

    def concurrent_unrelated_entry(source):
        # Cold export can be long. An unrelated entry in an ancestor directory
        # after acquisition must not invalidate the already acquired snapshot.
        (tmp_path / "unrelated-entry").write_bytes(b"unrelated")
        return logical_digest(source)

    monkeypatch.setattr(storage, "projection_logical_digest", concurrent_unrelated_entry)
    with storage.bind_projection(manifest, expected=coordinate) as projection:
        assert projection._connection.execute("SELECT COUNT(*) FROM pins").fetchone()[0] == 1
