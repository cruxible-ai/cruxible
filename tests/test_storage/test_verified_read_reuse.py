"""Remembered verification never outlives the exact file and path binding it proved."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillCasError, PlaybillFormatError
from cruxible_core.ledger import git as ledger_git
from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage import cas
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore

READ = BodyAccessContext(principal_id="owner", can_read_body=True)


def _store(tmp_path: Path) -> tuple[ContentAddressedBodyStore, str, Path]:
    root = tmp_path / "managed" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    digest = store.store(b"good").digest
    assert store.read(digest, access=READ) == b"good"  # warm the remembered proof
    return store, digest, store._path(digest)


def test_a_file_replaced_just_before_a_warm_read_is_refused(tmp_path, monkeypatch):
    store, digest, path = _store(tmp_path)
    opened = os.open

    def replace_then_open(target, flags, *args, **kwargs):
        if str(target) == path.name:
            evil = path.with_name("evil")
            evil.write_bytes(b"evil")
            os.replace(evil, path)
            monkeypatch.setattr(cas.os, "open", opened)
        return opened(target, flags, *args, **kwargs)

    monkeypatch.setattr(cas.os, "open", replace_then_open)
    with pytest.raises(PlaybillCasError, match="do not match"):
        store.read(digest, access=READ)


def test_a_file_rewritten_in_place_after_warming_is_refused(tmp_path):
    store, digest, path = _store(tmp_path)
    path.chmod(0o600)
    with path.open("r+b") as handle:  # same inode, same size, new bytes
        handle.write(b"evil")
    with pytest.raises(PlaybillCasError, match="do not match"):
        store.read(digest, access=READ)


@pytest.mark.parametrize("level", ["algorithm", "cas", "managed"])
def test_a_swapped_ancestor_never_redirects_a_retained_store(tmp_path, level):
    store, digest, _path = _store(tmp_path)
    ancestor = {
        "algorithm": store._algorithm_root,
        "cas": store._algorithm_root.parent,
        "managed": store._algorithm_root.parent.parent,
    }[level]
    outside = tmp_path / "outside"
    outside.mkdir()
    # Move the directory out of custody and put a symlink to an empty stand-in
    # at its path: the retained store must keep addressing what it validated.
    os.replace(ancestor, outside / "moved")
    stand_in = outside / "stand-in"
    stand_in.mkdir()
    ancestor.symlink_to(stand_in, target_is_directory=True)

    assert store.read(digest, access=READ) == b"good"
    written = store.store(b"written after the swap").digest
    assert store.read(written, access=READ) == b"written after the swap"
    assert list(stand_in.rglob("*")) == []


def test_a_symlinked_shard_or_object_is_refused(tmp_path):
    store, digest, path = _store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / path.name).write_bytes(b"good")
    shard = path.parent
    os.replace(shard, tmp_path / "moved-shard")
    shard.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PlaybillCasError, match="not trustworthy"):
        store.read(digest, access=READ)


class _Layout:
    def model_dump(self) -> dict[str, str]:
        return {"nested": "a/b"}


def test_a_swapped_ancestor_of_a_warm_storage_path_is_refused(tmp_path):
    root = tmp_path / "instance"
    (root / "a" / "b").mkdir(parents=True)
    layout = _Layout()
    assert PlaybillInstance._validated_paths(root, layout)["nested"] == (root / "a" / "b")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.replace(root / "a", outside / "a")
    (root / "a").symlink_to(outside / "a", target_is_directory=True)
    with pytest.raises(PlaybillFormatError, match="escapes"):
        PlaybillInstance._validated_paths(root, layout)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_child_forked_while_a_reader_is_busy_reads_without_deadlock(tmp_path):
    import threading

    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha1",
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )
    oid = ledger._git(["hash-object", "-w", "--stdin"], input_bytes=b"blob").decode().strip()
    assert ledger.read_blobs([oid]) == {oid: b"blob"}
    reader = ledger_git._batch_reader(ledger.path)
    # Another thread holds the reader while this one forks.
    held, release = threading.Event(), threading.Event()

    def busy() -> None:
        with reader._lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    pid = os.fork()  # waits for the busy reader, then forks
    if pid == 0:  # pragma: no cover - exercised in the child
        try:
            ok = ledger.read_blobs([oid]) == {oid: b"blob"}
        except BaseException:
            ok = False
        os._exit(0 if ok else 1)
    worker.join(5)
    deadline_status = None
    for _ in range(100):
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            deadline_status = status
            break
        threading.Event().wait(0.05)
    if deadline_status is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("the forked child deadlocked on an inherited reader")
    assert os.waitstatus_to_exitcode(deadline_status) == 0
    # The parent's reader still answers after the fork.
    assert ledger.read_blobs([oid]) == {oid: b"blob"}


def test_availability_rehashes_what_a_remembered_proof_would_trust(tmp_path):
    store, digest, path = _store(tmp_path)
    assert store.availability(digest) == "present"
    path.chmod(0o600)
    with path.open("r+b") as handle:
        handle.write(b"rot!")
    # Rot that kept the file's identity: the remembered proof still vouches.
    with cas._VERIFIED_LOCK:
        cas._VERIFIED[store._memo_key(digest)] = cas._file_identity(path.stat())
    assert store.verify(digest) is True
    assert store.availability(digest) == "corrupt"
    path.unlink()
    assert store.availability(digest) == "missing"
