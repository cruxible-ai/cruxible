"""Compiler proposals share admission and permission checks across public surfaces."""

import json

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_client.contracts.compiler_upgrade import COMPILER_UPGRADE_PATH, parse_compiler_upgrade
from cruxible_core.compiler.compiler import UPGRADE_COMPILER
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from tests.test_ledger.test_compiler_upgrade import old_instance


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


@pytest.mark.parametrize("surface", ["sdk", "mcp-local", "mcp-remote", "cli"])
def test_upgrade_surfaces_create_the_same_reviewable_proposal(
    tmp_path, monkeypatch, host_client, surface
):
    from cruxible_core.cli.commands import playbill as commands
    from cruxible_core.mcp import handlers

    instance, _, _ = old_instance(tmp_path, monkeypatch)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    before = instance.accepted_coordinate()
    base = PlaybillAcceptedCoordinate.from_internal(before)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda: "owner")
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = host_client
    if surface == "sdk":
        result = client.propose_playbill_compiler_upgrade(
            instance_id, target=UPGRADE_COMPILER, base=base, proposal_name="upgrade"
        )
        payload = result.model_dump(mode="json")
    elif surface.startswith("mcp"):
        monkeypatch.setattr(
            handlers, "_get_client", lambda: None if surface == "mcp-local" else client
        )
        result = handlers.handle_playbill_compiler_upgrade(
            instance_id,
            target_compiler_digest=UPGRADE_COMPILER.rule_digest,
            base=base.model_dump(mode="json"),
            proposal_name="upgrade",
        )
        payload = result.model_dump(mode="json")
    else:
        monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
        result = CliRunner().invoke(
            commands.propose_compiler_upgrade,
            [
                "--to",
                UPGRADE_COMPILER.rule_digest,
                "--name",
                "upgrade",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
    proposal = payload["proposal"]
    assert proposal["candidate"] is not None, payload
    tree = instance.proposal_tree(proposal["evaluation"]["evaluated_tree_oid"])
    transition = parse_compiler_upgrade(tree[COMPILER_UPGRADE_PATH])
    assert transition.target == UPGRADE_COMPILER
    assert transition.base.git_oid == base.git_oid
    assert transition.base.compiler_digest == base.compiler_digest
    assert instance.accepted_coordinate() == before
    # The same route cannot create even a proposal under a lower credential ceiling.
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    try:
        response = host_client.post(
            f"/api/v1/{instance_id}/playbill/compiler/proposals",
            json={
                "target": UPGRADE_COMPILER.model_dump(mode="json"),
                "base": base.model_dump(mode="json"),
                "proposal_name": "denied",
            },
        )
        assert response.status_code == 403, response.text
    finally:
        monkeypatch.delenv("CRUXIBLE_MODE")
        reset_permissions()
