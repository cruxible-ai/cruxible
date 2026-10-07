"""Activation writes no floor; floor export is the client-side pull."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.authoring.workspace import observe_next_workspace
from cruxible_client.contracts.errors import ProposalActivationRequestInvalid
from cruxible_client.contracts.floor import FloorDelta
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import cli
from tests.support.floor_exports import delta_from_export, floor_v5_export


def _coordinate() -> contracts.AcceptedCoordinate:
    return contracts.AcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def _export() -> contracts.FloorExport:
    return floor_v5_export({"cards/fresh.json": b'{"fresh":true}\n'}, coordinate=_coordinate())


def _delta(*, corrupt: bool = False) -> FloorDelta:
    return delta_from_export(_export(), corrupt="cards/fresh.json" if corrupt else None)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / ".cruxible/coverage.json").write_text(
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
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            assert instance_id == "inst_test"
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def activate_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.ActivationReceipt:
            assert (instance_id, proposal_id) == ("inst_test", "proposal-1")
            return contracts.ActivationReceipt(
                proposal_id=proposal_id,
                activated_by="owner",
                status=status,
                accepted_coordinate=_coordinate() if status == "accepted" else None,
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
            )

        def floor_delta(
            self,
            instance_id: str,
            *,
            at=None,  # type: ignore[no-untyped-def]
            base_generation: int | None = None,
            base_renderer: str | None = None,
        ) -> FloorDelta:
            assert instance_id == "inst_test"
            assert at == (_coordinate() if status == "accepted" else None)
            return _delta(corrupt=corrupt)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())


def test_floor_export_records_missing_config_and_clears_floor_missing(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace)],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(CliContextState(server_url="http://test", instance_id="inst_test"))

    class StubClient:
        socket_path = None

        def host_workspace_registration(
            self, instance_id: str, *, workspace_root: str | None = None
        ) -> contracts.HostWorkspaceRegistration:
            # A TCP export asks whether the daemon delivers this floor first.
            assert workspace_root is not None
            return contracts.HostWorkspaceRegistration(
                instance_id=instance_id, status="not_registered", delivers_here=False
            )

        def floor_delta(
            self,
            instance_id: str,
            *,
            at=None,  # type: ignore[no-untyped-def]
            base_generation: int | None = None,
            base_renderer: str | None = None,
        ) -> FloorDelta:
            assert instance_id == "inst_test"
            assert at is None
            return _delta()

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(cli, ["floor", "export", "--json"])

    assert result.exit_code == 0, result.output
    config = json.loads((workspace / ".cruxible" / "coverage.json").read_text())
    assert config["floor_output"]["format"] == "playbill-floor-export-v6"
    observation = observe_next_workspace(workspace)
    assert observation["floor_status"] != "missing"
    assert observation["floor_status"] != "not_configured"
    assert observation["installed_coordinate"] == _coordinate().model_dump(mode="json")


def test_activation_is_a_daemon_act_that_writes_no_floor(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """The daemon's floor-refresh trigger delivers the floor; activate writes nothing."""

    workspace = _workspace(tmp_path)
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))
    _install_client(monkeypatch, tmp_path)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["proposal", "activate", "proposal-1", "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["tag"] == "playbill-activation-receipt-v1"
    assert receipt["status"] == "accepted"
    assert "floor_refresh" not in receipt
    assert sorted(path.relative_to(workspace) for path in workspace.rglob("*")) == before
    removed = CliRunner().invoke(
        cli, ["proposal", "activate", "proposal-1", "--workspace-root", str(workspace)]
    )
    assert removed.exit_code == 2 and "No such option: --workspace-root" in removed.output


def test_activation_renders_malformed_proposal_id_as_typed_refusal(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(CliContextState(server_url="http://test", instance_id="inst_test"))

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def activate_proposal(
            self, _instance_id: str, _proposal_id: str
        ) -> contracts.ActivationReceipt:
            raise ProposalActivationRequestInvalid(
                "cruxible.proposal.activation_request_invalid: proposal_id must be a "
                "canonical sha256 digest"
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(
        cli,
        ["proposal", "activate", "bogus-no-prefix", "--json"],
    )

    assert result.exit_code == 1
    assert "ProposalActivationRequestInvalid" in result.output
    assert "cruxible.proposal.activation_request_invalid" in result.output
