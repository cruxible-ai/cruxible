"""A CAS object becomes addressable only once its bytes are complete.

``store`` once created the object at its final name and wrote into it there. A
concurrent ``store`` of the same bytes (two submissions of one shared body) met
the half-written file, verified it and failed "CAS object bytes do not match
their content address"; a reader saw it as corrupt. Bytes are now written under
a private temporary name, verified, and then linked to the object name.

A non-journal store holds its body's landing marker (``LandingLog.operation``)
throughout, so two such stores of one body serialize; journal payloads take no
marker, so their stores race on the publication protocol alone.

A store that published its body never takes anything back: an erase and a
fresh store of the same body by another writer, landing between the first
writer's link and its return, leave the second writer's object in place.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from cruxible_core.storage import cas, landing_log
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore

READ = BodyAccessContext(principal_id="test", can_read_body=True)


def test_a_store_of_bytes_another_writer_is_still_writing_never_sees_them_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    content = b"shared tree\n" * 4096
    digest = store.digest_bytes(content).tagged
    paused, release = threading.Event(), threading.Event()
    real_write = os.write
    first: dict[str, threading.Thread] = {}

    def write_part_then_pause(descriptor: int, data: bytes | memoryview) -> int:
        if threading.current_thread() is first.get("writer") and not paused.is_set():
            written = real_write(descriptor, bytes(data[:16]))
            paused.set()
            release.wait(10)
            return written
        return real_write(descriptor, data)

    monkeypatch.setattr(cas.os, "write", write_part_then_pause)
    landed_before = store.landings.head()
    outcome: dict[str, object] = {}

    def first_store() -> None:
        try:
            outcome["first"] = store.store(content, journal_payload=True)
        except BaseException as exc:  # noqa: BLE001
            outcome["first"] = exc

    writer = threading.Thread(target=first_store)
    first["writer"] = writer
    writer.start()
    try:
        assert paused.wait(10), "the first writer never started writing"
        # Mid-write, the object is not there yet, never there and corrupt.
        assert store.availability(digest) == "missing"
        second = store.store(content, journal_payload=True)
        assert second.digest == digest
    finally:
        release.set()
        writer.join(10)
    assert not isinstance(outcome["first"], BaseException), outcome["first"]
    assert outcome["first"].digest == digest  # type: ignore[union-attr]
    assert store.read(digest, access=READ) == content
    shard = store._algorithm_root / digest.removeprefix("sha256:")[:2]
    assert sorted(path.name for path in shard.iterdir()) == [digest.removeprefix("sha256:")]
    # Journal payloads are logged by the record that names them, never as landings.
    assert store.landings.head() == landed_before


def test_an_erase_and_restore_after_a_publication_keeps_the_second_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    other = ContentAddressedBodyStore(root)
    content = b"ordinary verified publication\n"
    digest = store.digest_bytes(content).tagged
    real_link = os.link
    republished: dict[str, object] = {}

    def link_then_erase_and_restore(src: str, dst: str, **kwargs: object) -> None:
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]
        if republished:
            return
        # The first writer's object is now public; another writer erases it
        # and stores the same body again before the first writer returns.
        republished["erased"] = other.erase(digest)
        republished["stored"] = other.store(content)
        republished["identity"] = other.file_identity(digest)

    monkeypatch.setattr(cas.os, "link", link_then_erase_and_restore)
    # A journal payload: a non-journal store would hold the landing marker the
    # erase waits on, so no other writer could land between its link and return.
    first = store.store(content, journal_payload=True)

    assert republished["erased"] is True
    assert first.digest == digest
    assert republished["stored"].digest == digest  # type: ignore[attr-defined]
    identity = republished["identity"]
    assert identity is not None
    # The second writer's object survives, as the same file, with whole bytes.
    current = other.file_identity(digest)
    assert current is not None and current[:2] == identity[:2]  # type: ignore[index]
    assert store.availability(digest) == "present"
    assert store.read(digest, access=READ) == content


def test_a_create_answered_enoent_by_a_concurrent_create_takes_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS answers ENOENT to the loser of two concurrent ``O_CREAT`` opens of one
    new name relative to a directory descriptor. The landing marker's create is
    retried past it, so the losing store still takes the marker and lands."""

    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    content = b"marker raced by a concurrent create\n"
    digest = store.digest_bytes(content).tagged
    marker_name = digest.removeprefix("sha256:")
    real_open = os.open
    answered: list[str] = []

    def lose_the_first_create(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        if path == marker_name and dir_fd is not None and flags & os.O_CREAT and not answered:
            # The concurrent winner creates the name; this open is told ENOENT.
            os.close(real_open(path, flags, mode, dir_fd=dir_fd))
            answered.append(path)
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(landing_log.os, "open", lose_the_first_create)
    stored = store.store(content)

    assert answered == [marker_name]
    assert stored.digest == digest
    head = store.landings.head()
    assert store.landings.present(digest, identity=head.identity, sequence=head.sequence)
    assert not store.landings.unfinished()


@pytest.mark.parametrize("stores", ["one store", "two stores over one root"])
def test_concurrent_stores_of_one_new_body_both_land(tmp_path: Path, stores: str) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    first = ContentAddressedBodyStore(root)
    second = first if stores == "one store" else ContentAddressedBodyStore(root)
    for round_ in range(16):
        content = f"one body two writers {round_}\n".encode()
        barrier = threading.Barrier(2)
        outcome: dict[int, object] = {}

        def submit(slot: int, store: ContentAddressedBodyStore) -> None:
            barrier.wait(10)
            try:
                outcome[slot] = store.store(content).digest
            except BaseException as exc:  # noqa: BLE001
                outcome[slot] = exc

        writers = [
            threading.Thread(target=submit, args=(slot, store))
            for slot, store in enumerate((first, second))
        ]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(10)
        digest = first.digest_bytes(content).tagged
        assert outcome == {0: digest, 1: digest}
        assert first.read(digest, access=READ) == content
    assert not first.landings.unfinished()
