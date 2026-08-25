"""Owner-governed principal onboarding never exports private key custody."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_principal_add_keeps_private_key_client_side_and_proposes_public_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    custody = tmp_path / "reviewer-custody"
    submitted: list[dict[str, Any]] = []

    class StubClient:
        def list_playbill_principals(self, instance_id: str) -> contracts.PlaybillPrincipalList:
            assert instance_id == "inst_principals"
            return contracts.PlaybillPrincipalList(coordinate=COORDINATE, principals=[])

        def propose_playbill_principal_change(
            self,
            instance_id: str,
            *,
            principal: dict[str, Any],
            proposal_name: str,
        ) -> contracts.PlaybillProposalInspection:
            assert (instance_id, proposal_name) == ("inst_principals", "add-reviewer")
            submitted.append(principal)
            return contracts.PlaybillProposalInspection(
                proposal={"proposal_id": "sha256:" + "5" * 64},
                accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://principals.example.test",
            "--instance-id",
            "inst_principals",
            "playbill",
            "principal",
            "add",
            "reviewer",
            "--role",
            "reviewer",
            "--key-dir",
            str(custody),
            "--name",
            "add-reviewer",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert submitted[0]["principal_id"] == "reviewer"
    assert submitted[0]["authority_roles"] == ["reviewer"]
    assert "private" not in json.dumps(submitted[0])
    assert "PRIVATE KEY" not in result.output
    private_key = custody / "reviewer.ed25519"
    assert private_key.is_file()
    assert stat.S_IMODE(private_key.stat().st_mode) == 0o600
    assert (custody / "reviewer.ed25519.pub").is_file()
    assert result.stderr == (
        "target: inst_principals @ https://principals.example.test (explicit)\n"
    )


def test_cli_principal_add_rejects_existing_identity_before_generating_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    custody = tmp_path / "reviewer-custody"

    class StubClient:
        def list_playbill_principals(self, instance_id: str) -> contracts.PlaybillPrincipalList:
            return contracts.PlaybillPrincipalList(
                coordinate=COORDINATE,
                principals=[{"principal_id": "reviewer"}],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://principals.example.test",
            "--instance-id",
            "inst_principals",
            "playbill",
            "principal",
            "add",
            "reviewer",
            "--role",
            "reviewer",
            "--key-dir",
            str(custody),
            "--name",
            "duplicate",
        ],
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert not custody.exists()


def test_cli_principal_add_refuses_daemon_authority() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "principal",
            "add",
            "bad",
            "--role",
            "daemon",
            "--key-dir",
            "/outside/workspace",
            "--name",
            "bad",
        ],
    )

    assert result.exit_code != 0
    assert "'daemon' is not one of 'owner', 'reviewer', 'recovery'" in result.output
