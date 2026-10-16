"""Advisory workspace ref advertisement and Git object-format inheritance."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cruxible_core.floor.workspace_advertisement import (
    advertise_workspace_refs,
    workspace_git_object_format,
)

PROPOSAL_KEY = "a" * 64


# Notes and commits need an identity, which a CI host cannot auto-detect.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY},
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
        # Name the branch: git's default (master on a host without
        # init.defaultBranch) would leave a clone of the ledger without main.
        ["git", "init", "--bare", "-b", "main", f"--object-format={object_format}", str(ledger)],
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
        "refs/remotes/cruxible-ledger/accepted",
        f"refs/remotes/cruxible-ledger/proposals/{PROPOSAL_KEY}",
    )
    assert _git(workspace, "status", "--porcelain=v1") == before_status
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(workspace, "config", "--get-all", "remote.cruxible-ledger.fetch").splitlines() == [
        "+refs/heads/main:refs/remotes/cruxible-ledger/accepted",
        "+refs/heads/proposals/*:refs/remotes/cruxible-ledger/proposals/*",
    ]
    assert _git(workspace, "config", "--get", "remote.cruxible-ledger.tagOpt") == "--no-tags"
    assert _git(workspace, "config", "--get", "remote.cruxible-ledger.skipFetchAll") == "true"


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
    _git(workspace, "config", "--add", "remote.cruxible-ledger.url", f"../{relative}")

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
        "remote.cruxible-ledger.uploadpack",
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


@pytest.mark.parametrize("relative", [False, True])
def test_advertisement_refuses_instead_of_ssh_command_without_execution(
    tmp_path: Path,
    relative: bool,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    daemon_uid_marker = tmp_path / "daemon-uid"
    raw_url = f"../{ledger.name}" if relative else str(ledger.resolve())
    _git(workspace, "config", "remote.cruxible-ledger.url", raw_url)
    _git(
        workspace,
        "config",
        "url.ssh://attacker.invalid/x.insteadOf",
        raw_url,
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

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("workspace advertisement spawned a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )
    assert result.status == "updated"


def test_non_utf8_git_config_output_is_a_typed_remote_conflict(tmp_path: Path) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    with (workspace / ".git/config").open("ab") as config:
        config.write(b'\n[remote "cruxible-ledger"]\n\turl = invalid-\xff\n')

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "failed"
    assert result.failure_code == "remote_conflict"


def test_advertisement_needs_no_git_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    monkeypatch.setenv("PATH", "")

    result = advertise_workspace_refs(
        workspace_root=workspace,
        ledger_path=ledger,
        ledger_object_format="sha1",
    )

    assert result.status == "updated"


def test_advertisement_is_total_for_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise MemoryError("simulated")

    monkeypatch.setattr(
        "cruxible_core.floor.workspace_advertisement._advertise_workspace_refs",
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
    _git(workspace, "remote", "add", "cruxible-ledger", str(tmp_path / "someone-elses-ledger.git"))

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
    assert refreshed.advertised_refs == ("refs/remotes/cruxible-ledger/accepted",)
    assert _git(workspace, "rev-parse", "refs/heads/main") == local_main
    assert _git(workspace, "rev-parse", "refs/remotes/cruxible-ledger/accepted") == _git(
        producer, "rev-parse", "HEAD"
    )


def test_advertisement_prunes_pre_df3_remote_refs_without_touching_local_main(
    tmp_path: Path,
) -> None:
    workspace, ledger = _repositories(tmp_path, "sha1")
    local_main = _git(workspace, "rev-parse", "refs/heads/main")
    _git(workspace, "update-ref", "refs/remotes/cruxible-ledger/main", local_main)
    _git(
        workspace,
        "symbolic-ref",
        "refs/remotes/cruxible-ledger/HEAD",
        "refs/remotes/cruxible-ledger/main",
    )
    _git(
        workspace,
        "update-ref",
        "refs/remotes/cruxible-ledger/proposals/owner/example",
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
        "refs/remotes/cruxible-ledger",
    ).splitlines()
    assert "refs/remotes/cruxible-ledger/HEAD" not in remaining
    assert "refs/remotes/cruxible-ledger/main" not in remaining
    assert "refs/remotes/cruxible-ledger/proposals/owner/example" not in remaining


def test_advertisement_fetches_the_daemons_note_refs_for_the_reviewer(
    tmp_path: Path,
) -> None:
    """A note a reviewer is told to read has to reach the workspace by name.

    Under `refs/notes/` and no deeper: `git notes --ref=` prefixes anything that
    does not already begin with `refs/notes/`, so a note parked inside
    `refs/remotes/cruxible-ledger/` reads back as "no note found".
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


