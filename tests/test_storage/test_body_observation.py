"""Every CAS access inside ``observe_bodies`` names the object it consulted, or says it cannot.

The stored ``next`` queue is served only while the bodies its fold consulted
keep their identity, so an access that returns an answer without recording the
object behind it would let a fold look complete with an input missing. Outside
an observation the store does exactly the filesystem work it did before.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.errors import CasError
from cruxible_core.storage import cas
from cruxible_core.storage.cas import (
    BodyAccessContext,
    BodyObservation,
    ContentAddressedBodyStore,
    observe_bodies,
)

READ = BodyAccessContext(principal_id="owner", can_read_body=True)

ACCESSES: dict[str, Callable[[ContentAddressedBodyStore, str], Any]] = {
    "peek": lambda store, digest: store.peek(digest, 4),
    "availability": lambda store, digest: store.availability(digest),
    "file_identity": lambda store, digest: store.file_identity(digest),
    "verify": lambda store, digest: store.verify(digest),
    "read": lambda store, digest: store.read(digest, access=READ),
    "metadata": lambda store, digest: store.metadata(digest, access=READ),
}
# The methods that open an object without blocking, so a FIFO can be planted.
NONBLOCKING = ("peek", "availability", "file_identity")
# The methods whose open and read failures are answered rather than raised.
ANSWERED_FAILURES = ("peek", "availability")


def _store(tmp_path: Path) -> tuple[ContentAddressedBodyStore, str, Path]:
    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    digest = store.store(b"good").digest
    return store, digest, store._path(digest)


def _observed(call: Callable[[], Any]) -> BodyObservation:
    with observe_bodies() as observation:
        try:
            call()
        except CasError:
            pass  # a refusal still rests on the object it refused
    return observation


@pytest.mark.parametrize("method", ACCESSES)
def test_a_successful_access_records_the_object_identity(tmp_path, method):
    store, digest, path = _store(tmp_path)
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert observation.identities == {digest: cas._file_identity(os.stat(path))}
    assert observation.consistent and observation.complete


@pytest.mark.parametrize("method", ACCESSES)
def test_a_missing_object_in_an_existing_shard_records_its_absence(tmp_path, method):
    store, digest, path = _store(tmp_path)
    path.unlink()
    assert path.parent.is_dir()
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert observation.identities == {digest: None}
    assert observation.consistent and observation.complete


@pytest.mark.parametrize("method", ACCESSES)
def test_a_missing_shard_records_the_objects_absence(tmp_path, method):
    store, digest, path = _store(tmp_path)
    path.unlink()
    path.parent.rmdir()
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert observation.identities == {digest: None}
    assert observation.consistent and observation.complete


@pytest.mark.parametrize("method", NONBLOCKING)
def test_a_nonregular_object_records_its_identity(tmp_path, method):
    store, digest, path = _store(tmp_path)
    path.unlink()
    os.mkfifo(path, 0o600)
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert observation.identities == {digest: cas._file_identity(os.stat(path))}
    assert observation.consistent and observation.complete


@pytest.mark.parametrize("method", ANSWERED_FAILURES)
def test_an_open_failure_marks_the_observation_incomplete(tmp_path, monkeypatch, method):
    store, digest, path = _store(tmp_path)
    opened = os.open

    def refuse(target, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(target) == path.name:
            raise OSError(errno.EACCES, "denied")
        return opened(target, flags, *args, **kwargs)

    monkeypatch.setattr(cas.os, "open", refuse)
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert not observation.complete


@pytest.mark.parametrize("method", ANSWERED_FAILURES)
def test_a_read_failure_marks_the_observation_incomplete(tmp_path, monkeypatch, method):
    store, digest, _path = _store(tmp_path)

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EIO, "unreadable")

    monkeypatch.setattr(cas.os, "read", fail)
    observation = _observed(lambda: ACCESSES[method](store, digest))
    assert not observation.complete


@pytest.mark.parametrize("method", ("peek", "availability"))
@pytest.mark.parametrize("observing", (False, True))
def test_a_descriptor_read_takes_its_closing_stat_only_when_observed(
    tmp_path, monkeypatch, method, observing
):
    store, digest, _path = _store(tmp_path)
    store.read(digest, access=READ)  # warm, as an inventory or sweep would be
    stats = 0
    fstat = os.fstat

    def counted(descriptor):  # type: ignore[no-untyped-def]
        nonlocal stats
        stats += 1
        return fstat(descriptor)

    monkeypatch.setattr(cas.os, "fstat", counted)
    assert cas._BODY_OBSERVATION.get() is None
    if observing:
        observation = _observed(lambda: ACCESSES[method](store, digest))
        assert digest in observation.identities
    else:
        ACCESSES[method](store, digest)
    assert stats == (2 if observing else 1)


def test_the_observation_ends_with_its_context(tmp_path):
    store, digest, _path = _store(tmp_path)
    with pytest.raises(RuntimeError):
        with observe_bodies():
            raise RuntimeError("fold failed")
    assert cas._BODY_OBSERVATION.get() is None
    store.read(digest, access=READ)
    assert cas._BODY_OBSERVATION.get() is None
