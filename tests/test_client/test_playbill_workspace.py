"""Shared client-owned workspace and floor mechanics."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.workspace import (
    PlaybillWorkspaceError,
    activate_with_workspace_refresh,
    inspect_workspace_floor,
    materialize_playbill_floor,
    observe_playbill_next_workspace,
    record_playbill_floor_output,
    write_playbill_workspace_config,
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


def test_workspace_config_writer_refuses_differences_and_never_carries_secrets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    written = write_playbill_workspace_config(
        workspace,
        instance_id="inst_one",
        server_url="https://one.example.test",
    )
    original = written.read_bytes()

    with pytest.raises(PlaybillWorkspaceError, match="--replace"):
        write_playbill_workspace_config(
            workspace,
            instance_id="inst_two",
            server_url="https://two.example.test",
        )
    assert written.read_bytes() == original

    written.write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "server_url": "https://one.example.test",
                "instance_id": "inst_one",
                "bearer_token": "must-not-survive",
            }
        ),
        encoding="utf-8",
    )
    write_playbill_workspace_config(
        workspace,
        instance_id="inst_two",
        server_socket=str(tmp_path / "daemon.sock"),
        replace=True,
    )
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload == {
        "floor_output": {
            "format": "playbill-floor-export-v2",
            "tag": "playbill-floor-output-v1",
        },
        "instance_id": "inst_two",
        "server_socket": str(tmp_path / "daemon.sock"),
        "tag": "playbill-coverage-workspace-config-v2",
    }
    assert "must-not-survive" not in written.read_text(encoding="utf-8")


def test_workspace_config_writer_refuses_credentials_embedded_in_url(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(PlaybillWorkspaceError, match="CRUXIBLE_SERVER_BEARER_TOKEN"):
        write_playbill_workspace_config(
            workspace,
            instance_id="inst_secret",
            server_url="https://agent:secret@example.test",
        )

    assert not (workspace / ".playbill" / "coverage.json").exists()


def test_workspace_config_writer_adds_machine_local_git_exclusion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)

    write_playbill_workspace_config(
        workspace,
        instance_id="inst_local",
        server_url="https://playbill.example.test",
    )

    excluded = subprocess.run(
        ["git", "-C", str(workspace), "check-ignore", "--quiet", ".playbill/coverage.json"],
        check=False,
    )
    assert excluded.returncode == 0
    assert b"/.playbill/coverage.json\n" in (workspace / ".git/info/exclude").read_bytes()


def test_floor_output_writer_upgrades_and_preserves_safe_coverage_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_path = workspace / ".playbill" / "coverage.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v1",
                "server_url": "https://playbill.example.test",
                "instance_id": "inst_floor",
                "rules": [],
            }
        ),
        encoding="utf-8",
    )

    record_playbill_floor_output(
        workspace,
        instance_id="inst_floor",
        server_url="https://playbill.example.test",
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["tag"] == "playbill-coverage-workspace-config-v2"
    assert payload["rules"] == []
    assert payload["floor_output"] == {
        "tag": "playbill-floor-output-v1",
        "format": "playbill-floor-export-v2",
    }


def test_replace_upgrades_v1_config_without_dropping_coverage_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_path = workspace / ".playbill" / "coverage.json"
    config_path.parent.mkdir(parents=True)
    preserved = {
        "root": "docs",
        "scan_budget": {"max_scanned_bytes": 4096},
        "max_observed_paths": 128,
        "rules": [
            {
                "tag": "coverage-path-prefix-rule-v1",
                "path_prefix": "docs/",
                "source": {
                    "tag": "logical-source-identity-v1",
                    "source_id": "docs",
                    "revision": 1,
                },
            }
        ],
    }
    config_path.write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v1",
                "instance_id": "inst_old",
                "server_url": "https://old.example.test",
                **preserved,
            }
        ),
        encoding="utf-8",
    )

    write_playbill_workspace_config(
        workspace,
        instance_id="inst_new",
        server_socket=str(tmp_path / "daemon.sock"),
        replace=True,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["tag"] == "playbill-coverage-workspace-config-v2"
    assert payload["instance_id"] == "inst_new"
    assert payload["server_socket"] == str(tmp_path / "daemon.sock")
    assert "server_url" not in payload
    assert {key: payload[key] for key in preserved} == preserved


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


def test_accepted_activation_skips_sync_for_an_unattached_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "plain-checkout"
    workspace.mkdir()

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

    result = activate_with_workspace_refresh(
        StubClient(),  # type: ignore[arg-type]
        "inst_test",
        "proposal-1",
        workspace=workspace,
    )

    assert result.status == "accepted"
    assert result.block_sync is not None
    assert result.block_sync.has_refusals is False
    (item,) = result.block_sync.items
    assert item.outcome == "skipped"
    assert item.reason == "workspace_not_attached"
    assert item.repair_commands == ("cruxible playbill host create --workspace .",)
