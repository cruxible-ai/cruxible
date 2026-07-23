"""Trust tests for mutating-command target visibility."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import MUTATING_COMMAND_TARGETS, cli

EXPECTED_MUTATING_COMMAND_TARGETS = {
    ("init",): "create",
    ("lock",): "lock",
    ("run",): "active",
    ("apply",): "active",
    ("propose",): "active",
    ("snapshot", "create"): "active",
    ("clone",): "active",
    ("source", "register"): "active",
    ("state", "publish"): "active",
    ("state", "create-overlay"): "create",
    ("state", "pull-apply"): "active",
    ("instance", "backup"): "active",
    ("instance", "restore"): "create",
    ("instance", "relocate"): "active",
    ("credential", "claim-bootstrap"): "active",
    ("credential", "mint"): "active",
    ("credential", "recover-admin"): "manual",
    ("credential", "revoke"): "active",
    ("credential", "rotate"): "active",
    ("decision-record", "create"): "active",
    ("decision-record", "finalize"): "active",
    ("decision-record", "abandon"): "active",
    ("config", "reload"): "active",
    ("config", "add-constraint"): "active",
    ("config", "add-decision-policy"): "active",
    ("feedback", "record"): "active",
    ("feedback", "from-query"): "active",
    ("feedback", "batch"): "active",
    ("outcome", "record"): "active",
    ("entity", "add"): "active",
    ("entity", "update"): "active",
    ("relationship", "add"): "active",
    ("relationship", "update"): "active",
    ("batch-direct-write",): "active",
    ("group", "propose"): "active",
    ("group", "resolve"): "active",
    ("group", "trust"): "active",
    ("procedure", "propose"): "active",
    ("procedure", "resolve"): "active",
    ("procedure", "retire"): "active",
    ("procedure", "run"): "active",
}


class _BatchWriteClient:
    def batch_direct_write(self, instance_id, payload, *, dry_run=False):
        return contracts.BatchDirectWriteResult(
            dry_run=dry_run,
            valid=True,
            entities_added=len(payload.entities),
            relationships_added=len(payload.relationships),
            receipt_id="RCP-target",
        )

    def schema(self, instance_id):
        return {"entity_types": {}, "relationships": [], "named_queries": {}}


def _write_payload(path: Path) -> None:
    path.write_text("entities: []\nrelationships: []\nshared_evidence: {}\n")


def _command_at_path(path: tuple[str, ...]) -> click.Command:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


def test_mutating_command_inventory_is_authoritative_and_registered() -> None:
    assert MUTATING_COMMAND_TARGETS == EXPECTED_MUTATING_COMMAND_TARGETS
    for path in MUTATING_COMMAND_TARGETS:
        command = _command_at_path(path)
        assert command.callback is not None, path


def test_explicit_target_notice_is_stderr_only_for_json_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload_file = tmp_path / "batch.yaml"
    _write_payload(payload_file)
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: _BatchWriteClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://explicit.example.test",
            "--instance-id",
            "inst_explicit",
            "batch-direct-write",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["receipt_id"] == "RCP-target"
    assert result.stderr == ("target: inst_explicit @ https://explicit.example.test (explicit)\n")


def test_remembered_target_notice_marks_both_context_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload_file = tmp_path / "batch.yaml"
    _write_payload(payload_file)
    save_cli_context(
        CliContextState(
            server_url="https://remembered.example.test",
            instance_id="inst_remembered",
        )
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: _BatchWriteClient(),
    )

    result = CliRunner().invoke(
        cli,
        ["batch-direct-write", "--payload-file", str(payload_file), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["receipt_id"] == "RCP-target"
    assert result.stderr == (
        "target: inst_remembered @ https://remembered.example.test (remembered)\n"
    )


def test_mixed_target_notice_names_component_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload_file = tmp_path / "batch.yaml"
    _write_payload(payload_file)
    save_cli_context(
        CliContextState(
            server_url="https://old.example.test",
            instance_id="inst_remembered",
        )
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: _BatchWriteClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://explicit.example.test",
            "batch-direct-write",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "target: inst_remembered @ https://explicit.example.test "
        "(instance=remembered, transport=explicit)\n"
    )


def test_local_write_notice_uses_discovered_instance_root(
    monkeypatch: pytest.MonkeyPatch,
    initialized_project: CruxibleInstance,
) -> None:
    monkeypatch.chdir(initialized_project.root)
    result = CliRunner().invoke(
        cli,
        ["decision-record", "create", "--question", "Which target?", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["record"]["question"] == "Which target?"
    assert result.stderr == (f"target: local @ {initialized_project.root.resolve()} (discovered)\n")


def test_read_command_remains_silent_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: _BatchWriteClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://explicit.example.test",
            "--instance-id",
            "inst_explicit",
            "schema",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["entity_types"] == {}
    assert result.stderr == ""
