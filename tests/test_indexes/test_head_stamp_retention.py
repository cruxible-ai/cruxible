"""Pinned reads of older coordinates never push the serving head's stamp out.

Only the newest few source-authentication stamps are kept beside the pieces, and
only stamps on disk when a process first binds there are trusted. Each pinned
read of an older coordinate that has to authenticate its piece writes a stamp of
its own; once more of those than the ring holds had landed, the head's stamp was
gone, and the next process to bind the head re-derived every typed row from
source (15-17 s on real state). The head's stamp is now kept however many older
stamps arrive, and protection follows the serving pointer when the head moves.

Each older-coordinate authentication is represented by the stamp write it ends
with (`_record_authentication_stamp`, the call `require_source_authentication`
makes); the seeded world has fewer accepted generations than the ring holds.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.indexes import sqlite as playbill_projection
from cruxible_core.indexes import typed_sqlite
from cruxible_core.indexes.projection import (
    AcceptedProjectionCoordinate,
    AssemblerResult,
    ProjectionManifest,
)
from cruxible_core.indexes.serving import publish_serving_manifest
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._knowledge_loop_support import seed_claims

_RING = playbill_projection._SOURCE_AUTHENTICATION_STAMPS_RETAINED
_FAILURE_BOUND_SECONDS = 30.0


def _count_authentications(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = typed_sqlite.authenticate_source_rows

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(typed_sqlite, "authenticate_source_rows", counted)
    return calls


def _older_coordinate_reads(
    directory: Path,
    accepted: AcceptedProjectionCoordinate,
    manifest: ProjectionManifest,
    count: int,
) -> None:
    """Land ``count`` distinct older-coordinate authentication stamps."""

    for index in range(count):
        older = accepted.model_copy(
            update={"git_oid": hashlib.sha256(f"older-{index}".encode()).hexdigest()}
        )
        playbill_projection._record_authentication_stamp(directory, older, manifest)


def _stamps(directory: Path) -> list[dict[str, object]]:
    path = directory / playbill_projection.SOURCE_AUTHENTICATION_STAMPS
    loaded = json.loads(path.read_bytes())
    assert isinstance(loaded, list)
    return loaded


def _publish(directory: Path, manifest_path: Path, manifest: ProjectionManifest) -> None:
    """Point serving at a built projection, as activation does."""

    result = AssemblerResult.model_construct(manifest_path=str(manifest_path), manifest=manifest)
    publish_serving_manifest(directory, result)


class _World:
    """A seeded instance, its first head, and an earlier generation to activate."""

    def __init__(self, tmp_path: Path) -> None:
        self.instance, _owner = seed_claims(tmp_path)
        self.first_head = self.instance.accepted_coordinate()
        with self.instance.bind_accepted_projection(self.first_head) as handle:
            self.directory = handle.index_path.parent
            self.first_manifest = handle.manifest
        self.earlier = self.instance.coordinate_for_oid(self.instance.accepted_history()[-2].oid)
        with self.instance.bind_accepted_projection(self.earlier) as handle:
            self.earlier_manifest = handle.manifest
            self.earlier_manifest_path = handle.manifest_path
        self.first_stamp = playbill_projection._authentication_stamp(
            self.first_head, self.first_manifest
        )
        self.earlier_stamp = playbill_projection._authentication_stamp(
            self.earlier, self.earlier_manifest
        )

    def activate_earlier(self) -> None:
        """Stamp the build, then publish it: the assembler's and activation's writes."""

        playbill_projection._record_authentication_stamp(
            self.directory, self.earlier, self.earlier_manifest
        )
        _publish(self.directory, self.earlier_manifest_path, self.earlier_manifest)

    def bind_earlier_in_a_new_process(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        calls = _count_authentications(monkeypatch)
        playbill_projection.reset_projection_verification_memo()
        with self.instance.bind_accepted_projection(self.earlier):
            pass
        return calls


def _bind_head(instance: PlaybillInstance) -> None:
    with instance.bind_accepted_projection(instance.accepted_coordinate()):
        pass


def test_a_head_read_after_many_older_reads_does_not_reauthenticate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as handle:
        directory = handle.index_path.parent
        manifest = handle.manifest
    head_stamp = playbill_projection._authentication_stamp(head, manifest)

    _older_coordinate_reads(directory, head, manifest, _RING + 3)

    calls = _count_authentications(monkeypatch)
    playbill_projection.reset_projection_verification_memo()  # a new process
    _bind_head(instance)
    assert calls == []
    stamps = _stamps(directory)
    assert len(stamps) == _RING
    assert head_stamp in stamps


def test_protection_follows_the_serving_pointer_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    assert world.earlier_stamp in _stamps(world.directory)
    _publish(world.directory, world.earlier_manifest_path, world.earlier_manifest)

    _older_coordinate_reads(world.directory, world.first_head, world.first_manifest, _RING + 3)

    assert world.bind_earlier_in_a_new_process(monkeypatch) == []
    stamps = _stamps(world.directory)
    assert len(stamps) == _RING
    assert world.earlier_stamp in stamps
    # The former head is an ordinary older stamp now and ages out.
    assert world.first_stamp not in stamps


def test_publication_restores_a_new_head_stamp_older_reads_pushed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assembler stamps a build before activation publishes it; reads in between
    protect the old head and can push the new stamp out. Publication puts it back."""

    world = _World(tmp_path)
    playbill_projection._record_authentication_stamp(
        world.directory, world.earlier, world.earlier_manifest
    )
    _older_coordinate_reads(world.directory, world.first_head, world.first_manifest, _RING + 3)
    assert world.earlier_stamp not in _stamps(world.directory)

    _publish(world.directory, world.earlier_manifest_path, world.earlier_manifest)

    stamps = _stamps(world.directory)
    assert len(stamps) == _RING
    assert world.earlier_stamp in stamps
    assert world.bind_earlier_in_a_new_process(monkeypatch) == []


