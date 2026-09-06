"""Exact remote snapshots survive local churn and refuse competing writers."""

from __future__ import annotations

import subprocess

import pytest

from cruxible_core.playbill import git as git_module
from cruxible_core.playbill.git import NOTE_REFS, GitLedger

MAIN = "refs/heads/main"
NOTE = NOTE_REFS["approval"]
PROPOSAL = "refs/heads/proposals/" + "a" * 64
SETTLED = "refs/settled/" + "a" * 64


@pytest.fixture(params=("sha1", "sha256"))
def repos(tmp_path, request):
    def create(name):
        return GitLedger.initialize(
            tmp_path / name,
            object_format=request.param,
            signing_key_path=tmp_path / "unused-key",
            allowed_signers_path=tmp_path / "unused-signers",
        )

    return create("local.git"), create("remote.git")


def commit(ledger, message, parent=None):
    tree = ledger._git(["mktree"], input_bytes=b"").decode().strip()
    args = ["-c", "commit.gpgsign=false", "commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return ledger._git(args).decode().strip()


def refs(ledger, **values):
    for ref, oid in values.items():
        ledger._git(["update-ref", ref, oid])


def pins(ledger):
    return ledger._git(["for-each-ref", "--format=%(refname)", "refs/playbill-mirror-pins/"])


def test_snapshot_is_exact_despite_live_ref_changes(repos, monkeypatch):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first, PROPOSAL: first})
    captured = local.mirror_refs()
    later = commit(local, "later", first)
    refs(local, **{MAIN: later, NOTE: later})
    original = git_module._command

    def inspect(args, **kwargs):
        if "ls-remote" in args or "push" in args:
            assert pins(local)
            assert kwargs["timeout"] == git_module.MIRROR_PUSH_TIMEOUT_SECONDS
        return original(args, **kwargs)

    monkeypatch.setattr(git_module, "_command", inspect)
    assert local.push_mirror(str(remote.path), snapshot=captured) is None
    assert remote.mirror_refs() == captured
    assert local.read_main() == later
    assert not pins(local)


def test_leased_replacement_and_only_known_deletion(repos):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first, PROPOSAL: first})
    before = local.mirror_refs()
    assert local.push_mirror(str(remote.path)) is None
    unrelated = "refs/heads/reviewer-work"
    refs(remote, **{unrelated: first})
    # Notes may be restated onto an unrelated commit; the lease authorizes it.
    restated = commit(local, "restated")
    refs(local, **{NOTE: restated, SETTLED: first})
    local._git(["update-ref", "-d", PROPOSAL])
    desired = local.mirror_refs()
    assert local.push_mirror(str(remote.path), snapshot=desired, expected_remote=before) is None
    assert remote.mirror_refs() == desired
    assert remote._git(["rev-parse", unrelated]).decode().strip() == first
    assert not pins(local)


def test_uncertain_older_attempt_and_empty_remote_repair(repos):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first, PROPOSAL: first})
    attempted = local.mirror_refs()
    assert local.push_mirror(str(remote.path)) is None
    later = commit(local, "later", first)
    refs(local, **{MAIN: later, NOTE: later, SETTLED: first})
    local._git(["update-ref", "-d", PROPOSAL])
    desired = local.mirror_refs()
    assert local.push_mirror(str(remote.path), snapshot=desired, previous_attempt=attempted) is None
    assert remote.mirror_refs() == desired
    for ref in desired:
        remote._git(["update-ref", "-d", ref])
    assert local.push_mirror(str(remote.path), expected_remote=desired) is None
    assert remote.mirror_refs() == desired


def test_state_loss_proves_forward_notes_and_exact_settlement(repos):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first, PROPOSAL: first})
    assert local.push_mirror(str(remote.path)) is None
    later = commit(local, "later", first)
    refs(local, **{MAIN: later, NOTE: later, SETTLED: first})
    local._git(["update-ref", "-d", PROPOSAL])
    assert local.push_mirror(str(remote.path)) is None
    assert remote.mirror_refs() == local.mirror_refs()


def test_first_publish_refuses_unknown_nonempty_remote_ref(repos):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first})
    assert local.push_mirror(str(remote.path)) is None
    other = commit(remote, "other")
    refs(remote, **{PROPOSAL: other})
    assert "diverged" in local.push_mirror(str(remote.path))
    assert remote.mirror_refs()[PROPOSAL] == other
    assert not pins(local)


