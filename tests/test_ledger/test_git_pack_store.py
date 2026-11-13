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
    assert {
        ledger._blob_oid(body): fresh.info(ledger._blob_oid(body)) for body in tree.values()
    } == {ledger._blob_oid(body): ("blob", len(body)) for body in tree.values()}
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


def test_metadata_does_not_read_or_hash_payloads(tmp_path, monkeypatch):
    import hashlib
    import zlib

    store = PackStore(tmp_path)
    (tmp_path / "objects/pack").mkdir(parents=True)
    body = b"large body" * 100_000
    oid = hashlib.sha256(b"blob " + str(len(body)).encode() + b"\x00" + body).hexdigest()
    store.write({oid: ("blob", body)})

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata loaded an object payload")

    monkeypatch.setattr(store, "read", forbidden)
    monkeypatch.setattr(store, "_at", forbidden)
    assert store.info(oid) == ("blob", len(body))
    # Loose headers still answer even when the payload is absent/corrupt.
    loose_oid = "f" * 64
    loose = tmp_path / "objects" / loose_oid[:2] / loose_oid[2:]
    loose.parent.mkdir()
    loose.write_bytes(zlib.compress(b"blob 100000000\x00truncated"))
    inflated = bytearray()
    real_inflater = zlib.decompressobj

    class HeaderInflater:
        def __init__(self):
            self.inner = real_inflater()

        @property
        def unconsumed_tail(self):
            return self.inner.unconsumed_tail

        def decompress(self, data, limit):
            output = self.inner.decompress(data, limit)
            inflated.extend(output)
            return output

    monkeypatch.setattr(zlib, "decompressobj", HeaderInflater)
    assert store.info(loose_oid) == ("blob", 100000000)
    assert inflated == b"blob 100000000\x00"
    assert store.info("e" * 64) is None


def test_damaged_advisory_pack_is_absent_without_hiding_durable_objects(tmp_path):
    store = PackStore(tmp_path)
    (tmp_path / "objects/pack").mkdir(parents=True)
    # Use real identities so a durable read still exercises payload integrity.
    import hashlib

    durable_oid = hashlib.sha256(b"blob 7\x00durable").hexdigest()
    advisory_oid = hashlib.sha256(b"blob 8\x00advisory").hexdigest()
    store.write({durable_oid: ("blob", b"durable")})
    path = store.write({advisory_oid: ("blob", b"advisory")}, durable=False)
    assert path is not None
    path.chmod(0o644)
    path.write_bytes(b"torn")
    fresh = PackStore(tmp_path)
    assert fresh.read(advisory_oid) is None
    assert fresh.info(advisory_oid) is None
    assert fresh.read(durable_oid) == ("blob", b"durable")
    fresh.close()
    path.with_suffix(".idx").chmod(0o644)
    path.with_suffix(".idx").write_bytes(b"torn index")
    fresh = PackStore(tmp_path)
    assert fresh.read(advisory_oid) is None
    assert fresh.read(durable_oid) == ("blob", b"durable")
    fresh.close()


def test_metadata_batch_checks_pack_directory_once(tmp_path, monkeypatch):
    import hashlib

    store = PackStore(tmp_path)
    (tmp_path / "objects/pack").mkdir(parents=True)
    objects = {}
    for i in range(100):
        body = str(i).encode()
        oid = hashlib.sha256(f"blob {len(body)}\x00".encode() + body).hexdigest()
        objects[oid] = ("blob", body)
    store.write(objects)
    calls = []
    discover = store._discover

    def observe(width):
        calls.append(width)
        return discover(width)

    monkeypatch.setattr(store, "_discover", observe)
    assert store.infos(tuple(objects)) == {
        oid: ("blob", len(body)) for oid, (_, body) in objects.items()
    }
    assert calls == [32]


def _packed_ledger(tmp_path, batches):
    """A ledger whose reachable blobs sit in one durable pack per batch."""

    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha256",
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    tree: dict[str, bytes] = {}
    for batch in range(batches):
        tree.update({f"b{batch}/item{i}": f"{batch}:{i}".encode() for i in range(5)})
        root = ledger._write_tree(dict(tree))
    commit = ledger._git(["commit-tree", root, "-m", "root"]).decode().strip()
    ledger._git(["update-ref", "refs/heads/main", commit])
    blobs = {ledger._blob_oid(body): body for body in tree.values()}
    return ledger, blobs


def _retire_while_walking(monkeypatch, ledger):
    """Interleave a live `git repack -a -d` with discovery, step by step.

    Git publishes the consolidated pack first, then retires each replaced
    pair. The hook does the same around the reader: the first index read
    publishes the consolidated pack, and every superseded index is retired just
    before the reader opens it, so each walk of the old listing meets a vanished
    durable index. Deterministic: no thread, no sleep.
    """

    import pathlib

    directory = ledger.path / "objects" / "pack"
    old = {path for path in directory.glob("*.idx") if not path.with_suffix(".keep").exists()}
    assert len(old) > 1
    real = pathlib.Path.read_bytes
    published: list[bool] = []

    def read_bytes(self):
        if self in old:
            if not published:
                ledger._git(["repack", "-a", "-q"])
                published.append(True)
            old.discard(self)
            self.unlink()
            self.with_suffix(".pack").unlink()
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
    return published


@pytest.mark.parametrize("operation", ["read", "infos", "write"])
def test_discovery_survives_a_concurrent_repack_retiring_durable_indexes(
    tmp_path, monkeypatch, operation
):
    import hashlib

    ledger, blobs = _packed_ledger(tmp_path, batches=4)
    store = PackStore(ledger.path)
    published = _retire_while_walking(monkeypatch, ledger)
    if operation == "read":
        assert {oid: store.read(oid) for oid in blobs} == {
            oid: ("blob", body) for oid, body in blobs.items()
        }
    elif operation == "infos":
        assert store.infos(tuple(blobs)) == {
            oid: ("blob", len(body)) for oid, body in blobs.items()
        }
    else:
        body = b"written during a repack"
        oid = hashlib.sha256(f"blob {len(body)}\x00".encode() + body).hexdigest()
        store.write({oid: ("blob", body)})
        assert store.read(oid) == ("blob", body)
        assert {item: store.read(item) for item in blobs} == {
            item: ("blob", value) for item, value in blobs.items()
        }
    assert published, "the hook never interleaved a repack"
    store.close()


def test_metadata_finds_a_loose_object_packed_and_pruned_beneath_it(tmp_path, monkeypatch):
    import pathlib

    ledger, blobs = _packed_ledger(tmp_path, batches=1)
    body = b"loose until the repack"
    oid = ledger._git(["hash-object", "-w", "--stdin"], input_bytes=body).decode().strip()
    tree = ledger._git(["mktree"], input_bytes=f"100644 blob {oid}\tx\n".encode()).decode().strip()
    commit = ledger._git(["commit-tree", tree, "-m", "x"]).decode().strip()
    ledger._git(["update-ref", "refs/heads/loose", commit])
    store = PackStore(ledger.path)
    assert store.info(next(iter(blobs))) is not None  # discovery is warm
    loose = ledger.path / "objects" / oid[:2] / oid[2:]
    assert loose.is_file()
    real = pathlib.Path.open

    def open_(self, *args, **kwargs):
        if self == loose and loose.exists():
            # `repack -a -d` packs the loose object, then prunes the loose copy.
            ledger._git(["repack", "-a", "-d", "-q"])
            assert not loose.exists()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", open_)
    assert store.info(oid) == ("blob", len(body))
    store.close()
