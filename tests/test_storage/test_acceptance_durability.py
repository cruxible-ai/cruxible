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
    monkeypatch.setattr(
        cas, "_sync_filesystem", lambda fd: calls.append(os.fstat(fd).st_ino) or True
    )
    monkeypatch.setattr(os, "fsync", lambda fd: pytest.fail("per-file Linux fsync"))
    store.flush_cited(digests)
    assert calls == [tmp_path.parent.stat().st_ino]
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
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.ledger.git import GitLedger
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.test_procedures.test_procedure_settlement_delivery import run_settle, settle_world

    instance, root, line = settle_world(tmp_path)
    store = instance.body_store()
    assert instance.activation_publisher().bodies is store
    already_durable = set(store._durable)
    flushed = Counter()
    namespaces = set()
    observations = []
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

    def barrier(self, digests, **kwargs):
        nonlocal in_barrier
        covered.update(digests)
        in_barrier = True
        try:
            original_barrier(self, digests, **kwargs)
        finally:
            in_barrier = False

    def main(self, oid, *, expected_oid):
        # Every new citation is durable while main still names the parent.
        from cruxible_core.proposals.settlement import parse_change_set_record

        tree = self.read_tree(oid)
        path = max(path for path in tree if path.startswith("changesets/"))
        record = parse_change_set_record(tree[path], path=path)
        expected = set()
        for member in record.members:
            if member.path.startswith("claims/"):
                for digest in parse_claim(
                    tree[member.path], path=member.path
                ).backing.capture_digests:
                    expected.add(digest)
                    envelope = parse_capture_envelope(
                        store.read(
                            digest,
                            access=BodyAccessContext(principal_id="test", can_read_body=True),
                        )
                    )
                    if envelope.commitment.materialization == "cas":
                        expected.add(envelope.commitment.digest)
                    content_digest = getattr(envelope.source, "content_digest", None)
                    if content_digest is not None:
                        expected.add(content_digest)
                    if envelope.input_receipt_set_manifest_digest is not None:
                        expected.add(envelope.input_receipt_set_manifest_digest)
        observations.append(
            (
                expected,
                set(covered),
                set(store._durable),
                {
                    digest: flushed[
                        (store._path(digest).stat().st_dev, store._path(digest).stat().st_ino)
                    ]
                    for digest in expected
                },
                {
                    digest: (
                        store._path(digest).parent.stat().st_dev,
                        store._path(digest).parent.stat().st_ino,
                    )
                    in namespaces
                    for digest in expected
                },
                self.read_main() == expected_oid,
            )
        )
        return original_main(self, oid, expected_oid=expected_oid)

    monkeypatch.setattr(cas, "_sync_filesystem", lambda fd: False)
    monkeypatch.setattr(os, "fsync", flush)
    monkeypatch.setattr(ContentAddressedBodyStore, "flush_cited", barrier)
    monkeypatch.setattr(GitLedger, "compare_and_set_main", main)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert observations
    for expected, covered_at_main, durable_at_main, counts, parents, parent_held in observations:
        assert expected and expected <= covered_at_main
        assert expected <= durable_at_main
        assert counts == {digest: 0 if digest in already_durable else 1 for digest in expected}
        assert all(parents[digest] for digest in expected if digest not in already_durable)
        assert parent_held
    store.flush_cited(tuple(covered))
    assert max(flushed.values()) == 1


def test_failed_acceptance_barrier_never_moves_main(tmp_path, monkeypatch):
    from cruxible_core.ledger import activation
    from tests.test_procedures.test_procedure_settlement_delivery import run_settle, settle_world

    instance, root, line = settle_world(tmp_path)
    before = instance.accepted_coordinate()
    attempted = []

    def fail(*args):
        attempted.append(True)
        raise OSError("acceptance flush failed")

    monkeypatch.setattr(activation, "flush_acceptance", fail)
    try:
        run_settle(instance, root, line)
    except OSError:
        pass
    assert attempted
    assert instance.accepted_coordinate() == before
