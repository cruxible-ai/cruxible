"""Acceptance covers each new citation and its namespace once, before main moves."""

import os
import stat
from collections import Counter

import pytest

from cruxible_core.storage import cas
from cruxible_core.storage.cas import ContentAddressedBodyStore


def test_fallback_flushes_each_body_once_and_all_namespace_parents(tmp_path, monkeypatch):
    root = tmp_path / "instance" / "cas"
    root.mkdir(parents=True)
    store = ContentAddressedBodyStore(root)
    digests = tuple(
        item.digest for item in store.store_many([b"one", b"two"], journal_payload=True)
    )
    calls = Counter()
    real = os.fsync

    def flush(fd):
        metadata = os.fstat(fd)
        calls[(metadata.st_dev, metadata.st_ino)] += 1
        real(fd)

    monkeypatch.setattr(cas, "_sync_filesystem", lambda fd: False)
    monkeypatch.setattr(os, "fsync", flush)
    store.flush_cited((*digests, *digests))
    store.flush_cited(digests)
    for path in (store._path(d) for d in digests):
        metadata = path.stat()
        assert calls[(metadata.st_dev, metadata.st_ino)] == 1
    for path in {root, root.parent, root / "sha256", *(store._path(d).parent for d in digests)}:
        metadata = path.stat()
        assert calls[(metadata.st_dev, metadata.st_ino)] == 1


def test_linux_is_one_filesystem_barrier_without_per_file_fsync(tmp_path, monkeypatch):
    store = ContentAddressedBodyStore(tmp_path)
    digests = tuple(
        item.digest for item in store.store_many([b"one", b"two"], journal_payload=True)
    )
    calls = []
    monkeypatch.setattr(cas.sys, "platform", "linux")
    monkeypatch.setattr(cas, "_sync_filesystem", lambda fd: calls.append(fd) or True)
    monkeypatch.setattr(os, "fsync", lambda fd: pytest.fail("per-file Linux fsync"))
    store.flush_cited(digests)
    assert calls == [store._root_fd]
    assert store._durable == set(digests)


def test_failed_barrier_does_not_mark_citations_durable(tmp_path, monkeypatch):
    store = ContentAddressedBodyStore(tmp_path)
    digest = store.store(b"best effort", journal_payload=True).digest
    monkeypatch.setattr(cas, "_sync_filesystem", lambda fd: False)
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        store.flush_cited((digest,))
    assert digest not in store._durable


def test_settle_flush_covers_every_citation_before_main(tmp_path, monkeypatch):
    from cruxible_core.ledger.git import GitLedger
    from cruxible_core.storage.acceptance import cited_bodies
    from tests.test_procedures.test_procedure_settlement_delivery import run_settle, settle_world

    instance, root, line = settle_world(tmp_path)
    store = instance.body_store()
    flushed = Counter()
    namespaces = set()
    original_flush = os.fsync
    original_main = GitLedger.compare_and_set_main
    original_barrier = ContentAddressedBodyStore.flush_cited
    in_barrier = False
    covered = set()

    def flush(fd):
        if in_barrier:
            metadata = os.fstat(fd)
            if stat.S_ISREG(metadata.st_mode):
                flushed[(metadata.st_dev, metadata.st_ino)] += 1
            else:
                namespaces.add((metadata.st_dev, metadata.st_ino))
        original_flush(fd)

    def barrier(self, digests):
        nonlocal in_barrier
        covered.update(digests)
        in_barrier = True
        try:
            original_barrier(self, digests)
        finally:
            in_barrier = False

    def main(self, oid, *, expected_oid):
        # Every new citation is durable while main still names the parent.
        from cruxible_core.proposals.settlement import parse_change_set_record

        tree = self.read_tree(oid)
        path = max(path for path in tree if path.startswith("changesets/"))
        record = parse_change_set_record(tree[path], path=path)
        expected = set(cited_bodies(record, tree, store))
        assert expected and expected <= covered
        assert expected <= store._durable
        for digest in expected:
            metadata = store._path(digest).stat()
            assert flushed[(metadata.st_dev, metadata.st_ino)] == 1
            parent = store._path(digest).parent.stat()
            assert (parent.st_dev, parent.st_ino) in namespaces
        assert self.read_main() == expected_oid
        return original_main(self, oid, expected_oid=expected_oid)

    monkeypatch.setattr(cas, "_sync_filesystem", lambda fd: False)
    monkeypatch.setattr(os, "fsync", flush)
    monkeypatch.setattr(ContentAddressedBodyStore, "flush_cited", barrier)
    monkeypatch.setattr(GitLedger, "compare_and_set_main", main)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert covered
    store.flush_cited(tuple(covered))
    assert max(flushed.values()) == 1
