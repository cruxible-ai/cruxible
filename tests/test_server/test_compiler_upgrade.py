"""Compiler proposals share admission and permission checks across public surfaces.

This release installs one compiler and no upgrade edge, so every surface refuses
an upgrade with the same typed refusal and writes nothing.
"""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from tests.test_ledger.test_activation import _instance

UNKNOWN_COMPILER = CompilerCoordinate(rule_digest="sha256:" + "ff" * 32)


@pytest.fixture
def host_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    playbill_api.get_playbill_manager().clear()
    return TestClient(create_app())


def _served(tmp_path, monkeypatch):
    instance, _, _ = _instance(tmp_path)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")
    return instance, instance_id


@pytest.mark.parametrize("surface", ["sdk", "mcp", "cli", "http"])
def test_every_surface_refuses_an_upgrade_with_no_edge(tmp_path, monkeypatch, host_client, surface):
    from cruxible_core.cli.commands import playbill as commands
    from cruxible_core.mcp import handlers

    instance, instance_id = _served(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    base = AcceptedCoordinate.from_internal(before)
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = host_client
    code = "cruxible.compiler_upgrade.unsupported_transition"
    if surface == "sdk":
        with pytest.raises(Exception, match=code):
            client.propose_compiler_upgrade(
                instance_id, target=UNKNOWN_COMPILER, base=base, proposal_name="upgrade"
            )
    elif surface == "mcp":
        monkeypatch.setattr(handlers, "_get_client", lambda: client)
        with pytest.raises(Exception, match=code):
            handlers.handle_playbill_compiler_upgrade(
                instance_id,
                target_compiler_digest=UNKNOWN_COMPILER.rule_digest,
                base=base.model_dump(mode="json"),
                proposal_name="upgrade",
            )
    elif surface == "cli":
        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        result = CliRunner().invoke(
            commands.propose_compiler_upgrade,
            ["--to", UNKNOWN_COMPILER.rule_digest, "--name", "upgrade", "--json"],
        )
        assert result.exit_code != 0
        assert code in result.output
    else:
        response = host_client.post(
            f"/api/v1/{instance_id}/compiler/proposals",
            json={
                "target": UNKNOWN_COMPILER.model_dump(mode="json"),
                "base": base.model_dump(mode="json"),
                "proposal_name": "upgrade",
            },
        )
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error_code"] == code
        assert "supported targets: none" in body["message"]
    assert instance.accepted_coordinate() == before
    # The same route refuses even the attempt under a lower credential ceiling.
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    try:
        response = host_client.post(
            f"/api/v1/{instance_id}/compiler/proposals",
            json={
                "target": UNKNOWN_COMPILER.model_dump(mode="json"),
                "base": base.model_dump(mode="json"),
                "proposal_name": "denied",
            },
        )
        assert response.status_code == 403, response.text
    finally:
        monkeypatch.delenv("CRUXIBLE_MODE")
        reset_permissions()


def test_upgrade_to_the_current_compiler_is_a_coded_400_not_a_500(
    tmp_path, monkeypatch, host_client
):
    instance, instance_id = _served(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    base = AcceptedCoordinate.from_internal(before)

    response = host_client.post(
        f"/api/v1/{instance_id}/compiler/proposals",
        json={
            "target": before.compiler.model_dump(mode="json"),
            "base": base.model_dump(mode="json"),
            "proposal_name": "noop",
        },
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "cruxible.compiler_upgrade.already_current"
    assert before.compiler.rule_digest in body["message"]
    assert body["repair"]["hand_edit"]["required_change"]
    assert instance.accepted_coordinate() == before


def test_an_upgrade_preview_with_no_edge_refuses_and_writes_nothing(
    tmp_path, monkeypatch, host_client
):
    from tests.support.store_snapshot import assert_writes_nothing

    instance, instance_id = _served(tmp_path, monkeypatch)
    before = instance.accepted_coordinate()
    body = {
        "target": UNKNOWN_COMPILER.model_dump(mode="json"),
        "base": AcceptedCoordinate.from_internal(before).model_dump(mode="json"),
        "proposal_name": "upgrade",
        "dry_run": True,
    }

    response = assert_writes_nothing(
        [tmp_path],
        lambda: host_client.post(f"/api/v1/{instance_id}/compiler/proposals", json=body),
        warm=instance.settled_workspace_advertisement,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "cruxible.compiler_upgrade.unsupported_transition"
    assert instance.accepted_coordinate() == before


def test_approve_without_a_key_names_where_to_find_or_make_one(tmp_path, monkeypatch):
    from cruxible_core.cli.commands import playbill as commands

    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(object(), "inst_x"))

    result = CliRunner().invoke(
        commands.approve_proposal, ["sha256:" + "ab" * 32, "--signer-id", "owner", "--yes"]
    )

    assert result.exit_code == 2, result.output
    assert "no signing key for principal owner" in result.output
    assert "cruxible context use --principal owner" in result.output
    assert "cruxible principal add" in result.output
