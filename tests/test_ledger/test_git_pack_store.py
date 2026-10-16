"""Pack writes/readback interoperate with system Git, including repack deltas."""

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


def test_corrupted_packed_payload_is_refused(tmp_path):
    from cruxible_client.contracts.errors import GitError

    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha256",
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    root = ledger._write_tree({"file-name": b"payload"})
    pack = next((ledger.path / "objects/pack").glob("*.pack"))
    raw = pack.read_bytes()
    assert b"file-name" in raw  # level-zero tree stream
    pack.chmod(0o644)
    pack.write_bytes(raw.replace(b"file-name", b"fake-name"))
    with pytest.raises(GitError):
        PackStore(ledger.path).read(root)
