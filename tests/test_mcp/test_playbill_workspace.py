"""MCP owns local floor writes while the daemon remains filesystem-free."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV2,
    ProjectionClaimBackingV1,
    frame_projection_block,
)
from cruxible_client.contracts.floor import PlaybillFloorDeltaV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.workspace import resolve_workspace_path
from tests.support.floor_exports import delta_from_export, floor_v5_export


def _coordinate(seed: str = "1") -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid=seed * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export() -> contracts.PlaybillFloorExport:
    return floor_v5_export({"cards/fresh.json": b'{"fresh":true}\n'}, coordinate=_coordinate())


class _StubClient:
    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt:
        return contracts.PlaybillActivationReceipt(
            proposal_id=proposal_id,
            activated_by="owner",
            status="accepted",
            accepted_coordinate=_coordinate(),
            workspace_advertisement={"status": "not_attached", "workspace_path": None},
        )

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
    ) -> contracts.PlaybillFloorExport:
        return _export()

    def playbill_floor_delta(
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> PlaybillFloorDeltaV1:
        return delta_from_export(_export())

    def playbill_head(self, instance_id: str) -> contracts.PlaybillHeadV1:
        return contracts.PlaybillHeadV1(
            instance=instance_id,
            coordinate=_coordinate().model_dump(mode="json"),  # type: ignore[arg-type]
            generation=3,
        )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
    )
    (root / ".playbill").mkdir(parents=True)
    (root / ".playbill/coverage.json").write_text(
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


def test_activate_refreshes_the_operator_configured_workspace(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    assert result.status == "accepted"
    assert result.floor_refresh.status == "refreshed"
    assert (workspace / ".playbill/floor/cards/fresh.json").is_file()


def test_activate_from_nested_cwd_refreshes_the_containing_git_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    nested = workspace / "a/b/sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    assert result.status == "accepted"
    assert result.floor_refresh.status == "refreshed"
    assert (workspace / ".playbill/floor/cards/fresh.json").is_file()
    assert not (nested / ".playbill/floor").exists()


def test_activate_outside_a_git_worktree_skips_the_floor_refresh_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.delenv("CRUXIBLE_MCP_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    assert result.status == "accepted"
    assert result.floor_refresh.status == "not_configured"
    assert result.floor_refresh.message is not None
    assert "Git worktree" in result.floor_refresh.message
    assert result.block_sync is None
    assert not (plain / ".playbill").exists()


def test_library_mode_activate_checks_an_attached_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".playbill/coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": "inst_test",
                "server_socket": str(tmp_path / "daemon.sock"),
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "format": "playbill-floor-export-v2",
                },
            }
        ),
        encoding="utf-8",
    )
    old_body = b"status: old\n"
    # The page declares it must stay current, so drift under it gates the sweep.
    stamp = ProjectionBlockStampV2(
        source_id="corpus.runbook",
        block_id="pub-mcp",
        declared_generation=1,
        declared_coordinate=AcceptedCoordinate.model_validate(_coordinate().model_dump()),
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
                statement_digest="sha256:" + "7" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(old_body).hexdigest(),
        currency_policy="require_current",
    )
    source = workspace / "runbook.md"
    source.write_bytes(frame_projection_block(stamp=stamp, body=old_body))
    before = source.read_bytes()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_activate",
        lambda instance_id, proposal_id: contracts.PlaybillActivationReceipt(
            proposal_id=proposal_id,
            activated_by="owner",
            status="accepted",
            accepted_coordinate=_coordinate(),
            workspace_advertisement={"status": "updated", "workspace_path": str(workspace)},
        ),
    )
    monkeypatch.setattr(handlers.playbill_api, "playbill_export_floor", lambda _instance: _export())
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_floor_delta",
        lambda _instance, **_kwargs: delta_from_export(_export()),
    )

    checked: list[contracts.PlaybillProjectionCheckRequestV1] = []

    def check_blocks(
        instance_id: str,
        *,
        request: contracts.PlaybillProjectionCheckRequestV1,
    ) -> contracts.PlaybillProjectionCheckResultV1:
        # Block sync asks the daemon for every stamp's currency in one batch.
        assert instance_id == "inst_test"
        checked.append(request)
        coordinate = AcceptedCoordinate.model_validate(_coordinate().model_dump())
        results = []
        for held in request.stamps:
            moved = ProjectionClaimBackingV1(
                identity=held.backing[0].identity,
                statement_digest="sha256:" + "a" * 64,
            )
            results.append(
                contracts.PlaybillBlockSyncReadResultV1(
                    status="successor",
                    original_artifact_digest="sha256:" + "8" * 64,
                    artifact_digest="sha256:" + "9" * 64,
                    coordinate=coordinate,
                    generation=2,
                    backing=moved,
                    moved_backings=(moved,),
                )
            )
        return contracts.PlaybillProjectionCheckResultV1(
            coordinate=coordinate,
            evaluation_time=datetime(2026, 9, 16, tzinfo=UTC),
            results=tuple(results),
        )

    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_check_projection_blocks",
        check_blocks,
    )

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    # The closing sweep an activation runs REPORTS: nothing renders a block, so
    # a block whose held backing moved is named `stale` and the page is left
    # exactly as the author wrote it. Under the page's `require_current` policy
    # it counts as a refusal, so the sweep does not answer clean over a page that
    # has drifted from the state it declares.
    assert result.status == "accepted"
    assert result.block_sync is not None
    assert [(item.outcome, item.reason) for item in result.block_sync.items] == [
        ("stale", "block_backing_changed")
    ], result.block_sync.items
    assert result.block_sync.items[0].reason == "block_backing_changed"
    assert result.block_sync.items[0].currency_policy == "require_current"
    assert result.block_sync.items[0].repair is not None
    assert result.block_sync.items[0].repair.operation == "playbill.block.repin"
    assert [request.stamps for request in checked] == [(stamp,)]
    assert result.block_sync.has_refusals is True
    assert result.block_sync.changed_file_count == 0
    assert source.read_bytes() == before


def test_workspace_status_compares_the_installed_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())
    handlers.handle_playbill_floor_export("inst_test", mode="write")

    status = handlers.handle_playbill_floor_export("inst_test", mode="status")

    assert status.status == "current"
    assert status.installed_coordinate == _coordinate()


def test_floor_write_after_an_activation_refresh_is_a_no_op_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())
    activated = handlers.handle_playbill_activate("inst_test", "proposal-1")
    assert activated.floor_refresh.status == "refreshed"
    card = workspace / ".playbill/floor/cards/fresh.json"
    before = card.stat().st_mtime_ns

    again = handlers.handle_playbill_floor_export("inst_test", mode="write")

    assert again.status == "unchanged"
    assert again.floor_digest == activated.floor_refresh.floor_digest
    assert card.stat().st_mtime_ns == before


def test_floor_write_still_refuses_a_directory_holding_something_else(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())
    floor = workspace / ".playbill/floor"
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
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    written = handlers.handle_playbill_floor_export("inst_test", mode="write")

    assert written.destination == str(workspace / ".playbill/floor")
    assert (workspace / ".playbill/floor/cards/fresh.json").is_file()
    assert not (nested / ".playbill/floor").exists()


def test_explicit_nested_mcp_root_refuses_to_write_outside_its_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    scoped_root = workspace / "scoped/subdir"
    scoped_root.mkdir(parents=True)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(scoped_root))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())

    with pytest.raises(ConfigError, match="must name the Git worktree root"):
        handlers.handle_playbill_floor_export("inst_test", mode="write")

    assert not (workspace / ".playbill/floor").exists()
    assert not (scoped_root / ".playbill/floor").exists()


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


def _export_files(files: dict[str, bytes]) -> contracts.PlaybillFloorExport:
    return floor_v5_export(files, coordinate=_coordinate())


class _PartsClient(_StubClient):
    """Exports the discovery cards only when asked for them."""

    def __init__(self) -> None:
        self.includes: list[tuple[str, ...]] = []

    def export_playbill_floor(  # type: ignore[override]
        self,
        instance_id: str,
        *,
        at=None,
        include=(),  # type: ignore[no-untyped-def]
    ) -> contracts.PlaybillFloorExport:
        self.includes.append(tuple(include))
        files = {"current/k/a.yaml": b"# k/a  kind=k\n"}
        if "discovery" in include:
            files["subjects/k/a.profile.json"] = b"{}\n"
        return _export_files(files)

    def playbill_floor_delta(  # type: ignore[override]
        self,
        instance_id: str,
        *,
        at=None,  # type: ignore[no-untyped-def]
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> PlaybillFloorDeltaV1:
        # The default floor travels as a delta, never with the discovery cards.
        self.includes.append(())
        return delta_from_export(_export_files({"current/k/a.yaml": b"# k/a  kind=k\n"}))


def test_an_mcp_write_with_discovery_survives_an_activation_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    client = _PartsClient()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    profile = workspace / ".playbill/floor/subjects/k/a.profile.json"

    written = handlers.handle_playbill_floor_export(
        "inst_test", mode="write", force=True, include=("discovery",)
    )

    assert written.status == "written"
    assert profile.is_file()
    config = json.loads((workspace / ".playbill/coverage.json").read_text(encoding="utf-8"))
    assert config["floor_output"] == {
        "tag": "playbill-floor-output-v1",
        "format": "playbill-floor-export-v5",
        "include": ["discovery"],
    }

    activated = handlers.handle_playbill_activate("inst_test", "proposal-1")

    assert activated.floor_refresh.status == "refreshed"
    assert client.includes == [("discovery",), ("discovery",)]
    assert profile.is_file()

    # Writing without the part records that too, and the next refresh follows it.
    handlers.handle_playbill_floor_export("inst_test", mode="write", force=True)
    handlers.handle_playbill_activate("inst_test", "proposal-2")
    assert client.includes[-2:] == [(), ()]
    assert not profile.exists()
