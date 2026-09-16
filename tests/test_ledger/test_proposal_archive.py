"""A single leased archive retains bytes, without per-proposal publication growth."""

import subprocess

import pytest

from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_core.ledger.git import PROPOSAL_ARCHIVE_REF as ARCHIVE
from tests.test_ledger import test_git_mirror_snapshots as mirror_fixtures
from tests.test_ledger.test_git_mirror_snapshots import MAIN, PROPOSAL, commit, refs

repos = mirror_fixtures.repos


def close(local, oid, key="a" * 64):
    local.retain_proposal_review("sha256:" + key, oid)
    local.replace_proposal_review_refs({})
    return local.mirror_refs()[ARCHIVE]


def test_archive_appends_atomically_and_reconciliation_is_idempotent(repos):
    local, _ = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    first = commit(local, "one", base)
    first_tip = close(local, first)
    assert local.parent_of(first_tip) == first
    second = commit(local, "two", base)
    second_tip = close(local, second, "b" * 64)
    parents = local._git(["rev-list", "--parents", "-n", "1", second_tip]).decode().split()
    assert parents == [second_tip, first_tip, second]
    assert local.paths_at(second_tip) == ()
    local.archive_proposal_commits((first, second))
    local.replace_proposal_review_refs({})
    assert local.mirror_refs() == {MAIN: base, ARCHIVE: second_tip}


@pytest.mark.parametrize("already_archived", [False, True])
def test_archive_cas_failure_does_not_delete_review_branch(repos, monkeypatch, already_archived):
    local, _ = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    close(local, commit(local, "earlier", base))
    candidate = commit(local, "current", base)
    if already_archived:
        local.archive_proposal_commits((candidate,))
    local.retain_proposal_review("sha256:" + "a" * 64, candidate)
    competitor = commit(local, "competing archive")
    original = local._git

    def race(args, **kwargs):
        if args == ["update-ref", "--stdin"]:
            original(["update-ref", ARCHIVE, competitor])
        return original(args, **kwargs)

    monkeypatch.setattr(local, "_git", race)
    with pytest.raises(PlaybillGitError):
        local.replace_proposal_review_refs({})
    assert local.mirror_refs()[PROPOSAL] == candidate
    assert local.mirror_refs()[ARCHIVE] == competitor


def test_legacy_local_pins_are_folded_and_removed(repos):
    local, _ = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    candidates = [commit(local, name, base) for name in ("one", "two")]
    for key, oid in zip(("a", "b"), candidates):
        refs(local, **{"refs/settled/" + key * 64: oid})
    local.replace_proposal_review_refs({})
    inventory = (
        local._git(["for-each-ref", "--format=%(refname)", "refs/settled/"]).decode().splitlines()
    )
    assert inventory == [ARCHIVE]
    assert all(local.is_ancestor(oid, local.mirror_refs()[ARCHIVE]) for oid in candidates)


def test_archive_is_mirrored_and_retained_after_independent_clone_gc(repos, tmp_path):
    local, remote = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    candidate = commit(local, "closed", base)
    tip = close(local, candidate)
    assert local.push_mirror(str(remote.path)) is None
    assert remote.mirror_refs()[ARCHIVE] == tip
    clone = tmp_path / "reviewer"
    subprocess.run(["git", "clone", "--no-local", "-q", str(remote.path), str(clone)], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "fetch", "-q", "origin", f"{ARCHIVE}:{ARCHIVE}"], check=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "reflog", "expire", "--expire=now", "--all"], check=True
    )
    subprocess.run(["git", "-C", str(clone), "gc", "--prune=now"], check=True)
    subprocess.run(["git", "-C", str(clone), "cat-file", "-e", candidate], check=True)
    assert (
        subprocess.run(
            ["git", "-C", str(clone), "merge-base", "--is-ancestor", candidate, ARCHIVE]
        ).returncode
        == 0
    )


def test_archive_membership_does_not_authorize_unknown_remote_branch(repos):
    local, remote = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    close(local, commit(local, "candidate", base))
    assert local.push_mirror(str(remote.path)) is None
    refs(remote, **{PROPOSAL: base})
    assert "diverged" in local.push_mirror(str(remote.path), retired_proposal=lambda *_: False)
    assert remote.mirror_refs()[PROPOSAL] == base


def test_remote_legacy_archive_has_actionable_refusal(repos):
    local, remote = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    assert local.push_mirror(str(remote.path)) is None
    old_ref = "refs/settled/" + "a" * 64
    refs(remote, **{old_ref: base})
    message = local.push_mirror(str(remote.path))
    assert message and "per-proposal archive refs" in message and "new mirror URL" in message
    assert "retired per-proposal archive refs" in local.push_mirror(
        str(remote.path), expected_remote={MAIN: base, old_ref: base}
    )


def test_archive_cannot_be_deleted_or_rewound_even_with_matching_lease(repos):
    local, remote = repos
    base = commit(local, "base")
    refs(local, **{MAIN: base})
    first = close(local, commit(local, "one", base))
    second = close(local, commit(local, "two", base), "b" * 64)
    assert local.push_mirror(str(remote.path)) is None
    expected = local.mirror_refs()
    local._git(["update-ref", "-d", ARCHIVE])
    assert "missing" in local.push_mirror(str(remote.path), expected_remote=expected)
    assert local.push_mirror(str(remote.path)) is not None
    refs(local, **{ARCHIVE: first})
    assert "rewound" in local.push_mirror(str(remote.path), expected_remote=expected)
    assert remote.mirror_refs()[ARCHIVE] == second
