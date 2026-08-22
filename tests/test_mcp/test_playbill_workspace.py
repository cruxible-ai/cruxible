"""MCP owns local floor writes while the daemon remains filesystem-free."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.playbill.canonical import Sha256Value, typed_digest


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
            status="accepted",
            accepted_coordinate=_coordinate(),
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
    (root / ".playbill").mkdir(parents=True)
    (root / ".playbill/coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "path": "playbill-floor",
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
    assert (workspace / "playbill-floor/cards/fresh.json").is_file()


def test_workspace_status_compares_the_installed_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(handlers, "_get_client", lambda: _StubClient())
    handlers.handle_playbill_workspace_floor_export("inst_test", "playbill-floor", force=False)

    status = handlers.handle_playbill_workspace_floor_status("inst_test")

    assert status.status == "current"
    assert status.installed_coordinate == _coordinate()
