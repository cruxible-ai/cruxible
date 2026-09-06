"""Advisory workspace ref advertisement and Git object-format inheritance."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cruxible_core.playbill import workspace_advertisement as advertisement_module
from cruxible_core.playbill.workspace_advertisement import (
    advertise_workspace_refs,
    close_proposal_review_worktree,
    open_proposal_review_worktree,
    workspace_git_object_format,
)

PROPOSAL_KEY = "a" * 64


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repositories(tmp_path: Path, object_format: str) -> tuple[Path, Path]:
    workspace = tmp_path / f"workspace-{object_format}"
    ledger = tmp_path / f"ledger-{object_format}.git"
    subprocess.run(
        ["git", "init", "-b", "main", f"--object-format={object_format}", str(workspace)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--bare", f"--object-format={object_format}", str(ledger)],
        check=True,
        capture_output=True,
    )
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.invalid")
    (workspace / "README.md").write_text("workspace\n")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "workspace")
    _git(workspace, "push", str(ledger), "HEAD:refs/heads/main")
    head = _git(workspace, "rev-parse", "HEAD")
    _git(ledger, "update-ref", f"refs/heads/proposals/{PROPOSAL_KEY}", head)
    return workspace, ledger


def _repositories_with_alternate(tmp_path: Path) -> tuple[Path, Path]:
    seed = tmp_path / "alternate-seed"
    ledger = tmp_path / "alternate-ledger.git"
    workspace = tmp_path / "alternate-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(seed)],
        check=True,
        capture_output=True,
    )
    _git(seed, "config", "user.name", "test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(ledger)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", "--reference", str(seed), str(ledger), str(workspace)],
        check=True,
        capture_output=True,
    )
    head = _git(workspace, "rev-parse", "HEAD")
    _git(ledger, "update-ref", f"refs/heads/proposals/{PROPOSAL_KEY}", head)
    return workspace, ledger


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_advertisement_fetches_only_remote_tracking_refs(
    tmp_path: Path,
    object_format: str,
) -> None:
    workspace, ledger = _repositories(tmp_path, object_format)
    before_status = _git(workspace, "status", "--porcelain=v1")

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format=object_format,  # type: ignore[arg-type]
    )

    assert workspace_git_object_format(workspace) == object_format
    assert result.status == "updated"
    assert result.advertised_refs == (
        "refs/remotes/playbill/accepted",
        f"refs/remotes/playbill/proposals/{PROPOSAL_KEY}",
    )
    assert _git(workspace, "status", "--porcelain=v1") == before_status
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(workspace, "config", "--get-all", "remote.playbill.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/playbill/accepted",
        "+refs/heads/proposals/*:refs/remotes/playbill/proposals/*",
    ]
    assert _git(workspace, "config", "--get", "remote.playbill.tagOpt") == "--no-tags"
    assert _git(workspace, "config", "--get", "remote.playbill.skipFetchAll") == "true"


def test_review_worktree_is_detached_ignored_and_never_creates_a_branch(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    advertised = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )
    assert advertised.status == "updated"
    branches_before = _git(workspace, "for-each-ref", "--format=%(refname)", "refs/heads")

    opened = open_proposal_review_worktree(
        workspace_path=workspace,
        proposal_id=f"sha256:{PROPOSAL_KEY}",
    )

    assert opened == workspace / ".playbill" / "review" / PROPOSAL_KEY
    assert _git(opened, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(workspace, "for-each-ref", "--format=%(refname)", "refs/heads") == (branches_before)
    assert _git(workspace, "status", "--porcelain=v1") == ""
    exclude = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert b"/.playbill/review/\n" in (exclude / "info" / "exclude").read_bytes()

    closed = close_proposal_review_worktree(
        workspace_path=workspace,
        proposal_id=PROPOSAL_KEY,
    )

    assert closed == opened
    assert not opened.exists()
    assert _git(workspace, "for-each-ref", "--format=%(refname)", "refs/heads") == (branches_before)


def test_review_open_refuses_a_symlinked_playbill_directory(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    assert (
        advertise_workspace_refs(
            workspace_root=workspace,
            ledger_path=ledger,
            ledger_object_format="sha1",
        ).status
        == "updated"
    )
    outside = tmp_path / "outside"
    (outside / "review").mkdir(parents=True)
    (workspace / ".playbill").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes the Git worktree") as caught:
        open_proposal_review_worktree(
            workspace_path=workspace,
            proposal_id=PROPOSAL_KEY,
        )

    assert str(workspace / ".playbill") in str(caught.value)
    assert not (outside / "review" / PROPOSAL_KEY).exists()


def test_review_close_refuses_to_delete_through_a_symlinked_playbill_directory(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    assert (
        advertise_workspace_refs(
            workspace_root=workspace,
            ledger_path=ledger,
            ledger_object_format="sha1",
        ).status
        == "updated"
    )
    outside = tmp_path / "outside"
    review_root = outside / "review"
    review_root.mkdir(parents=True)
    target = review_root / PROPOSAL_KEY
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "worktree",
            "add",
            "--detach",
            str(target),
            f"refs/remotes/playbill/proposals/{PROPOSAL_KEY}",
        ],
        check=True,
        capture_output=True,
    )
    (workspace / ".playbill").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes the Git worktree") as caught:
        close_proposal_review_worktree(
            workspace_path=workspace,
            proposal_id=PROPOSAL_KEY,
        )

    assert str(workspace / ".playbill") in str(caught.value)
    assert target.is_dir()


def test_review_close_prunes_a_missing_registered_worktree(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    assert (
        advertise_workspace_refs(
            workspace_root=workspace,
            ledger_path=ledger,
            ledger_object_format="sha1",
        ).status
        == "updated"
    )
    opened = open_proposal_review_worktree(
        workspace_path=workspace,
        proposal_id=PROPOSAL_KEY,
    )
    unrelated = tmp_path / "unrelated-worktree"
    _git(workspace, "worktree", "add", "--detach", str(unrelated), "HEAD")
    moved_unrelated = tmp_path / "moved-unrelated-worktree"
    shutil.move(unrelated, moved_unrelated)
    shutil.rmtree(opened)

    closed = close_proposal_review_worktree(
        workspace_path=workspace,
        proposal_id=PROPOSAL_KEY,
    )

    assert closed == opened
    worktrees = _git(workspace, "worktree", "list", "--porcelain")
    assert str(opened) not in worktrees
    assert str(unrelated) in worktrees


def test_review_close_refuses_a_review_that_was_never_opened(tmp_path: Path) -> None:
    workspace, _ledger = _repositories(tmp_path, "sha1")
    (workspace / ".playbill" / "review").mkdir(parents=True)

    with pytest.raises(ValueError, match="review workspace was never opened"):
        close_proposal_review_worktree(
            workspace_path=workspace,
            proposal_id="b" * 64,
        )


def test_advertisement_does_not_fetch_ledger_tags(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    head = _git(workspace, "rev-parse", "HEAD")
    _git(ledger, "update-ref", "refs/tags/daemon-only", head)

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"
    assert _git(workspace, "tag", "--list", "daemon-only") == ""


def test_advertisement_resolves_relative_remote_url_from_workspace(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    relative = str(ledger.relative_to(workspace.parent))
    _git(workspace, "config", "--add", "remote.playbill.url", f"../{relative}")

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"


def test_advertisement_ignores_executable_workspace_git_config(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    fsmonitor_marker = tmp_path / "fsmonitor-executed"
    uploadpack_marker = tmp_path / "uploadpack-executed"
    _git(workspace, "config", "core.fsmonitor", f"touch {fsmonitor_marker}")
    _git(
        workspace,
        "config",
        "remote.playbill.uploadpack",
        f"touch {uploadpack_marker}; git-upload-pack",
    )
    fsmonitor_marker.unlink(missing_ok=True)
    uploadpack_marker.unlink(missing_ok=True)

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"
    assert not fsmonitor_marker.exists()
    assert not uploadpack_marker.exists()


def test_advertisement_refuses_instead_of_ssh_command_without_execution(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    daemon_uid_marker = tmp_path / "daemon-uid"
    _git(
        workspace,
        "config",
        "url.ssh://attacker.invalid/x.insteadOf",
        str(ledger.resolve()),
    )
    _git(
        workspace,
        "config",
        "core.sshCommand",
        f"/bin/sh -c 'id > {daemon_uid_marker}'",
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "remote_conflict"
    assert not daemon_uid_marker.exists()


def test_protocol_gate_blocks_execution_if_effective_url_check_is_bypassed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    daemon_uid_marker = tmp_path / "daemon-uid"
    _git(
        workspace,
        "config",
        "url.ssh://attacker.invalid/x.insteadOf",
        str(ledger.resolve()),
    )
    _git(
        workspace,
        "config",
        "core.sshCommand",
        f"/bin/sh -c 'id > {daemon_uid_marker}'",
    )
    monkeypatch.setattr(
        advertisement_module,
        "_effective_remote_urls",
        lambda _workspace: subprocess.CompletedProcess(
            (),
            0,
            f"{ledger.resolve()}\n".encode(),
            b"",
        ),
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "fetch_failed"
    assert not daemon_uid_marker.exists()


def test_advertisement_ignores_alternate_refs_command_with_real_alternates(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories_with_alternate(tmp_path)
    daemon_uid_marker = tmp_path / "alternate-refs-daemon-uid"
    _git(
        workspace,
        "config",
        "core.alternateRefsCommand",
        f"/bin/sh -c 'id > {daemon_uid_marker}'",
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert (workspace / ".git/objects/info/alternates").is_file()
    assert result.status == "updated"
    assert not daemon_uid_marker.exists()


def test_git_environment_drops_ambient_execution_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    ambient_global = tmp_path / "hostile-global-config"
    ambient = {
        "GIT_CONFIG_GLOBAL": str(ambient_global),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.sshCommand",
        "GIT_CONFIG_VALUE_0": "/bin/false",
        "GIT_DIR": str(tmp_path / "other.git"),
        "GIT_WORK_TREE": str(tmp_path / "other-worktree"),
        "GIT_SSH_COMMAND": "/bin/false",
        "GIT_EXTERNAL_DIFF": "/bin/false",
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    observed: list[dict[str, str]] = []
    commands: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def observe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append(dict(kwargs["env"]))
        commands.append(tuple(args[0]))  # type: ignore[arg-type]
        return real_run(*args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(advertisement_module.subprocess, "run", observe)

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"
    assert observed
    fetch_commands = tuple(command for command in commands if "fetch" in command)
    assert len(fetch_commands) == 1
    assert "--no-recurse-submodules" in fetch_commands[0]
    for environment in observed:
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert not (
            {
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_SSH_COMMAND",
                "GIT_EXTERNAL_DIFF",
            }
            & environment.keys()
        )


def test_non_utf8_git_config_output_is_a_typed_remote_conflict(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    with (workspace / ".git/config").open("ab") as config:
        config.write(b'\n[remote "playbill"]\n\turl = invalid-\xff\n')

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "remote_conflict"


def test_advertisement_reports_git_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    monkeypatch.setenv("PATH", "")

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "git_unavailable"


def test_advertisement_is_total_for_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise MemoryError("simulated")

    monkeypatch.setattr(
        "cruxible_core.playbill.workspace_advertisement._advertise_workspace_refs",
        explode,
    )
    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "unexpected_failure"


def test_advertisement_refuses_remote_name_conflict(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    _git(workspace, "remote", "add", "playbill", str(tmp_path / "someone-elses-ledger.git"))

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "remote_conflict"


def test_advertisement_refuses_object_format_mismatch(tmp_path: Path) -> None:
    workspace, _unused = _repositories(tmp_path, "sha1")
    ledger = tmp_path / "sha256-ledger.git"
    subprocess.run(
        ["git", "init", "--bare", "--object-format=sha256", str(ledger)],
        check=True,
        capture_output=True,
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha256",
    )

    assert result.status == "failed"
    assert result.failure_code == "object_format_mismatch"


def test_advertisement_refreshes_main_and_prunes_only_proposal_refs(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    first = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )
    assert first.status == "updated"
    local_main = _git(workspace, "rev-parse", "refs/heads/main")

    producer = tmp_path / "producer"
    subprocess.run(
        ["git", "clone", str(ledger), str(producer)],
        check=True,
        capture_output=True,
    )
    _git(producer, "config", "user.name", "test")
    _git(producer, "config", "user.email", "test@example.invalid")
    (producer / "accepted.txt").write_text("accepted\n")
    _git(producer, "add", "accepted.txt")
    _git(producer, "commit", "-m", "accepted")
    _git(producer, "push", "origin", "main")
    _git(ledger, "update-ref", "-d", f"refs/heads/proposals/{PROPOSAL_KEY}")

    refreshed = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert refreshed.status == "updated"
    assert refreshed.advertised_refs == ("refs/remotes/playbill/accepted",)
    assert _git(workspace, "rev-parse", "refs/heads/main") == local_main
    assert _git(workspace, "rev-parse", "refs/remotes/playbill/accepted") == _git(
        producer, "rev-parse", "HEAD"
    )


def test_advertisement_prunes_pre_df3_remote_refs_without_touching_local_main(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    local_main = _git(workspace, "rev-parse", "refs/heads/main")
    _git(workspace, "update-ref", "refs/remotes/playbill/main", local_main)
    _git(workspace, "symbolic-ref", "refs/remotes/playbill/HEAD", "refs/remotes/playbill/main")
    _git(
        workspace,
        "update-ref",
        "refs/remotes/playbill/proposals/owner/example",
        local_main,
    )

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"
    assert _git(workspace, "rev-parse", "refs/heads/main") == local_main
    remaining = _git(
        workspace,
        "for-each-ref",
        "--format=%(refname)",
        "refs/remotes/playbill",
    ).splitlines()
    assert "refs/remotes/playbill/HEAD" not in remaining
    assert "refs/remotes/playbill/main" not in remaining
    assert "refs/remotes/playbill/proposals/owner/example" not in remaining


def test_advertisement_fetches_the_daemons_note_refs_for_the_reviewer(
    tmp_path: Path,
) -> None:
    """A note a reviewer is told to read has to reach the workspace by name.

    Under `refs/notes/` and no deeper: `git notes --ref=` prefixes anything that
    does not already begin with `refs/notes/`, so a note parked inside
    `refs/remotes/playbill/` reads back as "no note found".
    """

    workspace, ledger = _repositories(tmp_path, "sha1")
    head = _git(ledger, "rev-parse", f"refs/heads/proposals/{PROPOSAL_KEY}")
    _git(ledger, "notes", "--ref=refs/notes/playbill-eval", "add", "-m", "EVALUATION", head)

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"
    assert _git(workspace, "notes", "--ref=refs/notes/playbill-eval", "show", head) == "EVALUATION"


def test_advertisement_succeeds_on_a_ledger_that_carries_no_notes_yet(
    tmp_path: Path,
) -> None:
    """Git refuses a fetch that names one absent ref, and a fresh ledger has none."""

    workspace, ledger = _repositories(tmp_path, "sha1")
    assert _git(ledger, "for-each-ref", "--format=%(refname)", "refs/notes") == ""

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"


def test_advertisement_never_prunes_the_authors_own_notes(tmp_path: Path) -> None:
    """The daemon fetches three names; the author's `refs/notes/` is not its business."""

    workspace, ledger = _repositories(tmp_path, "sha1")
    head = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "notes", "--ref=refs/notes/commits", "add", "-m", "mine", head)
    _git(ledger, "notes", "--ref=refs/notes/playbill-eval", "add", "-m", "EVALUATION", head)

    advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert _git(workspace, "notes", "--ref=refs/notes/commits", "show", head) == "mine"
