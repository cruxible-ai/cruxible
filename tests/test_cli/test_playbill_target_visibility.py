"""Target-visibility guardrails for surviving Playbill and credential writes."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import MUTATING_COMMAND_TARGETS, cli

EXPECTED_MUTATING_COMMAND_TARGETS = {
    ("playbill", "host", "create"): "create",
    ("playbill", "init"): "active",
    ("playbill", "body", "store"): "active",
    ("playbill", "document", "propose"): "active",
    ("playbill", "subject", "propose"): "active",
    ("playbill", "claim-type", "propose"): "active",
    ("playbill", "claim", "propose"): "active",
    ("playbill", "query", "propose"): "active",
    ("playbill", "proposal", "approve"): "active",
    ("playbill", "proposal", "activate"): "active",
    ("playbill", "sources", "propose"): "active",
    ("playbill", "principal", "rotate"): "active",
    ("playbill", "principal", "recover"): "active",
    ("playbill", "principal", "revoke"): "active",
    ("credential", "claim-bootstrap"): "active",
    ("credential", "mint"): "active",
    ("credential", "recover-admin"): "manual",
    ("credential", "revoke"): "active",
    ("credential", "rotate"): "active",
}


def _command_at_path(path: tuple[str, ...]) -> click.Command:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


def test_mutating_command_inventory_is_exact_and_registered() -> None:
    assert MUTATING_COMMAND_TARGETS == EXPECTED_MUTATING_COMMAND_TARGETS
    for path in MUTATING_COMMAND_TARGETS:
        assert _command_at_path(path).callback is not None, path


def test_explicit_playbill_write_names_instance_transport_and_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = tmp_path / "body.md"
    body.write_text("# governed\n")

    class StubClient:
        def store_playbill_body(
            self, instance_id: str, content: bytes
        ) -> contracts.PlaybillCasObjectResult:
            assert instance_id == "inst_explicit"
            assert content == b"# governed\n"
            return contracts.PlaybillCasObjectResult(
                digest="sha256:" + "1" * 64,
                present=True,
                byte_length=len(content),
                redacted=False,
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://explicit.example.test",
            "--instance-id",
            "inst_explicit",
            "playbill",
            "body",
            "store",
            str(body),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ("target: inst_explicit @ https://explicit.example.test (explicit)\n")


def test_remembered_playbill_write_marks_remembered_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    save_cli_context(
        CliContextState(
            server_url="https://remembered.example.test",
            instance_id="inst_remembered",
        )
    )

    class StubClient:
        def activate_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            assert (instance_id, proposal_id) == ("inst_remembered", "proposal-1")
            return contracts.PlaybillActivationReceipt(
                proposal_id=proposal_id,
                status="lost_cas",
                accepted_coordinate=None,
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        ["playbill", "proposal", "activate", "proposal-1", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "target: inst_remembered @ https://remembered.example.test (remembered)\n"
    )


def test_host_creation_names_explicit_requested_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))

    class StubClient:
        def create_playbill_host(
            self, *, instance_id: str | None = None
        ) -> contracts.PlaybillHostResult:
            assert instance_id == "inst_requested"
            return contracts.PlaybillHostResult(instance_id=instance_id, status="created")

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://host.example.test",
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_requested",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "target: inst_requested @ https://host.example.test (transport=explicit)\n"
    )
