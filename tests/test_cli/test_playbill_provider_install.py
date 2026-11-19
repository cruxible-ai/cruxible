"""CLI provider install carries change control and the control domain to the request."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.provider_installation import ProviderInstallResult
from cruxible_core.cli.main import cli


@pytest.fixture
def provider_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    requests = []

    def install_provider(instance_id, request):
        requests.append(request)
        return ProviderInstallResult(
            installation_id="inst-1",
            provider_id="demo",
            status="awaiting_approval",
            installed=True,
            registered=False,
            proposal_id="sha256:" + "c" * 64,
        )

    client = SimpleNamespace(install_provider=install_provider)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    args = ["--server-url", "http://unused.invalid", "--instance-id", "inst_test", "provider"]
    return args, requests


def test_install_by_name_passes_at_and_control_domain(provider_cli):
    args, requests = provider_cli
    result = CliRunner().invoke(
        cli,
        [*args, "install", "demo==1.0", "--control-domain", "lab", "--commit", "--at", "a" * 40],
    )
    assert result.exit_code == 0, result.output
    (request,) = requests
    assert (request.package, request.version) == ("demo", "1.0")
    assert request.control_domain == "lab"
    assert (request.dry_run, request.at) == (False, "a" * 40)
    assert "demo: awaiting_approval" in result.stdout


def test_install_help_states_it_lands_when_policy_allows(provider_cli):
    args, _ = provider_cli
    result = CliRunner().invoke(cli, [*args, "install", "--help"])
    assert result.exit_code == 0, result.output
    assert "lands at once when the" in result.output
    assert "--control-domain" in result.output
    assert "--at OID" in result.output
