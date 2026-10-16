"""Cross-verify SSHSIG signatures with Git and exercise files-backend leases."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cruxible_client.contracts.errors import GitError
from cruxible_core.governance.keys import generate_daemon_key
from cruxible_core.ledger.git import GitLedger
from tests.test_scale._counters import counting


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path, request):
    key = generate_daemon_key(tmp_path / "credentials")
    signers = tmp_path / "allowed"
    signers.write_bytes(b"daemon " + key.public_key_path.read_bytes())
    return GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=key.private_key_path,
        allowed_signers_path=signers,
    )


def test_git_and_our_signatures_verify_both_directions(ledger):
    ours = ledger.create_signed_genesis({"one": b"one"}, timestamp="2026-10-15T12:00:00.000000Z")
    ledger._git(
        ["-c", f"gpg.ssh.allowedSignersFile={ledger._allowed_signers_path}", "verify-commit", ours]
    )
    theirs = (
        ledger._git(["commit-tree", "-S", ledger.tree_oid(ours), "-m", "Git signed"])
        .decode()
        .strip()
    )
    assert ledger.verify_commit(theirs)
    from cryptography.hazmat.primitives import serialization

    from cruxible_core.ledger.git_signing import verify_commit

    key = serialization.load_ssh_public_key(
        ledger._allowed_signers_path.read_bytes().split(b" ", 1)[1]
    )
    body = ledger._git(["cat-file", "commit", theirs])
    assert not verify_commit(
        body.replace(b"Git signed", b"Bad signed"), key, sha256=ledger.object_format() == "sha256"
    )


def test_all_write_plumbing_runs_without_spawning_git(ledger):
    with counting(ledger.path) as work:
        base = ledger.create_signed_genesis({"one": b"one"}, timestamp="2026-10-15T12:00:00Z")
        ledger.set_main_genesis(base)
        proposal, tree = ledger.create_proposal_commit(
            {"one": b"two"},
            base_oid=base,
            target_ref="refs/proposals/owner/test",
            actor_id="owner",
            timestamp="2026-10-15T12:01:00Z",
            expected_ref_oid=None,
            message="Change one",
        )
        ledger.retain_proposal_review("sha256:" + "a" * 64, proposal)
        ledger.write_proposal_note("evaluation", proposal, b"evaluation\n")
        generation = ledger.create_signed_generation(
            {"one": b"two", "changesets/one": b"record"},
            parent_oid=base,
            sequence=1,
            timestamp="2026-10-15T12:01:00Z",
            message="Accept one",
            extends_tree=tree,
        )
        assert ledger.compare_and_set_main(generation, expected_oid=base)
        ledger.write_generation_note(generation, b"descriptor\n")
        ledger.replace_proposal_review_refs({})
    assert work.counts["git_spawns"] == 0
    assert (
        ledger._git(["notes", "--ref=refs/notes/playbill-eval", "show", proposal])
        == b"evaluation\n"
    )
    ledger._git(["fsck", "--strict", "--no-dangling"])


def test_packed_ref_cas_and_delete_match_git(ledger):
    base = ledger.create_signed_genesis({}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    ledger._git(["pack-refs", "--all"])
    ledger._update_refs([f"update refs/heads/other {base} {'0' * len(base)}"])
    ledger._git(["pack-refs", "--all"])
    with pytest.raises(GitError, match="compare-and-set"):
        ledger._update_refs([f"delete refs/heads/other {'1' * len(base)}"])
    assert ledger._resolve_ref("refs/heads/other") == base
    ledger._update_refs([f"delete refs/heads/other {base}"])
    assert ledger._resolve_ref("refs/heads/other") is None
    assert ledger._git(["for-each-ref", "--format=%(refname)"]).splitlines() == [b"refs/heads/main"]


def test_only_one_thread_can_win_a_ref_lease(ledger):
    base = ledger.create_signed_genesis({}, timestamp="2026-10-15T12:00:00Z")
    ref = "refs/heads/race"

    def compete(_):
        try:
            ledger._update_refs([f"update {ref} {base} {'0' * len(base)}"])
            return True
        except GitError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(compete, range(2))) == [False, True]
    assert not list((ledger.path / "refs").rglob("*.lock"))


def test_prepared_generation_survives_external_repack_until_ref_publication(ledger):
    base = ledger.create_signed_genesis({"one": b"base"}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    assert not list((ledger.path / "objects/pack").glob("*.keep"))
    generation = ledger.create_signed_generation(
        {"one": b"changed"},
        parent_oid=base,
        sequence=1,
        timestamp="2026-10-15T12:01:00Z",
        message="Accept one",
    )
    assert list((ledger.path / "objects/pack").glob("*.keep"))
    ledger._git(["repack", "-a", "-d"])
    assert ledger.object_exists(generation)
    assert ledger.compare_and_set_main(generation, expected_oid=base)
    assert not list((ledger.path / "objects/pack").glob("*.keep"))
    assert ledger.read_tree(generation) == {"one": b"changed"}


def test_request_stages_one_pair_and_flushes_only_at_acceptance(ledger, monkeypatch):
    import os

    base = ledger.create_signed_genesis({"one": b"one"}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    directory = ledger.path / "objects/pack"
    before = set(directory.glob("*.pack"))
    synced = []
    real_fsync = os.fsync

    def observe(fd):
        st = os.fstat(fd)
        synced.append((st.st_dev, st.st_ino))
        return real_fsync(fd)

    def sync_count(path):
        st = path.stat()
        return synced.count((st.st_dev, st.st_ino))

    monkeypatch.setattr(os, "fsync", observe)
    with ledger.request_pack():
        proposal, tree = ledger.create_proposal_commit(
            {"one": b"two"},
            base_oid=base,
            target_ref="refs/proposals/owner/test",
            actor_id="owner",
            timestamp="2026-10-15T12:01:00Z",
            expected_ref_oid=None,
            message="Change one",
        )
        ledger.retain_proposal_review("sha256:" + "a" * 64, proposal)
        ledger.write_proposal_note("evaluation", proposal, b"evaluation\n")
        assert not synced
        assert len(set(directory.glob("*.pack")) - before) == 1
        # Independent Git sees the staged proposal and note before main moves.
        assert ledger._git(["cat-file", "-t", proposal]) == b"commit\n"
        assert ledger.read_main() == base
        generation = ledger.create_signed_generation(
            {"one": b"two", "changesets/one": b"record"},
            parent_oid=base,
            sequence=1,
            timestamp="2026-10-15T12:01:00Z",
            message="Accept one",
            extends_tree=tree,
        )
        with ledger.activation_lock():
            assert ledger.compare_and_set_main(generation, expected_oid=base)
            ledger.write_generation_note(generation, b"descriptor\n")
        # The accepted pack is durable before a successor can acquire the lock.
        (accepted_pack,) = set(directory.glob("*.pack")) - before
        assert sync_count(accepted_pack) == 1
        assert sync_count(accepted_pack.with_suffix(".idx")) == 1
        ledger.replace_proposal_review_refs({})
    assert len(set(directory.glob("*.pack")) - before) == 1
    assert sync_count(accepted_pack) == 1
    assert sync_count(accepted_pack.with_suffix(".idx")) == 1
    assert not list(directory.glob("*.keep"))
    ledger._git(["fsck", "--strict", "--no-dangling"])


def test_acceptance_carries_an_earlier_best_effort_proposal_pack(ledger):
    from cruxible_core.ledger.git_objects import PackStore

    base = ledger.create_signed_genesis({"one": b"base"}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    with ledger.request_pack():
        proposal, tree = ledger.create_proposal_commit(
            {"one": b"earlier proposal"},
            base_oid=base,
            target_ref="refs/proposals/owner/later",
            actor_id="owner",
            timestamp="2026-10-15T12:01:00Z",
            expected_ref_oid=None,
            message="Change one later",
        )
        ledger.retain_proposal_review("sha256:" + "b" * 64, proposal)
    provisional = next((ledger.path / "objects/pack").glob("*.pending")).with_suffix(".pack")
    with ledger.request_pack(), ledger.activation_lock():
        generation = ledger.create_signed_generation(
            {"one": b"earlier proposal", "changesets/one": b"record"},
            parent_oid=base,
            sequence=1,
            timestamp="2026-10-15T12:02:00Z",
            message="Accept earlier proposal",
            extends_tree=tree,
        )
        assert ledger.compare_and_set_main(generation, expected_oid=base)
        ledger.write_generation_note(generation, b"descriptor\n")
    # Losing the old advisory pair cannot lose any accepted dependency.
    for suffix in (".pack", ".idx", ".pending"):
        provisional.with_suffix(suffix).unlink()
    fresh = PackStore(ledger.path)
    assert fresh.read(ledger._blob_oid(b"earlier proposal")) == ("blob", b"earlier proposal")
    fresh.close()
    ledger._git(["fsck", "--strict", "--no-dangling"])


def test_request_exit_does_not_resurrect_a_collected_losing_generation(ledger):
    base = ledger.create_signed_genesis({"one": b"base"}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    with ledger.request_pack():
        loser = ledger.create_signed_generation(
            {"one": b"loser"},
            parent_oid=base,
            sequence=1,
            timestamp="2026-10-15T12:01:00Z",
            message="Losing generation",
        )
        # A failed CAS stages its objects before discovering that the lease lost.
        from cruxible_core.ledger.git import _batch_reader

        _batch_reader(ledger.path).flush()
        assert loser in ledger.collect_unreachable_generation(loser)
        ledger.settle_generation_in_flight(loser)
        assert not ledger.object_exists(loser)
    assert not ledger.object_exists(loser)
    assert not list((ledger.path / "objects/pack").glob("*.keep"))
