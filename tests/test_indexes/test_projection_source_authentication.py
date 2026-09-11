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
    repository = MemoryLedger(tmp_path / "repository", _tree())
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
