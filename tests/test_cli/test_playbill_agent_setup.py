"""One command sets up an agent: key, principal, connection settings, and credential."""

from __future__ import annotations

import json
import shlex
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import cli
from cruxible_core.runtime.permissions import PermissionMode, reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry

INSTANCE = "inst_agent_setup"
URL = "http://cruxible-daemon"


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    for name in (
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    get_registry().create_governed_instance_with_id(INSTANCE)
    app = create_app()

    def get_client() -> CruxibleClient:
        # The CLI's own client, with its real headers, answered by this app in process.
        obj = _common._root_ctx_obj()
        cached = obj.get("_test_client")
        if isinstance(cached, CruxibleClient):
            return cached
        real = CruxibleClient(
            base_url=URL,
            token=get_runtime_bearer_token(),
            principal_id=obj.get("principal_id"),
        )
        headers = dict(real._client._client.headers)
        real._client = TestClient(app, base_url=URL, headers=headers)  # type: ignore[assignment]
        obj["_test_client"] = real
        return real

    monkeypatch.setattr(_common, "_get_client", get_client)
    yield app
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _run(*args: str, principal: str | None = None) -> Any:
    base = ["--server-url", URL, "--instance-id", INSTANCE]
    if principal is not None:
        base += ["--principal-id", principal]
    return CliRunner().invoke(cli, [*base, *args])


def _settings(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.startswith("export "):
            name, _, raw = line.removeprefix("export ").partition("=")
            values[name] = shlex.split(raw)[0]
    return values


def test_principal_add_registers_an_agent_and_writes_its_settings_with_auth_off(
    daemon: Any, tmp_path: Path
) -> None:
    owner_dir, agent_dir = tmp_path / "owner", tmp_path / "agent-b"
    made = _run("playbill", "init", "--key-dir", str(owner_dir), "--principal-id", "owner")
    assert made.exit_code == 0, made.output

    added = _run(
        "playbill",
        "principal",
        "add",
        "agent-b",
        "--key-dir",
        str(agent_dir),
        "--signer-key",
        str(owner_dir / "owner.ed25519"),
        principal="owner",
    )

    assert added.exit_code == 0, added.output
    assert "Principal agent-b: registered and active" in added.output
    settings_path = agent_dir / "cruxible.env"
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    settings = _settings(settings_path)
    assert settings["CRUXIBLE_PRINCIPAL_ID"] == "agent-b"
    assert settings["CRUXIBLE_INSTANCE_ID"] == INSTANCE
    assert settings["CRUXIBLE_SERVER_URL"] == URL
    assert settings["CRUXIBLE_PRINCIPAL_KEY"] == str((agent_dir / "agent-b.ed25519").resolve())
    # Auth off: the principal ID is the identity; no bearer credential is minted.
    assert "CRUXIBLE_SERVER_BEARER_TOKEN" not in settings
    who = _run("playbill", "whoami", "--json", principal="agent-b")
    assert who.exit_code == 0, who.output
    identity = json.loads(who.stdout)
    assert identity["actor_id"] == "agent-b"
    assert identity["principal_registration_status"] == "active"
    assert identity["authenticated"] is False


def test_principal_add_without_a_signer_key_proposes_and_names_each_next_step(
    daemon: Any, tmp_path: Path
) -> None:
    owner_dir = tmp_path / "owner"
    assert (
        _run("playbill", "init", "--key-dir", str(owner_dir), "--principal-id", "owner").exit_code
        == 0
    )

    added = _run(
        "playbill",
        "principal",
        "add",
        "agent-b",
        "--key-dir",
        str(tmp_path / "agent-b"),
        principal="owner",
    )

    assert added.exit_code == 0, added.output
    assert "Principal agent-b: proposed, not yet active" in added.output
    assert "Next: cruxible playbill proposal approve sha256:" in added.output
    assert "Next: cruxible playbill proposal activate sha256:" in added.output
    assert "cruxible credential mint --principal-id agent-b --key-dir" in added.output


def test_a_networked_owner_adds_a_propose_only_agent_in_one_command(
    daemon: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = get_runtime_credential_store().create_credential(
        instance_id=INSTANCE,
        label="bootstrap-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="runtime_bootstrap",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", operator.token)
    owner_dir, agent_dir = tmp_path / "owner", tmp_path / "agent-b"

    made = _run("playbill", "init", "--key-dir", str(owner_dir), "--principal-id", "owner")
    assert made.exit_code == 0, made.output
    owner = _settings(owner_dir / "cruxible.env")
    assert owner["CRUXIBLE_SERVER_BEARER_TOKEN"] not in made.output
    monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", owner["CRUXIBLE_SERVER_BEARER_TOKEN"])
    monkeypatch.setenv("CRUXIBLE_PRINCIPAL_KEY", owner["CRUXIBLE_PRINCIPAL_KEY"])

    added = _run(
        "playbill", "principal", "add", "agent-b", "--key-dir", str(agent_dir), principal="owner"
    )

    assert added.exit_code == 0, added.output
    assert "Principal agent-b: registered and active" in added.output
    assert "(governed_write), written to the settings file, not printed" in added.output
    agent = _settings(agent_dir / "cruxible.env")
    assert agent["CRUXIBLE_SERVER_BEARER_TOKEN"] not in added.output
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_KEY")
    pending = _run(
        "playbill",
        "principal",
        "add",
        "agent-c",
        "--key-dir",
        str(tmp_path / "agent-c"),
        "--json",
        principal="owner",
    )
    assert pending.exit_code == 0, pending.output
    pending_id = json.loads(pending.stdout)["proposal_id"]
    monkeypatch.setenv("CRUXIBLE_SERVER_BEARER_TOKEN", agent["CRUXIBLE_SERVER_BEARER_TOKEN"])
    who = _run("playbill", "whoami", "--json", principal="agent-b")
    assert who.exit_code == 0, who.output
    identity = json.loads(who.stdout)
    assert identity["actor_id"] == "agent-b"
    assert identity["actor_id_source"] == "runtime_credential"
    assert identity["credential_permission_mode"] == "governed_write"
    # Propose-only: the credential's tier refuses activation outright.
    refused = _run("playbill", "proposal", "activate", pending_id)
    assert refused.exit_code != 0
    assert "PermissionDeniedError" in refused.output
    assert "GRAPH_WRITE" in refused.output
