"""Whole-store Git scans must not race provisional pack/index replacement."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread

import pytest

from cruxible_client.contracts.errors import GitError
from cruxible_core.ledger.git import GitLedger, _batch_reader, _command


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
        if Path(destination) == pack:
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
            # Reproduce the race using unprotected Git, while the new pack
            # still has its predecessor's index. Exit bits depend on whether
            # Git detects an inaccessible pack or mismatched object contents.
            raw = _command(
                ["git", f"--git-dir={ledger.path}", "fsck", "--unreachable", "--no-reflogs"],
                check=False,
            )
            assert raw.returncode != 0, raw.stderr
            assert b"packfile" in raw.stderr and b"cannot be accessed" in raw.stderr
            scanning = scanner.submit(ledger.unreachable_commits)
            assert scan_attempted.wait(10), "scanner never attempted the read"
            assert not fsck_entered.is_set(), "fsck ran over an in-flight pack/index pair"
        finally:
            resume.set()
            writing.result(timeout=10)
        assert scanning is not None
        assert scanning.result(timeout=10) == ()
    assert fsck_entered.is_set()
    assert store.read(ledger._blob_oid(b"second")) == ("blob", b"second")
    # Serialization must not suppress actual, settled storage corruption.
    index = pack.with_suffix(".idx")
    index.chmod(0o644)
    index.write_bytes(b"broken index")
    with pytest.raises(GitError):
        ledger.unreachable_commits()
