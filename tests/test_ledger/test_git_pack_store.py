"""Pack writes/readback interoperate with system Git, including repack deltas."""

import os
from unittest.mock import patch

import pytest

from cruxible_core.ledger.git import GitLedger
from cruxible_core.ledger.git_objects import PackStore


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
def test_pack_index_is_git_readable_fsck_clean_and_reads_git_deltas(tmp_path, algorithm):
    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=algorithm,
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    tree = {f"dir/item{i}": b"common prefix" * 1000 + str(i).encode() for i in range(80)}
    root = ledger._write_tree(tree)
    assert len(list((ledger.path / "objects/pack").glob("*.pack"))) == 1
    for path, body in tree.items():
        assert ledger._git(["cat-file", "blob", ledger._blob_oid(body)]) == body
    ledger._git(["fsck", "--strict", "--no-dangling", root])
    # External repack exercises both index discovery and OFS_DELTA decoding.
    commit = ledger._git(["commit-tree", root, "-m", "root"]).decode().strip()
    ledger._git(["update-ref", "refs/heads/main", commit])
    ledger._git(["repack", "-a", "-d", "-f", "--window=80", "--depth=40"])
    fresh = PackStore(ledger.path)
    assert {
        ledger._blob_oid(body): fresh.read(ledger._blob_oid(body))[1] for body in tree.values()
    } == {ledger._blob_oid(body): body for body in tree.values()}
    fresh.close()


def test_pack_file_work_is_constant_per_batch(tmp_path):
    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha256",
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    calls = []
    sync = os.fsync
    for n in (1, 100):
        with patch("os.fsync", side_effect=lambda fd: (calls.append(fd), sync(fd))[-1]):
            ledger._store_loose_objects(
                {ledger._blob_oid(str(i).encode()): ("blob", str(i).encode()) for i in range(n)}
            )
        assert len(calls) == 3
        calls.clear()
    assert not list((ledger.path / "objects").glob("[0-9a-f][0-9a-f]/*"))
