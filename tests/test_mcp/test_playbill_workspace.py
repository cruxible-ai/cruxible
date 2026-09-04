"""MCP owns local floor writes while the daemon remains filesystem-free."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    frame_projection_block,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.workspace import resolve_workspace_path


def _coordinate(seed: str = "1") -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid=seed * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export() -> contracts.PlaybillFloorExport:
    content = b'{"fresh":true}\n'
    inventory = [
        {
            "path": "cards/fresh.json",
            "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "byte_length": len(content),
        }
    ]
    manifest = {
        "tag": "playbill-floor-manifest-v2",
        "format": "playbill-floor-export-v2",
        "coordinate": _coordinate().model_dump(mode="json"),
        "files": inventory,
        "floor_digest": typed_digest(
            Sha256Value,
            "playbill-floor-export-v2",
            {"files": inventory},
        ).tagged,
    }
    return contracts.PlaybillFloorExport(
        coordinate=_coordinate(),
        manifest=manifest,
        files=[
            contracts.PlaybillFloorFile(
                path="manifest.json",
                content_base64=base64.b64encode(json.dumps(manifest).encode()).decode(),
            ),
            contracts.PlaybillFloorFile(
                path="cards/fresh.json",
                content_base64=base64.b64encode(content).decode(),
            ),
        ],
    )


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

    def search_playbill(self, instance_id: str, *, mode: str) -> contracts.PlaybillSearchResult:
        return contracts.PlaybillSearchResult(
            mode="orient",
            coordinate=_coordinate(),
            evaluation_time="2026-08-22T00:00:00Z",
            rows=[],
            orientation={},
            selection_basis_digest="sha256:" + "5" * 64,
            truncated=False,
            result_digest="sha256:" + "6" * 64,
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
    stamp = ProjectionBlockStampV1(
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

    def read_backing(
        instance_id: str,
        *,
        request: contracts.PlaybillBlockSyncReadRequestV1,
    ) -> contracts.PlaybillBlockSyncReadResultV1:
        assert instance_id == "inst_test"
        moved = ProjectionClaimBackingV1(
            identity=request.stamp.backing[0].identity,
            statement_digest="sha256:" + "a" * 64,
        )
        return contracts.PlaybillBlockSyncReadResultV1(
            status="successor",
            original_artifact_digest="sha256:" + "8" * 64,
            artifact_digest="sha256:" + "9" * 64,
            coordinate=AcceptedCoordinate.model_validate(_coordinate().model_dump()),
            generation=2,
            backing=moved,
            moved_backings=(moved,),
        )

    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_read_block_sync_backing",
        read_backing,
    )

    result = handlers.handle_playbill_activate("inst_test", "proposal-1")

    # The closing sweep an activation runs REPORTS: nothing renders a block, so
    # a block whose held backing moved is named `stale` and the page is left
    # exactly as the author wrote it. It counts as a refusal so the sweep does
    # not answer clean over a page that has drifted from the state it declares.
    assert result.status == "accepted"
    assert result.block_sync is not None
    assert [(item.outcome, item.reason) for item in result.block_sync.items] == [
        ("stale", "block_backing_changed")
    ], result.block_sync.items
    assert result.block_sync.items[0].reason == "block_backing_changed"
    assert result.block_sync.items[0].repair is not None
    assert result.block_sync.items[0].repair.operation == "playbill.block.repin"
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
    handlers.handle_playbill_workspace_floor_export("inst_test", force=False)

    status = handlers.handle_playbill_workspace_floor_status("inst_test")

    assert status.status == "current"
    assert status.installed_coordinate == _coordinate()


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

    written = handlers.handle_playbill_workspace_floor_export("inst_test", force=False)

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
        handlers.handle_playbill_workspace_floor_export("inst_test", force=False)

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
