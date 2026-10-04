"""Local workspace controls select the daemon's single floor writer."""

import json
import subprocess

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


@pytest.mark.parametrize("command", ["attach", "attach-off", "on", "off"])
def test_local_workspace_delivery_controls(tmp_path, monkeypatch, command):
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    monkeypatch.chdir(workspace)
    calls = []

    class Client:
        def host_workspace_registration(self, instance_id):
            return contracts.HostWorkspaceRegistration(
                instance_id=instance_id,
                status="registered",
                workspace_path=str(workspace.resolve()),
            )

        def set_floor_delivery(self, instance_id, *, enabled):
            calls.append((instance_id, enabled))
            return self.host_workspace_registration(instance_id).model_copy(
                update={"floor_delivery": enabled}
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: Client())
    arguments = (
        ["attach", *(["--no-floor-delivery"] if command == "attach-off" else [])]
        if command.startswith("attach")
        else ["floor-delivery", command]
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(tmp_path / "socket"),
            "playbill",
            "workspace",
            *arguments,
            "--instance-id",
            "inst_floor",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [("inst_floor", command not in {"off", "attach-off"})]
    payload = json.loads(result.stdout)
    assert payload["instance_id"] == "inst_floor"
    if command.startswith("attach"):
        assert (workspace / ".playbill" / "coverage.json").exists()
    else:
        assert payload["floor_delivery"] == (command == "on")


def test_workspace_delivery_control_refuses_remote_transport(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://test",
            "playbill",
            "workspace",
            "floor-delivery",
            "on",
            "--instance-id",
            "inst_floor",
        ],
    )
    assert result.exit_code != 0
    assert "local --server-socket" in result.output
