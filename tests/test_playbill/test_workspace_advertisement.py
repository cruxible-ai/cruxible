"""Advisory workspace ref advertisement and Git object-format inheritance."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cruxible_core.playbill import workspace_advertisement as advertisement_module
from cruxible_core.playbill.workspace_advertisement import (
    advertise_workspace_refs,
    workspace_git_object_format,
)


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
    _git(ledger, "update-ref", "refs/proposals/owner/example", head)
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
    _git(ledger, "update-ref", "refs/proposals/owner/example", head)
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
        "refs/remotes/playbill/main",
        "refs/remotes/playbill/proposals/owner/example",
    )
    assert _git(workspace, "status", "--porcelain=v1") == before_status
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(workspace, "config", "--get-all", "remote.playbill.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/playbill/main",
        "+refs/proposals/*:refs/remotes/playbill/proposals/*",
    ]
    assert _git(workspace, "config", "--get", "remote.playbill.tagOpt") == "--no-tags"
    assert _git(workspace, "config", "--get", "remote.playbill.skipFetchAll") == "true"


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
    workspace, _ledger = _repositories(tmp_path, "sha1")
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
    real_run = subprocess.run

    def observe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append(dict(kwargs["env"]))
        return real_run(*args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(advertisement_module.subprocess, "run", observe)

    assert workspace_git_object_format(workspace) == "sha1"
    assert observed
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
    _git(ledger, "update-ref", "-d", "refs/proposals/owner/example")

    refreshed = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert refreshed.status == "updated"
    assert refreshed.advertised_refs == ("refs/remotes/playbill/main",)
    assert _git(workspace, "rev-parse", "refs/heads/main") == local_main
    assert _git(workspace, "rev-parse", "refs/remotes/playbill/main") == _git(
        producer, "rev-parse", "HEAD"
    )
