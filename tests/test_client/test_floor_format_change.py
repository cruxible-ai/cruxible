"""A floor format bump replaces the workspace floor whole; a foreign directory still refuses.

The floor's README names the product, so renaming it changed floor bytes for
the same accepted state, and the floor format moved to v6. A workspace holding
a v5 floor must take the v6 floor on its next refresh, not refuse it as a
non-empty directory the caller owns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.workspace import (
    WorkspaceDirectoryConflict,
    WorkspaceError,
    ensure_workspace_directory,
    holds_floor_of_another_format,
    materialize_floor,
    write_workspace_floor_delta,
)
from cruxible_client.contracts.floor import FLOOR_FORMAT
from cruxible_client.contracts.workspace_layout import FLOOR_PATH
from tests.support.floor_exports import floor_v5_delta, floor_v5_export

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="4" * 64,
    semantic_root="sha256:" + "1" * 64,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)
HEAD = {"README.md": (b"# Cruxible floor\n", 1), "current/k/a.yaml": (b"status: ready\n", 1)}


def _v5_floor(workspace: Path) -> Path:
    floor = workspace / FLOOR_PATH
    (floor / "current/k").mkdir(parents=True)
    (floor / "README.md").write_text("# Playbill floor\n", encoding="utf-8")
    (floor / "current/k/old.yaml").write_text("status: stale\n", encoding="utf-8")
    (floor / "manifest.json").write_text(
        json.dumps({"format": "playbill-floor-export-v5", "tag": "playbill-floor-manifest-v5"}),
        encoding="utf-8",
    )
    return floor


def test_only_a_floor_of_an_older_format_counts(tmp_path: Path) -> None:
    floor = _v5_floor(tmp_path)
    assert holds_floor_of_another_format(floor, FLOOR_FORMAT)
    (floor / "manifest.json").write_text(json.dumps({"format": FLOOR_FORMAT}), encoding="utf-8")
    assert not holds_floor_of_another_format(floor, FLOOR_FORMAT)
    (floor / "manifest.json").write_text("{}", encoding="utf-8")
    assert not holds_floor_of_another_format(floor, FLOOR_FORMAT)


def test_the_delta_writer_replaces_an_older_format_floor_whole(tmp_path: Path) -> None:
    floor = _v5_floor(tmp_path)

    _delta, written = write_workspace_floor_delta(
        lambda _generation, _renderer: floor_v5_delta(HEAD, coordinate=COORDINATE, generation=1),
        instance_id="inst",
        workspace=tmp_path,
        force=False,
    )

    assert written.status == "written"
    assert not (floor / "current/k/old.yaml").exists()
    assert (floor / "README.md").read_text(encoding="utf-8") == "# Cruxible floor\n"
    manifest = json.loads((floor / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "playbill-floor-export-v6"


def test_the_full_export_writer_replaces_an_older_format_floor_whole(tmp_path: Path) -> None:
    floor = _v5_floor(tmp_path)
    export = floor_v5_export(
        {path: content for path, (content, _at) in HEAD.items()}, coordinate=COORDINATE
    )

    written = materialize_floor(tmp_path, export=export, force=False)

    assert written.status == "written"
    assert not (floor / "current/k/old.yaml").exists()


def test_a_directory_that_holds_no_floor_still_refuses(tmp_path: Path) -> None:
    floor = tmp_path / FLOOR_PATH
    floor.mkdir(parents=True)
    (floor / "notes.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="non-empty directory"):
        write_workspace_floor_delta(
            lambda _generation, _renderer: floor_v5_delta(
                HEAD, coordinate=COORDINATE, generation=1
            ),
            instance_id="inst",
            workspace=tmp_path,
            force=False,
        )


@pytest.mark.parametrize("name", ["instance.json", "state.db"])
def test_a_workspace_holding_a_0_3_instance_refuses_with_a_repair(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / ".cruxible").mkdir()
    (tmp_path / ".cruxible" / name).write_text("{}", encoding="utf-8")

    with pytest.raises(WorkspaceDirectoryConflict) as refused:
        ensure_workspace_directory(tmp_path)

    assert refused.value.error_code == "cruxible.workspace.directory_conflict"
    assert refused.value.repair_commands == (f"mv {tmp_path}/.cruxible {tmp_path}/.cruxible-0.3",)


def test_the_home_directory_is_never_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    with pytest.raises(WorkspaceDirectoryConflict, match="daemon state root"):
        ensure_workspace_directory(tmp_path.resolve())
