"""`.cruxible/identity.json`: the folder's default principal, never a secret."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cruxible_client.authoring.folder_identity import (
    FOLDER_IDENTITY_PATH,
    FolderIdentityRefused,
    folder_key_custody,
    read_folder_identity,
    require_folder_key,
    write_folder_identity,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".cruxible").mkdir(parents=True)
    return folder.resolve()


def test_the_record_names_a_principal_its_instance_and_an_absolute_key_directory(
    folder: Path, tmp_path: Path
) -> None:
    keys = tmp_path / "keys" / "owner"
    path, status = write_folder_identity(
        folder, instance_id="inst_a", principal_id="owner", key_dir=keys
    )

    assert (path, status) == (folder / FOLDER_IDENTITY_PATH, "written")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "tag": "cruxible-folder-identity-v1",
        "instance_id": "inst_a",
        "principal_id": "owner",
        "key_dir": str(keys.resolve()),
    }
    record = read_folder_identity(folder)
    assert record is not None and record.private_key_path == keys.resolve() / "owner.ed25519"
    assert write_folder_identity(
        folder, instance_id="inst_a", principal_id="owner", key_dir=keys
    ) == (path, "unchanged")
    assert read_folder_identity(tmp_path) is None


def test_another_principal_is_kept_unless_replaced_and_a_stale_instance_is_replaced(
    folder: Path, tmp_path: Path
) -> None:
    keys = tmp_path / "keys"
    write_folder_identity(folder, instance_id="inst_a", principal_id="owner", key_dir=keys)

    _, status = write_folder_identity(
        folder, instance_id="inst_a", principal_id="reviewer", key_dir=keys
    )
    assert status == "kept"
    assert read_folder_identity(folder).principal_id == "owner"  # type: ignore[union-attr]

    _, status = write_folder_identity(
        folder, instance_id="inst_a", principal_id="reviewer", key_dir=keys, replace=True
    )
    assert status == "written"
    _, status = write_folder_identity(
        folder, instance_id="inst_b", principal_id="owner", key_dir=keys
    )
    assert status == "written"
    assert read_folder_identity(folder).instance_id == "inst_b"  # type: ignore[union-attr]


def test_a_git_folder_keeps_the_record_out_of_git(folder: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(folder)], check=True)
    write_folder_identity(folder, instance_id="inst_a", principal_id="owner", key_dir=tmp_path)
    ignored = subprocess.run(
        ["git", "-C", str(folder), "check-ignore", "--quiet", FOLDER_IDENTITY_PATH],
        check=False,
    )
    assert ignored.returncode == 0


def test_the_record_is_never_a_symlink_and_never_holds_a_secret(
    folder: Path, tmp_path: Path
) -> None:
    path, _ = write_folder_identity(
        folder, instance_id="inst_a", principal_id="owner", key_dir=tmp_path
    )
    target = tmp_path / "elsewhere.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(FolderIdentityRefused) as refused:
        read_folder_identity(folder)
    assert refused.value.error_code == "cruxible.identity.folder_record_invalid"

    path.unlink()
    record = json.loads(target.read_text(encoding="utf-8"))
    for extra in ({"token": "x"}, {"key_dir": "relative/keys"}, {"principal_id": "Not An ID"}):
        path.write_text(json.dumps({**record, **extra}), encoding="utf-8")
        with pytest.raises(FolderIdentityRefused) as refused:
            read_folder_identity(folder)
        assert refused.value.error_code == "cruxible.identity.folder_record_invalid"
        assert "cruxible init" in str(refused.value)


def test_a_missing_key_directory_or_key_refuses_by_name(folder: Path, tmp_path: Path) -> None:
    path, _ = write_folder_identity(
        folder, instance_id="inst_a", principal_id="owner", key_dir=tmp_path / "gone"
    )
    record = read_folder_identity(folder)
    assert record is not None
    with pytest.raises(FolderIdentityRefused) as refused:
        require_folder_key(record, path=path)
    assert refused.value.error_code == "cruxible.identity.folder_key_unavailable"

    (tmp_path / "gone").mkdir()
    with pytest.raises(FolderIdentityRefused, match="holds no owner.ed25519"):
        require_folder_key(record, path=path)


def test_the_key_custody_check_reads_current_modes_and_its_stamp_follows_them(
    folder: Path, tmp_path: Path
) -> None:
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    key = keys / "owner.ed25519"
    key.write_text("not parsed here", encoding="utf-8")
    key.chmod(0o600)
    path, _ = write_folder_identity(
        folder, instance_id="inst_a", principal_id="owner", key_dir=keys
    )
    record = read_folder_identity(folder)
    assert record is not None
    stamp = folder_key_custody(record, path=path)
    assert folder_key_custody(record, path=path) == stamp

    for item, mode, detail in (
        (key, 0o000, "not readable"),
        (key, 0o640, "exclude group/world"),
        (keys, 0o755, "exclude group/world"),
    ):
        restore = 0o700 if item == keys else 0o600
        item.chmod(mode)
        try:
            with pytest.raises(FolderIdentityRefused, match=detail) as refused:
                folder_key_custody(record, path=path)
            assert refused.value.error_code == "cruxible.identity.folder_key_unavailable"
        finally:
            item.chmod(restore)
    # A chmod there and back still moves the stamp (ctime), so no cached match survives it.
    assert folder_key_custody(record, path=path) != stamp
