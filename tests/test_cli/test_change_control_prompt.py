"""The shared commit prompt: an interactive preview asks once, then commits its exact pin."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.get_reads import GetCoordinate
from cruxible_client.contracts.kits import KitChangeResult, KitRemoveRequest
from cruxible_core.cli.main import cli

BASE = ["--server-url", "https://kits.example.test", "--instance-id", "inst", "kit"]
PIN = "a" * 12


class _StubClient:
    def __init__(self) -> None:
        self.sent: list[KitRemoveRequest] = []

    def remove_kit(self, instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
        self.sent.append(request)
        committing = request.dry_run is False
        return KitChangeResult(
            kit_id="acme",
            version=None,
            status="accepted" if committing else "would_propose",
            coordinate=GetCoordinate(git_oid=PIN, generation=4),
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    return client


def _terminal(monkeypatch: pytest.MonkeyPatch, interactive: bool) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._interactive_terminal", lambda: interactive
    )


def test_interactive_yes_commits_against_the_previews_pin(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, True)
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "acme: would_propose" in result.output
    assert "Commit this change? [y/N]" in result.output
    assert "--commit --at" not in result.output
    assert "acme: accepted" in result.output
    assert [(item.dry_run, item.at) for item in stub.sent] == [(None, None), (False, PIN)]


def test_interactive_no_commits_nothing_and_names_the_commit(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, True)
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme"], input="n\n")
    assert result.exit_code == 0, result.output
    assert f"Not committed. Commit it later: rerun with --commit --at {PIN}" in result.output
    assert len(stub.sent) == 1


def test_yes_commits_without_asking_even_off_a_terminal(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, False)
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Commit this change?" not in result.output
    assert [(item.dry_run, item.at) for item in stub.sent] == [(None, None), (False, PIN)]


@pytest.mark.parametrize(
    ("interactive", "extra"),
    [(False, []), (True, ["--json"]), (True, ["--dry-run"])],
    ids=["not-a-terminal", "json", "explicit-dry-run"],
)
def test_agent_forms_are_unchanged(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch, interactive: bool, extra: list[str]
) -> None:
    _terminal(monkeypatch, interactive)
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme", *extra])
    assert result.exit_code == 0, result.output
    assert "Commit this change?" not in result.output
    assert len(stub.sent) == 1
    if "--json" not in extra:
        assert f"Commit it: rerun the same command with --commit --at {PIN}" in result.output


@pytest.mark.parametrize("extra", [["--json"], ["--commit"]], ids=["json", "commit"])
def test_yes_refuses_forms_it_cannot_honor(
    stub: _StubClient, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    _terminal(monkeypatch, True)
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme", "--yes", *extra])
    assert result.exit_code == 2
    assert stub.sent == []


def test_every_change_controlled_command_takes_yes() -> None:
    for path in (["kit", "add"], ["kit", "remove"], ["ledger", "set-mirror"], ["block", "detach"]):
        result = CliRunner().invoke(cli, [*path, "--help"])
        assert result.exit_code == 0, result.output
        assert "--yes" in result.output


@pytest.mark.parametrize(
    "path",
    [
        ["document", "propose", "--envelope", "-", "--name", "doc"],
        ["principal", "rotate", "owner", "--key-dir", "keys", "--name", "rotate"],
        ["principal", "recover", "owner", "--key-dir", "keys", "--name", "recover"],
        ["principal", "revoke", "owner", "--name", "revoke"],
        ["compiler", "upgrade", "--to", "sha256:" + "a" * 64, "--name", "upgrade"],
    ],
)
def test_proposal_commands_select_json_or_text_and_preserve_prompt_control(
    monkeypatch: pytest.MonkeyPatch, path: list[str]
) -> None:
    import importlib
    import json

    from cruxible_client import contracts
    from cruxible_client.authoring.examples import document_example

    commands = importlib.import_module("cruxible_core.cli.commands.playbill")
    result = contracts.ProposalInspection(
        status="would_propose",
        proposal={"evaluation": {"verdict": "passed"}},
        accepted_coordinate=contracts.AcceptedCoordinate(
            git_oid="a" * 40,
            semantic_root="sha256:" + "b" * 64,
            generation_root="sha256:" + "c" * 64,
            compiler_digest="sha256:" + "d" * 64,
        ),
    )
    monkeypatch.setattr(commands, "_server_call", lambda *_a, **_kw: result)
    monkeypatch.setattr(commands, "_principal_successor", lambda **_kw: result)
    monkeypatch.setattr(commands, "_read_model", lambda *_a: document_example())
    _terminal(monkeypatch, True)

    rendered = CliRunner().invoke(cli, path, input="n\n")
    assert rendered.exit_code == 0, rendered.output
    assert "would_propose" in rendered.stdout
    assert "Commit this change?" in rendered.stdout

    machine = CliRunner().invoke(cli, [*path, "--json"])
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout)["status"] == "would_propose"
    assert "Commit this change?" not in machine.stdout