def test_remote_race_rejects_entire_atomic_update(repos, monkeypatch):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first, NOTE: first})
    before = local.mirror_refs()
    assert local.push_mirror(str(remote.path)) is None
    later = commit(local, "later", first)
    competitor = commit(remote, "competitor", first)
    refs(local, **{MAIN: later, NOTE: later})
    original = git_module._command

    def race(args, **kwargs):
        if "push" in args:
            refs(remote, **{NOTE: competitor})
        return original(args, **kwargs)

    monkeypatch.setattr(git_module, "_command", race)
    assert local.push_mirror(str(remote.path), expected_remote=before) is not None
    assert remote.read_main() == first
    assert remote.mirror_refs()[NOTE] == competitor
    assert not pins(local)


def test_main_cannot_be_rolled_back_even_with_matching_expected_state(repos):
    local, remote = repos
    first = commit(local, "first")
    later = commit(local, "later", first)
    refs(local, **{MAIN: later})
    assert local.push_mirror(str(remote.path)) is None
    assert "not an ancestor" in local.push_mirror(
        str(remote.path), snapshot={MAIN: first}, expected_remote={MAIN: later}
    )
    assert remote.read_main() == later


@pytest.mark.parametrize(
    "stage,failure",
    [
        ("ls-remote", "timeout"),
        ("ls-remote", "error"),
        ("ls-remote", "malformed"),
        ("push", "timeout"),
        ("push", "error"),
    ],
)
def test_transport_failure_cleans_pins_without_remote_mutation(repos, monkeypatch, stage, failure):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first})
    original = git_module._command

    def fail(args, **kwargs):
        if stage in args:
            assert pins(local)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 0.01)
            return subprocess.CompletedProcess(
                args, 1 if failure == "error" else 0, stdout=b"invalid", stderr=b"failed"
            )
        return original(args, **kwargs)

    monkeypatch.setattr(git_module, "_command", fail)
    assert local.push_mirror(str(remote.path)) is not None
    assert not pins(local)
    assert not remote._ref_exists(MAIN)


def test_limits_and_unowned_ref_refuse_before_transport(repos, monkeypatch):
    local, remote = repos
    first = commit(local, "first")
    refs(local, **{MAIN: first})
    assert "unowned" in local.push_mirror(
        str(remote.path), snapshot={MAIN: first, "refs/heads/no": first}
    )
    monkeypatch.setattr(git_module, "_MIRROR_ARG_BYTES", 1)
    assert "argument limit" in local.push_mirror(str(remote.path))
    assert not pins(local)
    assert not remote._ref_exists(MAIN)


def test_lost_uncertain_attempt_recovers_from_ancestry_and_exact_settlement(repos, monkeypatch):
    local, remote = repos
    published_oid = commit(local, "published P")
    refs(local, **{MAIN: published_oid, NOTE: published_oid})
    published = local.mirror_refs()
    assert local.push_mirror(str(remote.path)) is None

    attempted_oid = commit(local, "uncertain A", published_oid)
    refs(local, **{MAIN: attempted_oid, NOTE: attempted_oid, PROPOSAL: attempted_oid})
    attempted = local.mirror_refs()
    original = git_module._command

    def acknowledge_then_timeout(args, **kwargs):
        result = original(args, **kwargs)
        if "push" in args:
            assert result.returncode == 0
            raise subprocess.TimeoutExpired(args, 0.01)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(git_module, "_command", acknowledge_then_timeout)
        assert "unconfirmed" in local.push_mirror(str(remote.path), expected_remote=published)
    assert remote.mirror_refs() == attempted
    assert not pins(local)

    desired_oid = commit(local, "recorded B", attempted_oid)
    refs(local, **{MAIN: desired_oid, NOTE: desired_oid, SETTLED: attempted_oid})
    local._git(["update-ref", "-d", PROPOSAL])
    desired = local.mirror_refs()
    # B replaced attempted_refs on disk, then the daemon died before its push.
    # Restart knows P and B, while the actual remote still carries forgotten A.
    assert (
        local.push_mirror(
            str(remote.path), snapshot=desired, expected_remote=published, previous_attempt=desired
        )
        is None
    )
    assert remote.mirror_refs() == desired
    assert not pins(local)
