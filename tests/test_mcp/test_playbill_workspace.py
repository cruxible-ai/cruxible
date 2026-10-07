"""MCP owns local floor writes while the daemon remains filesystem-free."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.floor import FloorDelta
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.workspace import resolve_workspace_path
from tests.support.floor_exports import delta_from_export, floor_v5_export
from tests.support.mcp_daemon import bind_mcp_daemon


def _coordinate(seed: str = "1") -> contracts.AcceptedCoordinate:
    return contracts.AcceptedCoordinate(
        git_oid=seed * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export() -> contracts.FloorExport:
    return floor_v5_export({"cards/fresh.json": b'{"fresh":true}\n'}, coordinate=_coordinate())


class _StubClient:
    def activate_proposal(self, instance_id: str, proposal_id: str) -> contracts.ActivationReceipt:
        return contracts.ActivationReceipt(
            proposal_id=proposal_id,
            activated_by="owner",
            status="accepted",
            accepted_coordinate=_coordinate(),
            workspace_advertisement={"status": "not_attached", "workspace_path": None},
        )

    def export_floor(
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
    ) -> contracts.FloorExport:
        return _export()

    def floor_delta(
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> FloorDelta:
        return delta_from_export(_export())

    def head(self, instance_id: str) -> contracts.Head:
        return contracts.Head(
            instance=instance_id,
            coordinate=_coordinate().model_dump(mode="json"),  # type: ignore[arg-type]
            generation=3,
        )

    def host_workspace_registration(
        self, instance_id: str, *, workspace_root: str | None = None
    ) -> contracts.HostWorkspaceRegistration:
        # Delivery off: the floor export is this client's to write.
        return contracts.HostWorkspaceRegistration(
            instance_id=instance_id, status="not_registered", delivers_here=False
        )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
    )
    (root / ".cruxible").mkdir(parents=True)
    (root / ".cruxible/coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "format": "playbill-floor-export-v2",
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_activate_is_a_daemon_act_that_writes_nothing_locally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The daemon's trigger delivers the floor; MCP activate returns the receipt alone."""

    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    bind_mcp_daemon(monkeypatch, _StubClient())
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    assert isinstance(result, contracts.ActivationReceipt)
    assert result.status == "accepted"
    assert sorted(path.relative_to(workspace) for path in workspace.rglob("*")) == before


def test_workspace_status_compares_the_installed_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    bind_mcp_daemon(monkeypatch, _StubClient())
    handlers.handle_playbill_floor_export("inst_test", mode="write")

    status = handlers.handle_playbill_floor_export("inst_test", mode="status")

    assert status.status == "current"
    assert status.installed_coordinate == _coordinate()


def test_floor_write_still_refuses_a_directory_holding_something_else(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    bind_mcp_daemon(monkeypatch, _StubClient())
    floor = workspace / ".cruxible/floor"
    floor.mkdir(parents=True)
    (floor / "occupied.txt").write_text("not the floor\n", encoding="utf-8")

    with pytest.raises(Exception, match="non-empty directory"):
        handlers.handle_playbill_floor_export("inst_test", mode="write")
    assert (floor / "occupied.txt").is_file()


def test_floor_export_from_nested_cwd_uses_the_containing_git_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    nested = workspace / "a/b/sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    bind_mcp_daemon(monkeypatch, _StubClient())

    written = handlers.handle_playbill_floor_export("inst_test", mode="write")

    assert written.destination == str(workspace / ".cruxible/floor")
    assert (workspace / ".cruxible/floor/cards/fresh.json").is_file()
    assert not (nested / ".cruxible/floor").exists()


def test_explicit_nested_mcp_root_refuses_to_write_outside_its_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    scoped_root = workspace / "scoped/subdir"
    scoped_root.mkdir(parents=True)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(scoped_root))
    bind_mcp_daemon(monkeypatch, _StubClient())

    with pytest.raises(ConfigError, match="must name the Git worktree root"):
        handlers.handle_playbill_floor_export("inst_test", mode="write")

    assert not (workspace / ".cruxible/floor").exists()
    assert not (scoped_root / ".cruxible/floor").exists()


@pytest.mark.parametrize(
    "value",
    ["../outside", "/" + "tmp/outside", "source/../outside", "./source"],
)
def test_workspace_path_refuses_lexical_escape_forms(
    tmp_path: Path,
    value: str,
) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(DataValidationError, match="normalized, relative"):
        resolve_workspace_path(value, root=workspace)


def test_workspace_path_refuses_symlink_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.md").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DataValidationError, match="escapes the configured root"):
        resolve_workspace_path("escape/source.md", root=workspace, kind="file")


def _export_files(files: dict[str, bytes]) -> contracts.FloorExport:
    return floor_v5_export(files, coordinate=_coordinate())


class _PartsClient(_StubClient):
    """Exports the discovery cards only when asked for them."""

    def __init__(self) -> None:
        self.includes: list[tuple[str, ...]] = []

    def export_floor(  # type: ignore[override]
        self,
        instance_id: str,
        *,
        at=None,
        include=(),  # type: ignore[no-untyped-def]
    ) -> contracts.FloorExport:
        self.includes.append(tuple(include))
        files = {"current/k/a.yaml": b"# k/a  kind=k\n"}
        if "discovery" in include:
            files["subjects/k/a.profile.json"] = b"{}\n"
        return _export_files(files)

    def floor_delta(  # type: ignore[override]
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> FloorDelta:
        # The default floor travels as a delta, never with the discovery cards.
        self.includes.append(())
        return delta_from_export(_export_files({"current/k/a.yaml": b"# k/a  kind=k\n"}))


def test_an_mcp_write_with_discovery_records_the_floor_output_part(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    client = _PartsClient()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    bind_mcp_daemon(monkeypatch, client)
    profile = workspace / ".cruxible/floor/subjects/k/a.profile.json"

    written = handlers.handle_playbill_floor_export(
        "inst_test", mode="write", force=True, include=("discovery",)
    )

    assert written.status == "written"
    assert profile.is_file()
    config = json.loads((workspace / ".cruxible/coverage.json").read_text(encoding="utf-8"))
    assert config["floor_output"] == {
        "tag": "playbill-floor-output-v1",
        "format": "playbill-floor-export-v6",
        "include": ["discovery"],
    }
