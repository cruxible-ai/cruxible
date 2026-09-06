"""CLI publication status distinguishes an acknowledgment from newer pending work."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


@pytest.fixture
def mirror_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    receipt = contracts.PlaybillLedgerMirrorV1(
        instance_id="inst_test",
        mirror_url="/tmp/unused.git",
        status="pending",
        requested_sequence=3,
        published_sequence=2,
        wait_sequence=2,
    )
    calls = []
    client = SimpleNamespace(
        publish_playbill_ledger=lambda instance_id, **kwargs: (
            calls.append((instance_id, kwargs)) or receipt
        ),
        get_playbill_ledger_mirror=lambda _: receipt,
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    args = [
        "--server-url",
        "http://unused.invalid",
        "--instance-id",
        "inst_test",
        "playbill",
        "ledger",
    ]
    return args, calls


def test_cli_publish_reports_own_barrier_even_when_newer_work_pending(mirror_cli):
    args, calls = mirror_cli
    result = CliRunner().invoke(cli, [*args, "publish", "--timeout", "0"])
    assert result.exit_code == 0, result.output
    assert calls == [("inst_test", {"timeout": 0})]
    assert "Publication: pending" in result.stdout
    assert "Request 2: acknowledged" in result.stdout


def test_cli_publish_json_and_timeout_validation(mirror_cli):
    args, calls = mirror_cli
    result = CliRunner().invoke(cli, [*args, "publish", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["wait_sequence"] == 2
    assert calls == [("inst_test", {"timeout": 60})]
    refused = CliRunner().invoke(cli, [*args, "publish", "--timeout", "61"])
    assert refused.exit_code == 2
    assert len(calls) == 1


def test_cli_clone_url_keeps_stdout_pipeable_and_status_visible(mirror_cli):
    args, _ = mirror_cli
    result = CliRunner().invoke(cli, [*args, "clone-url"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "/tmp/unused.git\n"
    assert "Publication: pending" in result.stderr
    assert "acknowledged request 2, latest requested 3" in result.stderr
