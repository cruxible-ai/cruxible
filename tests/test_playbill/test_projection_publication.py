"""PB-B immutable publication, binding, lifecycle, and crash-point tests."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import cruxible_core.storage.playbill_projection as projection_module
from cruxible_core.playbill.assembler import PROJECTION_CRASH_POINTS, ProjectionAssembler
from cruxible_core.playbill.errors import ProjectionIntegrityError
from cruxible_core.playbill.projection import (
    ProjectionManifest,
    projection_manifest_name,
    render_projection_manifest,
)
from cruxible_core.storage.playbill_projection import (
    bind_projection,
    detect_projection_orphans,
    physical_file_digest,
)
from tests.test_playbill._projection_support import (
    MemoryLedger,
    accepted_coordinate,
    fixture_bytes,
)


class SimulatedCrash(RuntimeError):
    pass


def _publisher(
    tmp_path: Path,
    repository: MemoryLedger,
    *,
    publication: Path | None = None,
    generation_byte: str = "22",
) -> ProjectionAssembler:
    directory = publication or (tmp_path / "published")
    directory.mkdir(exist_ok=True)
    return ProjectionAssembler(
        repository,
        accepted=accepted_coordinate(repository, generation_byte=generation_byte),
        publication_directory=directory,
    )


def _publish(
    tmp_path: Path,
    repository: MemoryLedger,
    *,
    publication: Path | None = None,
    generation_byte: str = "22",
    stage: str = ".stage-publish",
):
    assembler = _publisher(
        tmp_path,
        repository,
        publication=publication,
        generation_byte=generation_byte,
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=assembler.publication_directory / stage)
    )
    return assembler, result


def test_tampered_missing_and_torn_publications_refuse_serving(tmp_path: Path) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", 1)},
    )
    assembler, result = _publish(tmp_path, repository)
    manifest_path = Path(result.manifest_path)
    piece_path = manifest_path.parent / result.manifest.pieces[0].name

    original_piece = piece_path.read_bytes()
    os.chmod(piece_path, 0o600)
    piece_path.write_bytes(original_piece[:-1] + bytes([original_piece[-1] ^ 1]))
    with pytest.raises(ProjectionIntegrityError, match="digest mismatch"):
        bind_projection(manifest_path, expected=assembler.accepted)

    piece_path.write_bytes(original_piece)
    os.chmod(piece_path, 0o400)
    original_manifest = manifest_path.read_bytes()
    manifest_path.unlink()
    with pytest.raises(ProjectionIntegrityError, match="regular file"):
        bind_projection(manifest_path, expected=assembler.accepted)

    manifest_path.write_bytes(original_manifest[: len(original_manifest) // 2])
    with pytest.raises(ProjectionIntegrityError, match="malformed"):
        bind_projection(manifest_path, expected=assembler.accepted)


def test_manifest_cannot_launder_a_changed_sqlite_logical_state(tmp_path: Path) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", 1)},
    )
    assembler, result = _publish(tmp_path, repository)
    manifest_path = Path(result.manifest_path)
    piece_path = manifest_path.parent / result.manifest.pieces[0].name
    os.chmod(piece_path, 0o600)
    connection = sqlite3.connect(piece_path)
    try:
        connection.execute(
            "UPDATE semantic_facts SET value_json = ? WHERE subject_identity = 'one'",
            ('{"forged":true}',),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(piece_path, 0o400)
    changed_piece = result.manifest.pieces[0].model_copy(
        update={
            "byte_length": piece_path.stat().st_size,
            "physical_digest": physical_file_digest(piece_path).tagged,
        }
    )
    forged = result.manifest.model_copy(update={"pieces": (changed_piece,)})
    os.chmod(manifest_path, 0o600)
    manifest_path.write_bytes(render_projection_manifest(forged))
    os.chmod(manifest_path, 0o400)
    with pytest.raises(ProjectionIntegrityError, match="logical digest"):
        bind_projection(manifest_path, expected=assembler.accepted)


def test_binding_verifies_once_and_reads_do_not_rehash(monkeypatch, tmp_path: Path) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"count": 1})},
    )
    assembler, result = _publish(tmp_path, repository)
    calls = 0
    real = projection_module.projection_logical_digest

    def counted(path: Path):
        nonlocal calls
        calls += 1
        return real(path)

    monkeypatch.setattr(projection_module, "projection_logical_digest", counted)
    handle = bind_projection(Path(result.manifest_path), expected=assembler.accepted)
    assert calls == 1
    assert handle.fixture("one") == handle.fixture("one")
    assert calls == 1


def test_manifest_models_ordered_pieces_but_pb_b_never_selects_them_independently(
    tmp_path: Path,
) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", 1)},
    )
    assembler, result = _publish(tmp_path, repository)
    first = result.manifest.pieces[0]
    second = first.model_copy(
        update={"ordinal": 1, "name": first.name.replace("-0000.sqlite", "-0001.sqlite")}
    )
    future_shaped = result.manifest.model_copy(update={"pieces": (first, second)})
    assert (
        ProjectionManifest.model_validate_json(future_shaped.model_dump_json()).pieces[1].ordinal
        == 1
    )
    manifest_path = Path(result.manifest_path)
    os.chmod(manifest_path, 0o600)
    manifest_path.write_bytes(render_projection_manifest(future_shaped))
    os.chmod(manifest_path, 0o400)
    with pytest.raises(ProjectionIntegrityError, match="one-piece serving"):
        bind_projection(manifest_path, expected=assembler.accepted)


def test_bound_old_handle_survives_later_publication_without_mixed_generation(
    tmp_path: Path,
) -> None:
    publication = tmp_path / "published"
    old_repository = MemoryLedger(
        tmp_path / "old-repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"generation": "old"})},
        oid_seed="old",
    )
    old_assembler, old_result = _publish(
        tmp_path,
        old_repository,
        publication=publication,
        generation_byte="22",
        stage=".stage-old",
    )
    old_handle = bind_projection(Path(old_result.manifest_path), expected=old_assembler.accepted)

    new_repository = MemoryLedger(
        tmp_path / "new-repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"generation": "new"})},
        oid_seed="new",
    )
    new_assembler, new_result = _publish(
        tmp_path,
        new_repository,
        publication=publication,
        generation_byte="33",
        stage=".stage-new",
    )
    new_handle = bind_projection(Path(new_result.manifest_path), expected=new_assembler.accepted)

    assert old_handle.fixture("one")["facts"][0]["value"] == {"generation": "old"}
    assert new_handle.fixture("one")["facts"][0]["value"] == {"generation": "new"}
    assert old_handle.manifest.git_oid != new_handle.manifest.git_oid


def test_bound_handle_keeps_the_verified_inode_if_path_is_replaced(tmp_path: Path) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"state": "bound"})},
    )
    assembler, result = _publish(tmp_path, repository)
    handle = bind_projection(Path(result.manifest_path), expected=assembler.accepted)
    piece_path = Path(result.manifest_path).parent / result.manifest.pieces[0].name
    replacement = tmp_path / "replacement.sqlite"
    shutil.copyfile(piece_path, replacement)
    connection = sqlite3.connect(replacement)
    try:
        connection.execute(
            "UPDATE semantic_facts SET value_json = ? WHERE subject_identity = 'one'",
            ('{"state":"replaced"}',),
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(replacement, piece_path)

    assert handle.fixture("one")["facts"][0]["value"] == {"state": "bound"}
    handle.close()
    with pytest.raises(ProjectionIntegrityError, match="closed"):
        handle.fixture("one")


@pytest.mark.parametrize("point", PROJECTION_CRASH_POINTS)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_frozen_publication_crash_points_leave_only_detectable_or_servable_state(
    tmp_path: Path,
    point: str,
    phase: str,
) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", 1)},
    )
    assembler = _publisher(tmp_path, repository)
    request = assembler.request(
        output_staging_directory=assembler.publication_directory / ".stage-crash"
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == f"{phase}:{point}":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        assembler.assemble(request, crash_hook=crash)

    manifest_path = assembler.publication_directory / projection_manifest_name(request)
    orphans = detect_projection_orphans(assembler.publication_directory)
    if point == "projection.manifest_publication" and phase == "after":
        handle = bind_projection(manifest_path, expected=assembler.accepted)
        assert handle.fixture("one") is not None
        assert any(orphan.kind == "staging-build" for orphan in orphans)
    else:
        assert not manifest_path.exists()
        if point != "projection.prebuild" or phase != "before":
            assert orphans

    # Detection is intentionally non-mutating.
    for orphan in orphans:
        if orphan.kind != "missing-piece":
            assert Path(orphan.path).exists()


def test_orphan_detector_reports_malformed_manifest_and_unreferenced_piece(
    tmp_path: Path,
) -> None:
    publication = tmp_path / "published"
    publication.mkdir()
    bad_manifest = publication / ("projection-" + "0" * 64 + ".json")
    bad_manifest.write_bytes(b"{torn")
    piece = publication / ("piece-" + "1" * 64 + "-0000.sqlite")
    piece.write_bytes(b"orphan")

    orphans = detect_projection_orphans(publication)
    assert [(orphan.kind, Path(orphan.path).name) for orphan in orphans] == [
        ("malformed-manifest", bad_manifest.name),
        ("unreferenced-piece", piece.name),
    ]
    assert bad_manifest.exists()
    assert piece.exists()
