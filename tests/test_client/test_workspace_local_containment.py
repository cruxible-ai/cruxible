"""Local joins and profiles retain their directory anchors through workspace swaps."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client.authoring import floor_apply
from cruxible_client.authoring import workspace as authoring
from cruxible_client.authoring.workspace import PlaybillWorkspaceError
from tests.support.floor_exports import floor_v5_delta
from tests.test_client.test_floor_apply import COORDINATE


@pytest.mark.parametrize("relative", ["sources/INDEX", "projections/INDEX", "coverage.json"])
@pytest.mark.parametrize("boundary", ["stage", "replace"])
def test_local_writes_do_not_follow_a_directory_swapped_after_open(
    tmp_path, monkeypatch, relative, boundary
):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    floor = workspace / ".playbill/floor"
    floor_apply.apply_floor_delta(
        floor,
        floor_v5_delta(
            {"sources/LEDGER": (b"# sources LEDGER  0 sources  changed gen 0\n", 0)},
            coordinate=COORDINATE,
            generation=1,
        ),
    )
    profile = relative == "coverage.json"
    parent = workspace / ".playbill" if profile else (floor / relative).parent
    parent.mkdir(exist_ok=True)
    identity = parent.stat().st_ino
    outside = tmp_path / "outside"
    outside.mkdir()
    name = Path(relative).name
    (outside / name).write_bytes(b"outside sentinel\n")
    held = parent.with_name(parent.name + "-held")
    swapped = False
    open_file = os.open
    replace_file = os.replace

    def swap():
        nonlocal swapped
        parent.rename(held)
        parent.symlink_to(outside, target_is_directory=True)
        swapped = True

    def targets_parent(path, descriptor):
        # Also exercise the old pathname shape: tempfile's os.open and replace
        # would follow this parent after its real-path containment check.
        if descriptor is not None:
            return os.fstat(descriptor).st_ino == identity
        return Path(path).parent == parent

    def open_at(path, flags, mode=0o777, *, dir_fd=None):
        if (
            boundary == "stage"
            and not swapped
            and flags & os.O_CREAT
            and targets_parent(path, dir_fd)
        ):
            swap()
        return open_file(path, flags, mode, dir_fd=dir_fd)

    def replace_at(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if boundary == "replace" and not swapped and targets_parent(destination, dst_dir_fd):
            swap()
        return replace_file(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(floor_apply.os, "open", open_at)
    monkeypatch.setattr(floor_apply.os, "replace", replace_at)
    if profile:
        authoring._atomic_write_workspace_config(
            workspace / ".playbill/coverage.json", {"instance_id": "inst_local"}
        )
    else:
        assert authoring.write_projection_index(workspace) == 0
    assert swapped
    assert (outside / name).read_bytes() == b"outside sentinel\n"
    assert sorted(path.name for path in outside.iterdir()) == [name]
    assert (held / name).is_file()
    assert (held / name).read_bytes() != b"outside sentinel\n"


@pytest.mark.parametrize("component", ["workspace", ".playbill", "floor", "sources", "projections"])
def test_local_index_refuses_a_component_swapped_before_open(tmp_path, monkeypatch, component):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    floor = workspace / ".playbill/floor"
    (floor / "sources").mkdir(parents=True)
    (floor / "projections").mkdir()
    target = {
        "workspace": workspace,
        ".playbill": workspace / ".playbill",
        "floor": floor,
        "sources": floor / "sources",
        "projections": floor / "projections",
    }[component]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "INDEX").write_bytes(b"outside sentinel\n")
    original = os.open
    swapped = False

    def open_at(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == component and flags & os.O_DIRECTORY:
            target.rename(target.with_name(target.name + "-held"))
            target.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(floor_apply.os, "open", open_at)
    relative = "projections/INDEX" if component == "projections" else "sources/INDEX"
    with pytest.raises(PlaybillWorkspaceError, match="link or a file"):
        authoring._write_floor_local(workspace, relative, "index\n")
    assert swapped
    assert (outside / "INDEX").read_bytes() == b"outside sentinel\n"
    assert sorted(path.name for path in outside.iterdir()) == ["INDEX"]


def test_join_does_not_bless_a_workspace_swap_by_resolving_it_again(tmp_path, monkeypatch):
    workspace = (tmp_path / "workspace").resolve()
    outside = (tmp_path / "outside").resolve()
    for root in (workspace, outside):
        root.mkdir()
        floor_apply.apply_floor_delta(
            root / ".playbill/floor",
            floor_v5_delta(
                {"sources/LEDGER": (b"# sources LEDGER  0 sources  changed gen 0\n", 0)},
                coordinate=COORDINATE,
                generation=1,
            ),
        )
    for relative in ("sources/INDEX", "projections/INDEX"):
        target = outside / ".playbill/floor" / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"outside sentinel\n")
    resolve = authoring._workspace_root

    def swap_before_resolve(path):
        workspace.rename(workspace.with_name("workspace-held"))
        workspace.symlink_to(outside, target_is_directory=True)
        return resolve(path)

    monkeypatch.setattr(authoring, "_workspace_root", swap_before_resolve)
    with pytest.raises(PlaybillWorkspaceError, match="link or a file"):
        authoring.write_projection_index(workspace)
    for relative in ("sources/INDEX", "projections/INDEX"):
        assert (outside / ".playbill/floor" / relative).read_bytes() == b"outside sentinel\n"
