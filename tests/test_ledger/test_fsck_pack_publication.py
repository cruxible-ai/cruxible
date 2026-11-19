"""Immutable pack publication and serialized recovery scans."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread

import pytest

from cruxible_client.contracts.errors import GitError
from cruxible_core.ledger.git import GitLedger, _batch_reader, _command
from cruxible_core.ledger.git_objects import PackStore


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
def test_unreachable_scan_waits_for_pack_index_replacement(tmp_path, monkeypatch, algorithm):
    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=algorithm,
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    store = _batch_reader(ledger.path).store
    objects = {ledger._blob_oid(b"first"): ("blob", b"first")}
    pack = store.write(objects, durable=False)
    objects[ledger._blob_oid(b"second")] = ("blob", b"second")
    gap, resume, scan_attempted, fsck_entered = (Event() for _ in range(4))
    original_replace, original_git, original_lock = os.replace, GitLedger._git, store.lock

    class ObservedLock:
        def __enter__(self):
            if current_thread().name.startswith("fsck-scan"):
                scan_attempted.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    def publish(source, destination):
        original_replace(source, destination)
        if Path(destination).suffix == ".pack" and Path(destination) != pack:
            new_pack = Path(destination)
            assert new_pack.with_suffix(".pending").exists()
            assert not new_pack.with_suffix(".idx").exists()
            gap.set()
            assert resume.wait(20), "publication was never resumed"

    def git(self, arguments, **kwargs):
        if arguments[0] == "fsck" and current_thread().name.startswith("fsck-scan"):
            fsck_entered.set()
            scan_attempted.set()
        return original_git(self, arguments, **kwargs)

    monkeypatch.setattr(store, "lock", ObservedLock())
    monkeypatch.setattr(os, "replace", publish)
    monkeypatch.setattr(GitLedger, "_git", git)
    with (
        ThreadPoolExecutor(max_workers=1) as writer,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="fsck-scan") as scanner,
    ):
        writing = writer.submit(store.write, objects, durable=False, replacing=pack)
        scanning = None
        try:
            assert gap.wait(10), "writer never reached the pack/index gap"
            # Independent Git keeps using the unchanged old pair until the
            # new checksum-derived name has its own complete index.
            raw = _command(
                ["git", f"--git-dir={ledger.path}", "fsck", "--unreachable", "--no-reflogs"],
                check=False,
            )
            assert raw.returncode == 0, raw.stderr
            scanning = scanner.submit(ledger.unreachable_commits)
            assert scan_attempted.wait(10), "scanner never attempted the read"
            assert not fsck_entered.is_set(), "fsck ran over an in-flight pack/index pair"
        finally:
            resume.set()
            current_pack = writing.result(timeout=10)
        assert scanning is not None
        assert scanning.result(timeout=10) == ()
    assert fsck_entered.is_set()
    assert store.read(ledger._blob_oid(b"second")) == ("blob", b"second")
    # Serialization must not suppress actual, settled storage corruption.
    assert current_pack != pack
    assert not pack.exists()
    index = current_pack.with_suffix(".idx")
    index.chmod(0o644)
    index.write_bytes(b"broken index")
    with pytest.raises(GitError):
        ledger.unreachable_commits()


@pytest.fixture(params=["sha1", "sha256"])
def pack_ledger(tmp_path, request):
    return GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )


def test_protections_precede_pack_and_index_publication(pack_ledger, monkeypatch):
    ledger = pack_ledger
    store = _batch_reader(ledger.path).store
    events = []
    touch, replace = Path.touch, os.replace

    def protect(path, *args, **kwargs):
        assert not path.with_suffix(".pack").exists()
        assert not path.with_suffix(".idx").exists()
        result = touch(path, *args, **kwargs)
        events.append(path.suffix)
        # Raw fsck can inventory the repository at each marker checkpoint too.
        ledger._git(["fsck", "--no-dangling"])
        return result

    def publish(source, destination):
        path = Path(destination)
        assert path.with_suffix(".pending").exists()
        assert path.with_suffix(".keep").exists()
        if path.suffix == ".idx":
            assert path.with_suffix(".pack").exists()
        result = replace(source, destination)
        events.append(path.suffix)
        ledger._git(["fsck", "--no-dangling"])
        return result

    monkeypatch.setattr(Path, "touch", protect)
    monkeypatch.setattr(os, "replace", publish)
    path = store.write({ledger._blob_oid(b"first"): ("blob", b"first")}, keep=True, durable=False)
    assert events == [".pending", ".keep", ".pack", ".idx"]
    width = len(ledger._blob_oid(b"first")) // 2
    assert path.stem == "pack-" + path.read_bytes()[-width:].hex()


def test_identical_pair_is_reused_and_sealed_in_place(pack_ledger, monkeypatch):
    ledger = pack_ledger
    store = _batch_reader(ledger.path).store
    objects = {ledger._blob_oid(b"first"): ("blob", b"first")}
    path = store.write(objects, durable=False, keep=True)
    pair = [path, path.with_suffix(".idx")]
    before = [item.stat() for item in pair]
    synced = []
    fsync = os.fsync

    def sync(fd):
        synced.append(os.fstat(fd).st_ino)
        return fsync(fd)

    def forbidden(*args, **kwargs):
        raise AssertionError("an identical checksum pair was rewritten")

    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr(os, "fsync", sync)
    assert store.write(objects, durable=False, keep=True, replacing=path) == path
    assert not synced
    assert store.write(objects, durable=True, keep=True, replacing=path) == path
    assert not path.with_suffix(".pending").exists()
    assert synced == [item.st_ino for item in before] + [path.parent.stat().st_ino]
    assert [(item.stat().st_ino, item.stat().st_mtime_ns) for item in pair] == [
        (stat.st_ino, stat.st_mtime_ns) for stat in before
    ]
    # A later best-effort duplicate must not downgrade a durable pair.
    assert store.write(objects, durable=False) == path
    assert not path.with_suffix(".pending").exists()
    fresh = PackStore(ledger.path)
    try:
        assert fresh.read(next(iter(objects))) == ("blob", b"first")
    finally:
        fresh.close()


def test_durable_replacement_is_synced_before_old_pair_retirement(pack_ledger, monkeypatch):
    ledger = pack_ledger
    store = _batch_reader(ledger.path).store
    objects = {ledger._blob_oid(b"first"): ("blob", b"first")}
    old = store.write(objects, keep=True)
    objects[ledger._blob_oid(b"second")] = ("blob", b"second")
    synced = []
    fsync, unlink = os.fsync, Path.unlink

    def sync(fd):
        # Losing the last durable copy before this sync would break previously
        # accepted dependencies even when main has not yet moved.
        assert old.exists() and old.with_suffix(".idx").exists()
        synced.append(os.fstat(fd).st_ino)
        return fsync(fd)

    def retire(path, *args, **kwargs):
        if path == old.with_suffix(".idx"):
            assert old.parent.stat().st_ino in synced
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "unlink", retire)
    current = store.write(objects, replacing=old, keep=True)
    assert current != old
    assert synced == [
        current.stat().st_ino,
        current.with_suffix(".idx").stat().st_ino,
        current.parent.stat().st_ino,
    ]
    assert not any(old.with_suffix(suffix).exists() for suffix in (".idx", ".pack", ".keep"))
    assert store.read(ledger._blob_oid(b"first")) == ("blob", b"first")


def test_advisory_append_preserves_borrowed_durable_pair(pack_ledger, monkeypatch):
    ledger = pack_ledger
    store = _batch_reader(ledger.path).store
    first = ledger._blob_oid(b"first")
    objects = {first: ("blob", b"first")}
    old = store.write(objects)
    before = [old.stat().st_ino, old.with_suffix(".idx").stat().st_ino]

    def forbidden(fd):
        raise AssertionError("advisory append added a durable flush")

    monkeypatch.setattr(os, "fsync", forbidden)
    assert store.write(objects, durable=False, keep=True) == old
    objects[ledger._blob_oid(b"second")] = ("blob", b"second")
    current = store.write(objects, durable=False, keep=True, replacing=old)
    assert current != old
    assert [old.stat().st_ino, old.with_suffix(".idx").stat().st_ino] == before
    assert not old.with_suffix(".pending").exists()
    assert not old.with_suffix(".keep").exists()
    assert current.with_suffix(".pending").exists()
    assert current.with_suffix(".keep").exists()
    # Even losing the entire advisory append leaves the prior durable bytes.
    for suffix in (".idx", ".pack", ".pending", ".keep"):
        current.with_suffix(suffix).unlink()
    fresh = PackStore(ledger.path)
    try:
        assert fresh.read(first) == ("blob", b"first")
    finally:
        fresh.close()