def test_linked_worktree_uses_common_config_objects_and_refs(tmp_path):
    workspace, ledger = _repositories(tmp_path, "sha256")
    linked = tmp_path / "linked"
    _git(workspace, "worktree", "add", "-b", "linked", str(linked))
    _git(ledger, "pack-refs", "--all")
    _git(ledger, "repack", "-a", "-d")
    result = advertise_workspace_refs(
        workspace_root=linked, ledger_path=ledger, ledger_object_format="sha256"
    )
    assert result.status == "updated"
    assert _git(linked, "rev-parse", "refs/remotes/cruxible-ledger/accepted") == _git(
        ledger, "rev-parse", "main"
    )
    _git(linked, "fsck", "--strict", "--no-dangling")


def test_advertisement_keeps_unicode_local_urls_git_readable(tmp_path):
    directory = tmp_path / "café"
    directory.mkdir()
    workspace, ledger = _repositories(directory, "sha1")
    result = advertise_workspace_refs(
        workspace_root=workspace, ledger_path=ledger, ledger_object_format="sha1"
    )
    assert result.status == "updated"
    assert _git(workspace, "config", "--get", "remote.cruxible-ledger.url") == str(ledger)


def test_config_publication_cleanup_preserves_the_next_writers_lock(tmp_path, monkeypatch):
    from cruxible_core.floor.workspace_advertisement import _config, _configure_remote

    workspace, ledger = _repositories(tmp_path, "sha1")
    common = workspace / ".git"
    config = common / "config"
    lock = common / "config.lock"
    real_replace = os.replace
    successor_fd = None

    def publish(source, destination):
        nonlocal successor_fd
        real_replace(source, destination)
        if Path(destination) == config:
            successor_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.write(successor_fd, b"successor config\n")

    monkeypatch.setattr(os, "replace", publish)
    try:
        _configure_remote(common, _config(config), str(ledger.resolve()), workspace)
        assert successor_fd is not None
        assert lock.read_bytes() == b"successor config\n"
        assert lock.stat().st_ino == os.fstat(successor_fd).st_ino
        with pytest.raises(FileExistsError):
            os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    finally:
        if successor_fd is not None:
            os.close(successor_fd)
        lock.unlink(missing_ok=True)


def _subject_ledger(tmp_path, object_format, size):
    from cruxible_core.governance.keys import generate_daemon_key
    from cruxible_core.ledger.git import GitLedger
    from tests.core_support._projection_support import subject_bytes

    tmp_path.mkdir()
    key = generate_daemon_key(tmp_path / "credentials")
    signers = tmp_path / "allowed"
    signers.write_bytes(b"daemon " + key.public_key_path.read_bytes())
    ledger = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=object_format,
        signing_key_path=key.private_key_path,
        allowed_signers_path=signers,
    )
    tree = {f"subjects/project.work_item/s{i}.json": subject_bytes(f"s{i}") for i in range(size)}
    base = ledger.create_signed_genesis(tree, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", f"--object-format={object_format}", str(workspace)],
        check=True,
        capture_output=True,
    )
    return workspace, ledger, base, tree


