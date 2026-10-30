"""CAS opening preserves correct metadata and still repairs permissions/refuses swaps."""

import os
import stat
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import CasError
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore

READ = BodyAccessContext(principal_id="owner", can_read_body=True)


def _snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (
            (s := p.lstat()).st_ino,
            s.st_mode,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
            p.read_bytes() if p.is_file() else None,
        )
        for p in (root, *root.rglob("*"))
    }


def test_correct_mode_reopening_preserves_all_metadata_and_contents(tmp_path, monkeypatch):
    root = tmp_path / "cas"
    root.mkdir()
    first = ContentAddressedBodyStore(root)
    digest = first.store(b"retained body").digest
    algorithm = root / "sha256"
    before = _snapshot(root)
    chmod = os.chmod
    calls = []

    def observed(path, *args, **kwargs):
        if Path(path) == algorithm:
            calls.append(path)
        return chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", observed)
    for _ in range(3):
        reopened = ContentAddressedBodyStore(root)
        assert reopened.read(digest, access=READ) == b"retained body"
        assert _snapshot(root) == before
    assert calls == []


@pytest.mark.parametrize("mode", [0o000, 0o100, 0o400, 0o500, 0o600, 0o755, 0o777, 0o1700])
def test_reopening_repairs_wrong_and_restrictive_permissions(tmp_path, mode):
    root = tmp_path / "cas"
    root.mkdir()
    first = ContentAddressedBodyStore(root)
    digest = first.store(b"retained body").digest
    algorithm = root / "sha256"
    inode = algorithm.lstat().st_ino
    algorithm.chmod(mode)
    reopened = ContentAddressedBodyStore(root)
    assert algorithm.lstat().st_ino == inode
    assert stat.S_IMODE(algorithm.lstat().st_mode) == 0o700
    assert reopened.read(digest, access=READ) == b"retained body"


def test_fresh_creation_remains_private_under_restrictive_umask(tmp_path):
    root = tmp_path / "cas"
    root.mkdir()
    previous = os.umask(0o777)
    try:
        store = ContentAddressedBodyStore(root)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((root / "sha256").lstat().st_mode) == 0o700
    assert store.store(b"first body").digest


@pytest.mark.parametrize(
    "kind", ["root_symlink", "root_file", "algorithm_symlink", "algorithm_file"]
)
def test_constructor_refuses_symlinks_and_non_directories(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o750)
    sentinel = outside / "untouched"
    sentinel.write_bytes(b"outside")
    before = _snapshot(outside)
    root = tmp_path / "cas"
    if kind == "root_symlink":
        root.symlink_to(outside, target_is_directory=True)
    elif kind == "root_file":
        root.write_bytes(b"file")
    else:
        root.mkdir()
        if kind == "algorithm_symlink":
            (root / "sha256").symlink_to(outside, target_is_directory=True)
        else:
            (root / "sha256").write_bytes(b"file")
    expected = FileExistsError if kind == "algorithm_file" else CasError
    with pytest.raises(expected):
        ContentAddressedBodyStore(root)
    assert _snapshot(outside) == before


@pytest.mark.parametrize("fault", ["path_swap", "metadata_error"])
def test_open_descriptor_is_closed_when_path_validation_fails(tmp_path, monkeypatch, fault):
    root = tmp_path / "cas"
    root.mkdir()
    algorithm = root / "sha256"
    algorithm.mkdir(mode=0o700)
    original_open, original_lstat, original_close = os.open, Path.lstat, os.close
    descriptors = []
    closed = []

    def opened(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == algorithm:
            descriptors.append(descriptor)
            if fault == "path_swap":
                algorithm.rename(root / "moved")
                algorithm.mkdir(mode=0o700)
        return descriptor

    def checked(path, *args, **kwargs):
        if path == algorithm and descriptors and fault == "metadata_error":
            raise OSError("injected metadata failure")
        return original_lstat(path, *args, **kwargs)

    def close(descriptor):
        if descriptor in descriptors:
            closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(Path, "lstat", checked)
    monkeypatch.setattr(os, "close", close)
    expected = CasError if fault == "path_swap" else OSError
    with pytest.raises(expected):
        ContentAddressedBodyStore(root)
    assert len(descriptors) == 1 and closed == descriptors
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
