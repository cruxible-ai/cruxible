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

    from cruxible_core.runtime import instance as instance_module

    instance, _owner = seed_write_surface(tmp_path)
    for value in ("ready", "blocked", "done"):
        _write(instance, _set(WI1, "status", value))
    before = instance.accepted_coordinate()
    evict = instance_module.evict_projection
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

    monkeypatch.setattr(instance_module, "evict_projection", interleaved)
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
    evict = instance_module.evict_projection

    def counted(directory: Path, name: str) -> bool | None:
        tried.append(name)
        return evict(directory, name)

    monkeypatch.setattr(instance_module, "evict_projection", counted)
    for value in ("blocked", "done", "ready"):
        tried.clear()
        left_beyond = len(instance._beyond_window)
        _write(instance, _set(WI1, "status", value))
        # One generation leaves the window per acceptance, whatever the
        # history, plus any piece a read held at an earlier pass.
        assert 1 <= len(tried) <= 1 + left_beyond


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
