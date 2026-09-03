from __future__ import annotations

import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.case_folding import volume_folds_case

import cruxible_core.playbill.workspace_file as workspace_file_module
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.workspace_file import WorkspaceFileSourceRequestV1
from cruxible_core.playbill.workspace_file import (
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
    workspace_binding_digest,
)


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 64,
        semantic_root="sha256:" + "b" * 64,
        generation_root="sha256:" + "c" * 64,
        compiler_digest="sha256:" + "d" * 64,
    )


def _reader(root: Path, **kwargs: object) -> WorkspaceFileReader:
    return WorkspaceFileReader(
        instance_id="workspace-reader-test",
        operating_profile="local",
        attached_roots=(root,),
        **kwargs,  # type: ignore[arg-type]
    )


def _request(root: Path, relative_path: str) -> WorkspaceFileSourceRequestV1:
    return WorkspaceFileSourceRequestV1(
        logical_source="workspace.docs",
        workspace_binding_digest=workspace_binding_digest(
            instance_id="workspace-reader-test", canonical_root=root.resolve()
        ),
        relative_path=relative_path,
        coordinate_type="workspace-snapshot-v1",
        coordinate={"revision": "working"},
        selector_type="workspace-file-v1",
        selector={"document": "docs"},
    )


def _read(
    reader: WorkspaceFileReader,
    request: WorkspaceFileSourceRequestV1,
    *,
    max_bytes: int = 1024,
):
    return reader.read(
        request,
        run_id="RUN-workspace",
        admission_binding_digest="sha256:" + "1" * 64,
        occurrence_path="source:read",
        policy_coordinate=_coordinate(),
        resolved_max_bytes=max_bytes,
        derived_request_digest="sha256:" + "2" * 64,
        read_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def test_regular_file_read_carries_only_bounded_bytes_and_receipt(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "note.txt").write_bytes(b"hello\n")
    result = _read(_reader(root), _request(root, "docs/note.txt"))

    assert result.provider_input == {
        "logical_source": "workspace.docs",
        "commitment_digest": "sha256:" + "2" * 64,
        "content_encoding": "base64",
        "bytes": "aGVsbG8K",
        "byte_length": 6,
        "bytes_digest": "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
    }
    serialized = result.receipt.model_dump(mode="json")
    assert serialized["relative_path"] == "docs/note.txt"
    assert serialized["resolved_max_bytes"] == 1024
    assert str(root.resolve()) not in repr(serialized)
    assert set(result.provider_input) == {
        "logical_source",
        "commitment_digest",
        "content_encoding",
        "bytes",
        "byte_length",
        "bytes_digest",
    }


@pytest.mark.parametrize(
    "path",
    (
        "/etc/passwd",
        "../secret",
        "a/../secret",
        "a//b",
        " leading",
        "trailing ",
        "line\nbreak",
        "delete\x7fkey",
        "\ufeffbom",
    ),
)
def test_path_grammar_refuses_absolute_and_escape(path: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(ValueError, match="normalized relative POSIX"):
        _request(root, path)


@pytest.mark.parametrize(
    ("path", "path_class"),
    (
        (".git/config", "git_metadata"),
        (".GIT/config", "git_metadata"),
        (".Git/config", "git_metadata"),
        (".gIt/config", "git_metadata"),
        ("nested/.GIT/config", "git_metadata"),
        (".playbill/coverage.json", "playbill_control"),
        (".PLAYBILL/coverage.json", "playbill_control"),
        (".PlayBill/coverage.json", "playbill_control"),
        ("owner.ed25519", "client_custody"),
        ("OWNER.ED25519", "client_custody"),
        ("Owner.Ed25519", "client_custody"),
        ("owner.ed25519.pub", "client_custody"),
        ("OWNER.ED25519.PUB", "client_custody"),
        ("daemon_ed25519", "client_custody"),
        ("DAEMON_ED25519", "client_custody"),
        ("Daemon_Ed25519.pub", "client_custody"),
        ("allowed_signers", "client_custody"),
        ("Allowed_Signers", "client_custody"),
        (".playbill-init-resume-owner.json", "client_custody"),
        (".PLAYBILL-INIT-RESUME-OWNER.JSON", "client_custody"),
    ),
)
def test_control_and_custody_path_classes_are_denied(
    path: str, path_class: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(root), _request(root, path))
    assert caught.value.path_class == path_class


DENIED_ON_DISK: tuple[tuple[str, str, str], ...] = (
    (".git/config", ".GIT/config", "git_metadata"),
    (".git/config", ".Git/CONFIG", "git_metadata"),
    (".playbill/coverage.json", ".PLAYBILL/coverage.json", "playbill_control"),
    ("owner.ed25519", "OWNER.ED25519", "client_custody"),
    ("daemon_ed25519", "DAEMON_ED25519", "client_custody"),
    ("allowed_signers", "Allowed_Signers", "client_custody"),
    (".playbill-init-resume-owner.json", ".Playbill-Init-Resume-Owner.JSON", "client_custody"),
    ("managed/state.db", "MANAGED/state.db", "managed_root"),
)


def test_case_variants_never_reach_a_denied_file_that_really_exists(tmp_path: Path) -> None:
    """The bypass premise: a folding volume resolves the variant to the real entry."""

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "note.txt").write_bytes(b"hello\n")
    folds = volume_folds_case(root)
    reader = _reader(root, managed_roots=(root / "managed",))
    if folds:
        # The volume really does fold, so every refusal below had a live bypass.
        assert _read(reader, _request(root, "DOCS/note.txt")).receipt.byte_length == 6

    for actual, requested, path_class in DENIED_ON_DISK:
        target = root / actual
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        assert (root / requested).exists() is folds
        with pytest.raises(WorkspaceFileReadRefused) as caught:
            _read(reader, _request(root, requested))
        assert caught.value.path_class == path_class, requested
        with pytest.raises(WorkspaceFileReadRefused) as caught:
            _read(reader, _request(root, actual))
        assert caught.value.path_class == path_class, actual


def test_missing_authorized_root_is_a_typed_binding_refusal(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _reader(tmp_path / "deleted-workspace")
    assert caught.value.path_class == "binding"


def test_authorized_root_cannot_turn_control_directory_into_plain_workspace(
    tmp_path: Path,
) -> None:
    control_root = tmp_path / ".GiT"
    control_root.mkdir()
    (control_root / "config").write_bytes(b"secret")

    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(control_root), _request(control_root, "config"))
    assert caught.value.path_class == "git_metadata"


def test_unknown_binding_and_cloud_refuse(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    request = _request(root, "note.txt")
    wrong = request.model_copy(update={"workspace_binding_digest": "sha256:" + "9" * 64})
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(root), wrong)
    assert caught.value.path_class == "binding"

    cloud = WorkspaceFileReader(
        instance_id="workspace-reader-test",
        operating_profile="cloud",
        attached_roots=(root,),
    )
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(cloud, request)
    assert caught.value.path_class == "cloud_no_mounts"


def test_operational_config_widens_roots_but_managed_root_is_excluded(tmp_path: Path) -> None:
    attached = tmp_path / "attached"
    allowed = tmp_path / "allowed"
    managed = allowed / "managed"
    for path in (attached, allowed, managed):
        path.mkdir()
    (allowed / "ok.txt").write_bytes(b"ok")
    (managed / "secret.txt").write_bytes(b"secret")
    reader = WorkspaceFileReader(
        instance_id="workspace-reader-test",
        operating_profile="local",
        attached_roots=(attached,),
        operational_allowed_roots=(allowed,),
        managed_roots=(managed,),
    )
    assert _read(reader, _request(allowed, "ok.txt")).receipt.byte_length == 2
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(reader, _request(allowed, "managed/secret.txt"))
    assert caught.value.path_class == "managed_root"
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(reader, _request(allowed, "MANAGED/secret.txt"))
    assert caught.value.path_class == "managed_root"


def test_managed_state_root_wins_over_operational_allowlist(tmp_path: Path) -> None:
    attached = tmp_path / "attached"
    state_root = tmp_path / "daemon-state"
    attached.mkdir()
    state_root.mkdir()
    (state_root / "daemon-secret").write_bytes(b"secret")
    reader = WorkspaceFileReader(
        instance_id="workspace-reader-test",
        operating_profile="local",
        attached_roots=(attached,),
        operational_allowed_roots=(state_root,),
        managed_roots=(state_root,),
    )

    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(reader, _request(state_root, "daemon-secret"))
    assert caught.value.path_class == "managed_root"


def test_symlink_hardlink_directory_socket_and_budget_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_bytes(b"outside")
    (root / "link").symlink_to(outside)
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "note").write_bytes(b"outside")
    (root / "directory-link").symlink_to(outside_directory)
    regular = root / "regular"
    regular.write_bytes(b"regular")
    os.link(regular, root / "hard")
    (root / "oversized").write_bytes(b"regular")
    (root / "directory").mkdir()
    sock = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(root)
    sock.bind("socket")
    try:
        expected = {
            "link": "symlink",
            "directory-link/note": "symlink",
            "hard": "hardlink",
            "directory": "non_regular",
            "socket": "non_regular",
        }
        for path, path_class in expected.items():
            with pytest.raises(WorkspaceFileReadRefused) as caught:
                _read(_reader(root), _request(root, path))
            assert caught.value.path_class == path_class
        with pytest.raises(WorkspaceFileReadRefused) as caught:
            _read(_reader(root), _request(root, "oversized"), max_bytes=2)
        assert caught.value.path_class == "size_budget"
    finally:
        sock.close()


def test_permission_denial_is_classified_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "locked").write_bytes(b"secret")
    original_open = workspace_file_module.os.open

    def refuse_locked(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        if path == "locked":
            raise PermissionError(13, "permission denied")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_file_module.os, "open", refuse_locked)
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(root), _request(root, "locked"))
    assert caught.value.path_class == "unreadable"


def test_replaced_file_after_open_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "note"
    target.write_bytes(b"first")

    original_read = workspace_file_module.os.read
    mutated = False

    def replace(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            replacement = root / "replacement"
            replacement.write_bytes(b"second")
            replacement.replace(target)
        return original_read(descriptor, count)

    monkeypatch.setattr(workspace_file_module.os, "read", replace)
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(root), _request(root, "note"))
    assert caught.value.path_class == "changed_during_read"


def test_disappearing_file_after_open_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "note"
    target.write_bytes(b"first")

    original_read = workspace_file_module.os.read
    mutated = False

    def unlink(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            target.unlink()
        return original_read(descriptor, count)

    monkeypatch.setattr(workspace_file_module.os, "read", unlink)
    with pytest.raises(WorkspaceFileReadRefused) as caught:
        _read(_reader(root), _request(root, "note"))
    assert caught.value.path_class == "missing"
