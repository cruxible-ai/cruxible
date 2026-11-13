"""The no-write proof itself must see every kind of write it is trusted with (R12)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support.store_snapshot import assert_writes_nothing, snapshot_stores


def _refused(roots: list[Path], write: object) -> None:
    with pytest.raises(AssertionError, match="a preview wrote"):
        assert_writes_nothing(roots, write)  # type: ignore[arg-type]


def test_a_file_passed_as_a_root_is_watched(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    store.write_bytes(b"before")

    _refused([store], lambda: store.write_bytes(b"after!"))
    _refused([store], lambda: store.chmod(0o600))
    _refused([store], store.unlink)


def test_directory_permissions_and_new_directories_are_watched(tmp_path: Path) -> None:
    watched = tmp_path / "state"
    (watched / "inner").mkdir(parents=True)

    _refused([watched], lambda: (watched / "inner").chmod(0o700))
    _refused([watched], lambda: watched.chmod(0o700))
    _refused([watched], lambda: (watched / "new").mkdir())


def test_symlinks_are_recorded_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"x")
    watched = tmp_path / "state"
    watched.mkdir()
    os.symlink(outside, watched / "escape")

    found = snapshot_stores(watched)
    assert found[str(watched / "escape")] == f"link:{outside}"
    assert not any(path.startswith(str(watched / "escape") + os.sep) for path in found)
    # Retargeting the link is a write the proof sees.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def retarget() -> None:
        (watched / "escape").unlink()
        os.symlink(elsewhere, watched / "escape")

    _refused([watched], retarget)


def test_a_symlinked_root_watches_its_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "store.db").write_bytes(b"before")
    alias = tmp_path / "alias"
    os.symlink(real, alias)

    _refused([alias], lambda: (real / "store.db").write_bytes(b"after!"))


def test_a_quiet_call_passes_and_shm_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state.db").write_bytes(b"db")

    def read() -> str:
        (tmp_path / "state.db-shm").write_bytes(b"index")
        return "read"

    assert assert_writes_nothing([tmp_path], read) == "read"
