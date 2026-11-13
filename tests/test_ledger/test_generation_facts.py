"""Accepting admission facts are signed, immutable, portable generation metadata."""

from pathlib import Path

from cruxible_core.ledger.generation_facts import AcceptingAdmission
from cruxible_core.ledger.git import _batch_reader
from cruxible_core.ledger.git_signing import load_private, verify_commit
from tests.core_support._support import initialize_local


def test_admission_identity_changes_oid_and_is_covered_by_signature(tmp_path: Path) -> None:
    instance, _ = initialize_local(tmp_path)
    ledger = instance._ledger
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    first = AcceptingAdmission(
        proposal_id="sha256:" + "11" * 32, admission_digest="sha256:" + "22" * 32
    )
    second = AcceptingAdmission(
        proposal_id="sha256:" + "33" * 32, admission_digest="sha256:" + "44" * 32
    )
    oids = [
        ledger.create_signed_generation(
            tree,
            parent_oid=base.git_oid,
            sequence=1,
            timestamp="2026-10-16T12:00:00.000000Z",
            message="Accept generation 1\n",
            accepting_admission=identity,
        )
        for identity in (None, first, second)
    ]
    assert len(set(oids)) == 3
    for oid, expected in zip(oids, (None, first, second), strict=True):
        assert ledger.verify_commit(oid)
        assert ledger.accepting_admission(oid) == expected
    # The same tree and prose with another admission has a different accepted
    # coordinate. Changing the header without signing invalidates the signature.
    found = _batch_reader(ledger.path).objects((oids[1],))[oids[1]]
    assert found is not None
    raw = found[1]
    public = load_private(ledger._signing_key_path.read_bytes()).public_key()
    sha256 = ledger.object_format() == "sha256"
    assert verify_commit(raw, public, sha256=sha256)
    tampered = raw.replace(first.header_value().encode(), second.header_value().encode())
    assert not verify_commit(tampered, public, sha256=sha256)
    assert raw.split(b"\n\n", 1)[1] == b"Accept generation 1\n"