def _change_one_subject(ledger, base, *, subject_id="s0", sequence=1):
    from cruxible_core.derived.derived_state import SnapshotTree
    from tests.core_support._projection_support import subject_bytes

    root = SnapshotTree(ledger.blob_refs_at(base))
    root._commit_oid = base
    edit = root.fork()
    edit[f"subjects/project.work_item/{subject_id}.json"] = subject_bytes(subject_id, retired=True)
    with ledger.request_pack(), ledger.activation_lock():
        generation = ledger.create_signed_generation(
            edit.snapshot(),
            parent_oid=base,
            sequence=sequence,
            timestamp="2026-10-15T12:01:00Z",
            message="Retire one Subject",
        )
        with ledger.prepared_generation_note(generation, b"descriptor\n") as publish_note:
            assert ledger.compare_and_set_main(generation, expected_oid=base)
            publish_note()
    return generation


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("coalesced", (False, True))
def test_warm_workspace_transfer_work_is_change_sized(
    tmp_path,
    monkeypatch,
    object_format,
    coalesced,
):
    from cruxible_core.floor import workspace_advertisement as advertisement
    from cruxible_core.ledger.git import _batch_reader
    from tests.core_support._projection_support import subject_bytes

    counts = []
    for size in (80, 800):
        workspace, ledger, base, tree = _subject_ledger(tmp_path / str(size), object_format, size)
        assert (
            advertise_workspace_refs(
                workspace_root=workspace,
                ledger_path=ledger.path,
                ledger_object_format=object_format,
            ).status
            == "updated"
        )
        generation = _change_one_subject(ledger, base)
        if coalesced:
            generation = _change_one_subject(ledger, generation, subject_id="s1", sequence=2)
        work = {"metadata": 0, "children": 0, "parsed_children": 0}
        writer = _batch_reader(workspace / ".git").store
        real_infos = writer.infos
        real_frontier = advertisement._tree_transfer_frontier
        real_entries = advertisement._tree_entries

        def infos(oids):
            work["metadata"] += len(oids)
            return real_infos(oids)

        def frontier(oid):
            result = real_frontier(oid)
            if result is not None:
                work["children"] += len(result.children)
            return result

        def entries(*args, **kwargs):
            result = real_entries(*args, **kwargs)
            if result is not None:
                work["parsed_children"] += len(result)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(writer, "infos", infos)
            patch.setattr(advertisement, "_tree_transfer_frontier", frontier)
            patch.setattr(advertisement, "_tree_entries", entries)
            result = advertise_workspace_refs(
                workspace_root=workspace,
                ledger_path=ledger.path,
                ledger_object_format=object_format,
            )
        assert result.status == "updated"
        counts.append(work)
        assert _git(workspace, "rev-parse", "refs/remotes/cruxible-ledger/accepted") == generation
        assert _git(workspace, "show", f"{generation}:subjects/project.work_item/s0.json") == (
            subject_bytes("s0", retired=True).decode().strip()
        )
        unchanged = f"subjects/project.work_item/s{size - 1}.json"
        assert (
            _git(workspace, "show", f"{generation}:{unchanged}") == tree[unchanged].decode().strip()
        )
        _git(workspace, "fsck", "--strict", "--no-dangling")
    assert counts[0] == counts[1], counts
    assert 0 < counts[1]["children"] < 20, counts
    assert counts[1]["parsed_children"] == 0, counts
    assert counts[1]["metadata"] < 64, counts


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("evicted", (False, True))
def test_workspace_transfer_recovers_without_a_warm_baseline(
    tmp_path,
    monkeypatch,
    object_format,
    evicted,
):
    from cruxible_core.floor import workspace_advertisement as advertisement

    workspace, ledger, base, tree = _subject_ledger(tmp_path / "source", object_format, 8)
    generation = _change_one_subject(ledger, base)
    if evicted:
        monkeypatch.setattr(advertisement, "_tree_transfer_frontier", lambda _oid: None)
    # First attachment has neither the parent nor any unchanged child. A source
    # change record alone must not be mistaken for a complete dependency list.
    assert (
        advertise_workspace_refs(
            workspace_root=workspace, ledger_path=ledger.path, ledger_object_format=object_format
        ).status
        == "updated"
    )
    assert _git(workspace, "rev-parse", "refs/remotes/cruxible-ledger/accepted") == generation
    path = "subjects/project.work_item/s7.json"
    assert _git(workspace, "show", f"{generation}:{path}") == tree[path].decode().strip()
    _git(workspace, "fsck", "--strict", "--no-dangling")
