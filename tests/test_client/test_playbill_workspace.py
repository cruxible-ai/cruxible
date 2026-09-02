"""Shared client-owned workspace and floor mechanics."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.workspace import (
    PlaybillWorkspaceError,
    activate_with_workspace_refresh,
    inspect_workspace_floor,
    materialize_playbill_floor,
    observe_playbill_next_workspace,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest


def _coordinate(seed: str = "1") -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid=seed * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export(*, content: bytes = b'{"fresh":true}\n') -> contracts.PlaybillFloorExport:
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


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / ".playbill/coverage.json").write_text(
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
    return workspace


def test_materialization_exactly_replaces_and_reports_current(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    destination = workspace / ".playbill/floor"
    destination.mkdir()
    (destination / "stale.json").write_text("old", encoding="utf-8")

    result = materialize_playbill_floor(
        workspace,
        export=_export(),
    )
    status = inspect_workspace_floor(workspace, current_coordinate=_coordinate())

    assert result.floor_digest.startswith("sha256:")
    assert not (destination / "stale.json").exists()
    assert (destination / "cards/fresh.json").read_bytes() == b'{"fresh":true}\n'
    assert status.status == "current"
    assert status.installed_coordinate == _coordinate()
    observation = observe_playbill_next_workspace(workspace)
    assert observation["installed_coordinate"] == _coordinate().model_dump(mode="json")
    assert observation["drift_observations"] is None


def test_materialization_refuses_symlink_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".playbill/floor").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PlaybillWorkspaceError, match="escapes"):
        materialize_playbill_floor(
            workspace,
            export=_export(),
        )


def test_config_refuses_the_obsolete_arbitrary_output_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    config_path = workspace / ".playbill/coverage.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["floor_output"]["path"] = "other-floor"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    status = inspect_workspace_floor(workspace, current_coordinate=_coordinate())

    assert status.status == "invalid"
    assert "path is obsolete" in (status.message or "")


@pytest.mark.parametrize(
    "exported_path",
    [
        "../outside.json",
        "/" + "tmp/outside.json",
        "cards/../outside.json",
        "cards//outside.json",
    ],
)
def test_materialization_refuses_export_file_escape_forms(
    tmp_path: Path,
    exported_path: str,
) -> None:
    workspace = _workspace(tmp_path)
    export = _export()
    malicious_export = export.model_copy(
        update={
            "files": [
                export.files[0],
                export.files[1].model_copy(update={"path": exported_path}),
            ]
        }
    )

    with pytest.raises(PlaybillWorkspaceError, match="escapes its root"):
        materialize_playbill_floor(
            workspace,
            export=malicious_export,
        )


def test_activate_reports_accepted_and_refresh_failure(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    class StubClient:
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
            export = _export()
            return export.model_copy(
                update={
                    "files": [
                        export.files[0],
                        export.files[1].model_copy(
                            update={"content_base64": base64.b64encode(b"tampered").decode()}
                        ),
                    ]
                }
            )

    result = activate_with_workspace_refresh(
        StubClient(),
        "inst_test",
        "proposal-1",
        workspace=workspace,
    )

    assert result.status == "accepted"
    assert result.floor_refresh.status == "failed"
    assert "differs" in (result.floor_refresh.message or "")


def test_accepted_activation_runs_workspace_sync_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    events: list[str] = []

    class StubClient:
        def activate_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            events.append("activate")
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
            events.append("floor")
            return _export()

    def sync(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        events.append("sync")
        return contracts.PlaybillBlockSyncResultV1(
            items=(), changed_file_count=0, would_change=False, has_refusals=False
        )

    monkeypatch.setattr(
        "cruxible_client.authoring.workspace.sync_projection_blocks",
        sync,
    )

    result = activate_with_workspace_refresh(
        StubClient(),
        "inst_test",
        "proposal-1",
        workspace=workspace,
    )

    assert events == ["activate", "floor", "sync"]
    assert result.block_sync is not None
    assert result.block_sync.has_refusals is False
