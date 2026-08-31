"""Client-owned floor refresh at the tail of proposal activation."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Literal

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.errors import ProposalActivationRequestInvalid
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import cli


def _coordinate() -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export(*, corrupt: bool = False) -> contracts.PlaybillFloorExport:
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
                content_base64=base64.b64encode(b"corrupt" if corrupt else content).decode(),
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


def _install_client(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    status: Literal["accepted", "lost_cas"] = "accepted",
    corrupt: bool = False,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(CliContextState(server_url="http://test", instance_id="inst_test"))

    class StubClient:
        def activate_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            assert (instance_id, proposal_id) == ("inst_test", "proposal-1")
            return contracts.PlaybillActivationReceipt(
                proposal_id=proposal_id,
                activated_by="owner",
                status=status,
                accepted_coordinate=_coordinate() if status == "accepted" else None,
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
            )

        def export_playbill_floor(
            self,
            instance_id: str,
            *,
            at=None,  # type: ignore[no-untyped-def]
        ) -> contracts.PlaybillFloorExport:
            assert instance_id == "inst_test"
            assert at is None
            return _export(corrupt=corrupt)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())


def test_accepted_activation_exactly_replaces_the_declared_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    old = workspace / ".playbill/floor"
    old.mkdir()
    (old / "retired-card.json").write_text("stale", encoding="utf-8")
    _install_client(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "proposal",
            "activate",
            "proposal-1",
            "--workspace-root",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not (old / "retired-card.json").exists()
    assert (old / "cards/fresh.json").read_bytes() == b'{"fresh":true}\n'
    payload = json.loads(result.stdout)
    assert payload["status"] == "accepted"
    assert payload["floor_refresh"]["status"] == "refreshed"


def test_invalid_refresh_preserves_the_old_floor_and_reports_both_truths(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    old = workspace / ".playbill/floor"
    old.mkdir()
    (old / "keep.json").write_text("old", encoding="utf-8")
    _install_client(monkeypatch, tmp_path, corrupt=True)

    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "proposal",
            "activate",
            "proposal-1",
            "--workspace-root",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert (old / "keep.json").read_text(encoding="utf-8") == "old"
    assert not (old / "cards/fresh.json").exists()
    payload = json.loads(result.stdout)
    assert payload["status"] == "accepted"
    assert payload["floor_refresh"]["status"] == "failed"
    assert "floor refresh failed" in result.stderr


def test_lost_cas_retry_safely_refreshes_the_current_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _install_client(monkeypatch, tmp_path, status="lost_cas")

    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "proposal",
            "activate",
            "proposal-1",
            "--workspace-root",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "lost_cas"
    assert payload["floor_refresh"]["status"] == "refreshed"
    assert (workspace / ".playbill/floor/cards/fresh.json").exists()


def test_activation_renders_malformed_proposal_id_as_typed_refusal(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(CliContextState(server_url="http://test", instance_id="inst_test"))

    class StubClient:
        def activate_playbill_proposal(
            self, _instance_id: str, _proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            raise ProposalActivationRequestInvalid(
                "playbill.proposal.activation_request_invalid: proposal_id must be a "
                "canonical sha256 digest"
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(
        cli,
        ["playbill", "proposal", "activate", "bogus-no-prefix", "--json"],
    )

    assert result.exit_code == 1
    assert "ProposalActivationRequestInvalid" in result.output
    assert "playbill.proposal.activation_request_invalid" in result.output


def test_floor_symlink_may_not_escape_the_workspace(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.json").write_text("outside", encoding="utf-8")
    (workspace / ".playbill/floor").symlink_to(outside, target_is_directory=True)
    _install_client(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "proposal",
            "activate",
            "proposal-1",
            "--workspace-root",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert (outside / "keep.json").read_text(encoding="utf-8") == "outside"
    assert not (outside / "cards/fresh.json").exists()