def test_a_historical_read_overlapping_an_activation_keeps_the_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp write that read the ring before an activation never lands after it.

    The historical reader is stopped right after it has read the serving pointer
    and the ring. The activation then stamps its build and publishes it. Unguarded,
    the activation finishes while the reader is stopped, and the reader's ring --
    merged from the old pointer and the old ring -- replaces the one holding the
    new head's stamp. Guarded, the activation waits for the reader to finish.
    """

    world = _World(tmp_path)
    # Fill the ring so the earlier generation's build-time stamp is gone and
    # only the activation below can put it back.
    _older_coordinate_reads(world.directory, world.first_head, world.first_manifest, _RING + 3)
    assert world.earlier_stamp not in _stamps(world.directory)

    reader_has_read = threading.Event()
    release_reader = threading.Event()
    activation_waits = threading.Event()
    activation_done = threading.Event()
    errors: list[BaseException] = []

    original_read = playbill_projection._authentication_stamps
    original_flock = playbill_projection.fcntl.flock

    def paused_read(directory: Path) -> list[dict[str, object]]:
        stamps = original_read(directory)
        if threading.current_thread().name == "historical-reader":
            reader_has_read.set()
            assert release_reader.wait(_FAILURE_BOUND_SECONDS)
        return stamps

    def observed_flock(descriptor: Any, operation: int) -> None:
        if threading.current_thread().name == "activation":
            activation_waits.set()
        original_flock(descriptor, operation)

    monkeypatch.setattr(playbill_projection, "_authentication_stamps", paused_read)
    monkeypatch.setattr(playbill_projection.fcntl, "flock", observed_flock)

    def historical_read() -> None:
        try:
            _older_coordinate_reads(world.directory, world.first_head, world.first_manifest, 1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def activation() -> None:
        try:
            world.activate_earlier()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            activation_done.set()

    reader = threading.Thread(target=historical_read, name="historical-reader")
    activator = threading.Thread(target=activation, name="activation")
    reader.start()
    try:
        assert reader_has_read.wait(_FAILURE_BOUND_SECONDS)
        activator.start()
        # Either the activation is now waiting on the guard, or -- unguarded --
        # it ran to completion while the reader was stopped.
        while not (activation_waits.is_set() or activation_done.is_set()):
            activation_done.wait(0.01)
    finally:
        release_reader.set()
        reader.join(_FAILURE_BOUND_SECONDS)
        if activator.ident is not None:
            activator.join(_FAILURE_BOUND_SECONDS)
    assert errors == []
    monkeypatch.setattr(playbill_projection, "_authentication_stamps", original_read)
    monkeypatch.setattr(playbill_projection.fcntl, "flock", original_flock)

    assert world.earlier_stamp in _stamps(world.directory)
    assert world.bind_earlier_in_a_new_process(monkeypatch) == []
