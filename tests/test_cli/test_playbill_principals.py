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
            "--kind",
            "ordinary",
            "--key-dir",
            str(custody),
            "--name",
            "Add Reviewer",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert submitted[0]["principal_id"] == "reviewer"
    assert submitted[0]["kind"] == "ordinary"
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
            "--kind",
            "ordinary",
            "--key-dir",
            str(custody),
            "--name",
            "duplicate",
        ],
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert not custody.exists()


def test_cli_principal_add_refuses_daemon_kind() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "principal",
            "add",
            "bad",
            "--kind",
            "daemon",
            "--key-dir",
            "/outside/workspace",
            "--name",
            "bad",
        ],
    )

    assert result.exit_code != 0
    assert "'daemon' is not one of 'ordinary', 'recovery'" in result.output


def test_the_global_principal_id_reaches_the_client_and_whoami_says_it_is_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_ID", raising=False)
    constructed: list[dict[str, Any]] = []

    class StubClient:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
            return contracts.PlaybillWhoAmI(
                actor_id="alice",
                credential_label=None,
                actor_id_source="principal_claim",
                authenticated=False,
                credential_permission_mode="admin",
                principal_registration_status="active",
                active_principal_ids=["alice"],
                coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common.client_compatibility.check_daemon_compatibility",
        lambda _client: None,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(tmp_path / "d.sock"),
            "--instance-id",
            "inst_principals",
            "--principal-id",
            "alice",
            "playbill",
            "whoami",
        ],
    )

    assert result.exit_code == 0, result.output
    assert constructed[0]["principal_id"] == "alice"
    assert "configured principal ID (CRUXIBLE_PRINCIPAL_ID)" in result.output
    assert "Identity is a claim, not authentication" in result.output
