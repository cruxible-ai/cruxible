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
