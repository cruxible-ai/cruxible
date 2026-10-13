"""Projection pieces past retention are evicted, manifest first, and rebuilt on a miss."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.indexes import projection_retention
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.sqlite import load_projection_manifest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.retention import RetentionSettings
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write

WI1 = f"{KIND}/wi-1"


def _projections(instance: PlaybillInstance) -> Path:
    return instance._validated_paths(instance.root, instance.descriptor.storage)["projections"]


def _manifest_oids(instance: PlaybillInstance) -> set[str]:
    return {
        load_projection_manifest(path).git_oid
        for path in _projections(instance).glob("projection-*.json")
    }


def _pieces(instance: PlaybillInstance) -> set[str]:
    return {path.name for path in _projections(instance).glob("piece-*.sqlite")}


def _coordinate(instance: PlaybillInstance, generation: int) -> AcceptedProjectionCoordinate:
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return instance.coordinate_for_oid(oid)


def _history_oids(instance: PlaybillInstance) -> list[str]:
    with instance.accepted_history_reader() as history:
        return [history.generation(item).git_oid for item in range(history.sequence + 1)]


def _referenced_pieces(instance: PlaybillInstance) -> set[str]:
    return {
        piece.name
        for path in _projections(instance).glob("projection-*.json")
        for piece in load_projection_manifest(path).pieces
    }


def test_activation_keeps_the_head_and_the_last_generations_only(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    instance.retention = RetentionSettings(generations=2)
    for value in ("ready", "blocked", "done", "ready"):
        _write(instance, _set(WI1, "status", value))
    oids = _history_oids(instance)
    # A write holds its base while it activates, so that generation goes on
    # the next pass; nothing older than the window and the base is left.
    assert _manifest_oids(instance) <= set(oids[-3:])
    instance.evict_projection_pieces()
    assert _manifest_oids(instance) == set(oids[-2:])
    # Every piece left is one a kept manifest names: none is orphaned.
    assert _pieces(instance) == _referenced_pieces(instance)


def test_an_evicted_coordinate_rebuilds_when_bound_and_is_evicted_again(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    instance.retention = RetentionSettings(generations=1)
    _write(instance, _set(WI1, "status", "ready"))
    _write(instance, _set(WI1, "status", "blocked"))
    with instance.accepted_history_reader() as history:
        head = history.sequence
    behind = _coordinate(instance, head - 1)
    instance.evict_projection_pieces()
    assert behind.git_oid not in _manifest_oids(instance)
    with instance.bind_accepted_projection(behind) as projection:
        assert projection.accepted.git_oid == behind.git_oid
        assert behind.git_oid in _manifest_oids(instance)
    _write(instance, _set(WI1, "status", "done"))
    instance.evict_projection_pieces()
    assert behind.git_oid not in _manifest_oids(instance)
    assert _pieces(instance) == _referenced_pieces(instance)


def test_a_held_coordinate_survives_eviction_until_its_handle_closes(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    instance.retention = RetentionSettings(generations=1)
    _write(instance, _set(WI1, "status", "ready"))
    behind = instance.accepted_coordinate()
    _write(instance, _set(WI1, "status", "blocked"))
    handle = instance.bind_accepted_projection(behind)
    try:
        _write(instance, _set(WI1, "status", "done"))
        instance.evict_projection_pieces()
        assert behind.git_oid in _manifest_oids(instance)
        assert handle.typed is not None
    finally:
        handle.close()
    assert len(instance.evict_projection_pieces()) == 1
    assert behind.git_oid not in _manifest_oids(instance)


def test_open_evicts_past_retention_and_an_evicted_base_rebuilds_when_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime import retention

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked", "done"):
        _write(instance, _set(WI1, "status", value))
    oids = _history_oids(instance)
    behind = instance.coordinate_for_oid(oids[-3])
    assert oids[-3] in _manifest_oids(instance)
    # Reopening under a one-generation window keeps only the head: an open
    # proposal is evaluated at the head, and an older base is rebuilt on a read.
    monkeypatch.setattr(retention, "DEFAULT_RETENTION", RetentionSettings(generations=1))
    monkeypatch.setattr(
        "cruxible_core.runtime.instance.DEFAULT_RETENTION", RetentionSettings(generations=1)
    )
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert _manifest_oids(reopened) == {oids[-1]}
    assert _pieces(reopened) == _referenced_pieces(reopened)
    with reopened.bind_accepted_projection(behind) as projection:
        assert projection.accepted.git_oid == behind.git_oid


def test_an_activation_during_a_pass_never_loses_the_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked", "done"):
        _write(instance, _set(WI1, "status", value))
    before = instance.accepted_coordinate()
    evict = projection_retention.evict_projection
    writers: list[threading.Thread] = []

    def interleaved(directory: Path, name: str) -> bool | None:
        if not writers:
            # Another request accepts a generation while this pass runs.
            writer = threading.Thread(target=_write, args=(instance, _set(WI1, "title", "New")))
            writers.append(writer)
            writer.start()
            deadline = time.monotonic() + 60
            while instance.accepted_coordinate() == before and time.monotonic() < deadline:
                time.sleep(0.01)
            assert instance.accepted_coordinate() != before
        return evict(directory, name)

    monkeypatch.setattr(projection_retention, "evict_projection", interleaved)
    instance.retention = RetentionSettings(generations=1)
    instance.evict_projection_pieces()
    writers[0].join(60)
    head = instance.accepted_coordinate()
    assert head != before and head.git_oid in _manifest_oids(instance)
    with instance.bind_accepted_projection(head) as projection:
        assert projection.accepted.git_oid == head.git_oid


def test_a_bind_holds_its_piece_and_one_evicted_before_its_hold_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.indexes import sqlite

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked"):
        _write(instance, _set(WI1, "status", value))
    behind = _coordinate(instance, 1)
    directory = _projections(instance)
    handle = instance.bind_accepted_projection(behind)
    name = handle.manifest_path.name
    try:
        # A held piece refuses the exclusive lock eviction takes, in any process.
        with projection_retention.retention_lock(directory):
            assert projection_retention.evict_projection(directory, name) is False
        assert behind.git_oid in _manifest_oids(instance)
    finally:
        handle.close()
    # Evicted between the manifest check and the hold: the bind binds again.
    hold = sqlite.lock_piece_shared
    evicted: list[bool] = []

    def evict_first(piece: Path, manifest: Path) -> int:
        if not evicted:
            with projection_retention.retention_lock(directory):
                evicted.append(bool(projection_retention.evict_projection(directory, name)))
        return hold(piece, manifest)

    monkeypatch.setattr(sqlite, "lock_piece_shared", evict_first)
    with instance.bind_accepted_projection(behind) as projection:
        assert evicted == [True]
        assert projection.accepted.git_oid == behind.git_oid
    assert behind.git_oid in _manifest_oids(instance)


def test_each_pass_handles_only_the_generations_that_crossed_the_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime import instance as instance_module

    instance, _owner = seed_write_surface(tmp_path)
    instance.retention = RetentionSettings(generations=2)
    for value in ("ready", "blocked", "done", "ready"):
        _write(instance, _set(WI1, "status", value))
    tried: list[str] = []
    evict = instance_module.evict_or_wait

    def counted(directory: Path, name: str, sequence: int) -> Any:
        tried.append(name)
        return evict(directory, name, sequence)

    monkeypatch.setattr(instance_module, "evict_or_wait", counted)
    for value in ("blocked", "done", "ready"):
        tried.clear()
        _write(instance, _set(WI1, "status", value))
        # One generation leaves the window per acceptance, whatever the history.
        assert len(tried) == 1
    tried.clear()
    # No acceptance and no release since: nothing to try.
    assert instance.evict_projection_pieces() == ()
    assert tried == []


def test_a_held_population_is_tried_again_only_as_its_holds_are_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.indexes import sqlite
    from cruxible_core.runtime import instance as instance_module

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    instance.retention = RetentionSettings(generations=1)
    instance.evict_projection_pieces()
    directory = _projections(instance)
    tried: list[str] = []

    def counted(directory: Path, name: str, sequence: int) -> Any:
        tried.append(name)
        return "absent"

    monkeypatch.setattr(instance_module, "evict_or_wait", counted)
    probes: dict[int, list[int]] = {}
    for size in (64, 512):
        held = [directory / f"projection-held-{size}-{index}.json" for index in range(size)]
        # Each piece is held here and was skipped as held at an earlier pass.
        for manifest in held:
            sqlite._HOLDS.taken(sqlite._hold_key(manifest))
            with sqlite.held_here(manifest, 0) as here:
                assert here
        counts: list[int] = []
        for _pass in range(3):
            tried.clear()
            instance.evict_projection_pieces()
            counts.append(len(tried))
        # Released holds are what make a piece worth trying again: exactly those.
        for manifest in held[:5]:
            sqlite._HOLDS.dropped(sqlite._hold_key(manifest))
        tried.clear()
        instance.evict_projection_pieces()
        counts.append(len(tried))
        assert sorted(tried) == sorted(manifest.name for manifest in held[:5])
        for manifest in held[5:]:
            sqlite._HOLDS.dropped(sqlite._hold_key(manifest))
        sqlite.released_pieces(directory)
        probes[size] = counts
    assert probes[64] == probes[512] == [0, 0, 0, 5]


def test_a_piece_another_process_holds_waits_for_the_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl
    import os

    from cruxible_core.runtime import retention

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    behind = instance.accepted_coordinate()
    with instance.bind_accepted_projection(behind) as handle:
        manifest_path = handle.manifest_path
        piece = manifest_path.parent / handle.manifest.pieces[0].name
    # A shared flock no handle in this process counts: another process's hold.
    descriptor = os.open(piece, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_SH)
    try:
        instance.retention = RetentionSettings(generations=1)
        _write(instance, _set(WI1, "status", "blocked"))
        assert behind.git_oid in _manifest_oids(instance)
        tried: list[str] = []
        evict = projection_retention.evict_projection

        def counted(directory: Path, name: str) -> bool | None:
            tried.append(name)
            return evict(directory, name)

        monkeypatch.setattr(projection_retention, "evict_projection", counted)
        # Not retried pass after pass while it stays held.
        assert instance.evict_projection_pieces() == ()
        assert tried == []
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    monkeypatch.setattr(retention, "DEFAULT_RETENTION", RetentionSettings(generations=1))
    monkeypatch.setattr(
        "cruxible_core.runtime.instance.DEFAULT_RETENTION", RetentionSettings(generations=1)
    )
    # The next open reads the directory once and evicts it.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert behind.git_oid not in _manifest_oids(reopened)


@pytest.mark.parametrize("boundary", ["before_load", "during_read"])
def test_a_manifest_evicted_before_its_hold_rebinds_by_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from cruxible_core.indexes import sqlite

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked"):
        _write(instance, _set(WI1, "status", value))
    behind = _coordinate(instance, 1)
    directory = _projections(instance)
    with instance.bind_accepted_projection(behind) as handle:
        name = handle.manifest_path.name
    evicted: list[bool] = []

    def evict_once() -> None:
        if not evicted:
            evicted.append(False)
            with projection_retention.retention_lock(directory):
                evicted[0] = bool(projection_retention.evict_projection(directory, name))

    if boundary == "before_load":
        # After the bind's presence check, before it reads the manifest.
        load = sqlite.load_projection_manifest

        def evicting_load(path: Path) -> Any:
            if path.name == name:
                evict_once()
            return load(path)

        monkeypatch.setattr(sqlite, "load_projection_manifest", evicting_load)
    else:
        # After the loader's regular-file check, as it reads the bytes.
        read_bytes = Path.read_bytes

        def evicting_read(path: Path) -> bytes:
            if path.name == name and not evicted:
                evict_once()
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", evicting_read)
    with instance.bind_accepted_projection(behind) as projection:
        assert evicted == [True]
        assert projection.accepted.git_oid == behind.git_oid
    assert behind.git_oid in _manifest_oids(instance)


def test_eviction_deletes_the_manifest_before_its_piece(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    _write(instance, _set(WI1, "status", "blocked"))
    directory = _projections(instance)
    seen: list[tuple[int, int]] = []
    fsync = projection_retention._fsync_directory

    def observed(path: Path) -> None:
        seen.append(
            (len(list(directory.glob("projection-*.json"))), len(list(directory.glob("piece-*"))))
        )
        fsync(path)

    monkeypatch.setattr(projection_retention, "_fsync_directory", observed)
    instance.retention = RetentionSettings(generations=1)
    before = (len(list(directory.glob("projection-*.json"))), len(_pieces(instance)))
    assert len(instance.evict_projection_pieces()) == before[0] - 1
    # After each manifest unlink, its piece is still there; then it goes.
    first = seen[0]
    assert first == (before[0] - 1, before[1])
    assert seen[1] == (before[0] - 1, before[1] - 1)


def test_an_activation_hashes_its_new_piece_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler import assembler
    from cruxible_core.indexes import sqlite

    instance, _owner = seed_write_surface(tmp_path)
    hashed: list[Any] = []
    digest = sqlite.physical_file_digest

    def counted(path: Path) -> Any:
        hashed.append(path)
        return digest(path)

    monkeypatch.setattr(assembler, "physical_file_digest", counted)
    monkeypatch.setattr(sqlite, "physical_file_digest", counted)
    _write(instance, _set(WI1, "status", "ready"))
    assert len(hashed) == 1


def test_a_manifest_read_that_found_it_evicted_rebinds_even_once_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked"):
        _write(instance, _set(WI1, "status", value))
    behind = _coordinate(instance, 1)
    with instance.bind_accepted_projection(behind) as handle:
        manifest = handle.manifest_path
    read_bytes = Path.read_bytes
    interleaved: list[bool] = []

    def evicted_then_rebuilt(path: Path) -> bytes:
        if path == manifest and not interleaved:
            interleaved.append(True)
            with projection_retention.retention_lock(path.parent):
                assert projection_retention.evict_projection(path.parent, path.name)
            try:
                read_bytes(path)
            except FileNotFoundError as gone:
                # Another reader rebuilds the coordinate before this read's
                # failure is classified: the path is back, the read still failed.
                with instance.bind_accepted_projection(behind) as rebuilt:
                    assert rebuilt.accepted.git_oid == behind.git_oid
                assert path.exists()
                raise gone
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", evicted_then_rebuilt)
    with instance.bind_accepted_projection(behind) as projection:
        assert interleaved == [True]
        assert projection.accepted.git_oid == behind.git_oid


def test_a_present_malformed_manifest_is_refused_not_rebuilt(tmp_path: Path) -> None:
    from cruxible_core.indexes import sqlite

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked"):
        _write(instance, _set(WI1, "status", value))
    behind = _coordinate(instance, 1)
    with instance.bind_accepted_projection(behind) as handle:
        manifest = handle.manifest_path
    manifest.chmod(0o600)
    manifest.write_bytes(b"{}")
    with pytest.raises(sqlite.ProjectionIntegrityError) as refused:
        instance.bind_accepted_projection(behind)
    assert not isinstance(refused.value, sqlite.ProjectionEvicted)
    assert sqlite._hold_key(manifest) not in sqlite._HOLDS.counts
