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
from cruxible_core.service.authoring.documents import AcceptedCoordinate
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


@pytest.mark.parametrize("surface", ["sdk", "mcp", "cli"])
def test_upgrade_surfaces_create_the_same_reviewable_proposal(
    tmp_path, monkeypatch, host_client, surface
):
    from cruxible_core.cli.commands import playbill as commands
    from cruxible_core.mcp import handlers

    instance, _, _ = old_instance(tmp_path, monkeypatch)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    before = instance.accepted_coordinate()
    base = AcceptedCoordinate.from_internal(before)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = host_client
    if surface == "sdk":
        result = client.propose_compiler_upgrade(
            instance_id, target=UPGRADE_COMPILER, base=base, proposal_name="upgrade"
        )
        payload = result.model_dump(mode="json")
    elif surface == "mcp":
        monkeypatch.setattr(handlers, "_get_client", lambda: client)
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
        landing = json.loads(result.output)
        # No signing key in this world: proposed, and the result says why it stopped.
        assert landing["outcome"]["reason"] == "signing_key_missing"
        payload = landing["proposal"]
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
            f"/api/v1/{instance_id}/compiler/proposals",
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


def test_upgrade_to_the_current_compiler_is_a_coded_400_not_a_500(
    tmp_path, monkeypatch, host_client
):
    instance, _, _ = old_instance(tmp_path, monkeypatch)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    before = instance.accepted_coordinate()
    base = AcceptedCoordinate.from_internal(before)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")

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


def test_an_upgrade_previews_on_the_proposal_path_and_writes_nothing(
    tmp_path, monkeypatch, host_client
):
    """R12: the upgrade's own evaluation, with no proposal, ref or record written."""

    from tests.support.store_snapshot import assert_writes_nothing

    instance, _, _ = old_instance(tmp_path, monkeypatch)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    before = instance.accepted_coordinate()
    base = AcceptedCoordinate.from_internal(before)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")
    body = {
        "target": UPGRADE_COMPILER.model_dump(mode="json"),
        "base": base.model_dump(mode="json"),
        "proposal_name": "upgrade",
    }
    url = f"/api/v1/{instance_id}/compiler/proposals"

    response = assert_writes_nothing(
        [tmp_path],
        lambda: host_client.post(url, json={**body, "dry_run": True}),
        warm=instance.settled_workspace_advertisement,
    )

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["status"] == "would_propose"
    assert "admission" not in preview["proposal"]
    assert preview["proposal"]["candidate"] is not None
    assert instance.accepted_coordinate() == before
    committed = host_client.post(
        url, json={**body, "dry_run": False, "at": preview["accepted_coordinate"]["git_oid"]}
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "admitted"


def _cli_world(tmp_path, monkeypatch, host_client, *, independent=False):
    from types import SimpleNamespace

    from cruxible_core.cli.commands import playbill as commands
    from cruxible_core.compiler.compiler import TRIGGER_CAPTURE_COMPILER

    if independent:
        from cruxible_core.runtime.instance import PlaybillInstance
        from tests.core_support._support import FIXED_TIMESTAMP, generate_client

        managed = tmp_path / "managed"
        owner = generate_client(
            tmp_path, managed_root=managed, principal_id="owner", roles=("owner",)
        )
        reviewer = generate_client(
            tmp_path, managed_root=managed, principal_id="reviewer", roles=("reviewer",)
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                "cruxible_core.runtime.instance.current_compiler_coordinate",
                lambda: TRIGGER_CAPTURE_COMPILER,
            )
            instance = PlaybillInstance.initialize(
                managed,
                instance_id="inst_activation",
                client_principals=(owner.principal, reviewer.principal),
                workspace_roots=(tmp_path / "workspace",),
                require_independent_approval=True,
                timestamp=FIXED_TIMESTAMP,
            )
    else:
        instance, owner, _reviewer = old_instance(tmp_path, monkeypatch, TRIGGER_CAPTURE_COMPILER)
    instance_id = instance.descriptor.instance_id
    get_registry().create_governed_instance_with_id(instance_id)
    monkeypatch.setattr(playbill_api.get_playbill_manager(), "get", lambda _: instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")
    client = CruxibleClient(base_url="http://testserver")
    client._client.close()
    client._client = host_client
    monkeypatch.setattr(client, "whoami", lambda _instance_id: SimpleNamespace(actor_id="owner"))
    monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(client, instance_id))
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return commands, instance, owner


def _upgrade(commands, *args):
    result = CliRunner().invoke(commands.propose_compiler_upgrade, [*args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_one_signed_command_proposes_signs_and_activates_the_running_compiler(
    tmp_path, monkeypatch, host_client
):
    from cruxible_core.compiler.compiler import current_compiler_coordinate

    commands, instance, owner = _cli_world(tmp_path, monkeypatch, host_client)
    before = instance.accepted_coordinate()
    key = str(owner.private_key_path)

    # Activation is one-way, so a key makes the bare command a preview first.
    preview = _upgrade(commands, "--key", key)
    assert preview["proposal"]["status"] == "would_propose"
    assert preview["proposal"]["compiler_upgrade"]["one_way"] is True
    assert preview["outcome"] is None and preview["approval"] is None
    assert preview["signing_key"] == {
        "signer_id": "owner",
        "private_key_path": key,
        "source": "option",
    }
    assert instance.accepted_coordinate() == before

    pin = preview["proposal"]["accepted_coordinate"]["git_oid"]
    landed = _upgrade(commands, "--key", key, "--commit", "--at", pin)
    assert landed["outcome"]["status"] == "landed", landed["outcome"]
    assert instance.accepted_coordinate().compiler == current_compiler_coordinate()
    assert landed["outcome"]["accepted_coordinate"]["git_oid"] == (
        instance.accepted_coordinate().git_oid
    )


def test_the_default_key_directory_supplies_the_key_init_made(tmp_path, monkeypatch, host_client):
    from cruxible_core.cli.principal_settings import default_key_dir

    commands, instance, owner = _cli_world(tmp_path, monkeypatch, host_client)
    custody = default_key_dir(instance.descriptor.instance_id, "owner")
    custody.mkdir(parents=True, mode=0o700)
    placed = custody / "owner.ed25519"
    placed.write_bytes(owner.private_key_path.read_bytes())
    placed.chmod(0o600)

    preview = _upgrade(commands, "--dry-run")
    assert preview["signing_key"]["source"] == "default_key_dir"
    assert preview["signing_key"]["private_key_path"] == str(placed)


@pytest.mark.parametrize(
    ("independent", "flags", "reason"),
    [
        (False, (), "signing_key_missing"),
        (False, ("--no-accept", "--key", "KEY"), "accept_never"),
        (True, ("--key", "KEY", "--commit"), "independent_approval_required"),
    ],
)
def test_an_upgrade_that_cannot_land_stays_a_proposal_with_its_reason(
    tmp_path, monkeypatch, host_client, independent, flags, reason
):
    commands, instance, owner = _cli_world(
        tmp_path, monkeypatch, host_client, independent=independent
    )
    before = instance.accepted_coordinate()
    args = [str(owner.private_key_path) if flag == "KEY" else flag for flag in flags]

    payload = _upgrade(commands, *args)

    assert payload["proposal"]["status"] == "admitted"
    assert payload["approval"] is None
    outcome = payload["outcome"]
    assert outcome["status"] == "proposed" and outcome["reason"] == reason, outcome
    assert outcome["proposal_id"] in outcome["next"]
    if reason == "signing_key_missing":
        # A fresh install's key location and the commands that load or make one.
        assert "cruxible init" in outcome["message"]
        assert "cruxible context use --principal owner" in outcome["message"]
        assert str(tmp_path / "config" / "cruxible" / "keys") in outcome["message"]
    assert instance.accepted_coordinate() == before


def test_approve_without_a_key_names_where_to_find_or_make_one(tmp_path, monkeypatch):
    from cruxible_core.cli.commands import playbill as commands

    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(commands, "_server_call", lambda op, **_: op(object(), "inst_x"))

    result = CliRunner().invoke(
        commands.approve_proposal, ["sha256:" + "ab" * 32, "--signer-id", "owner"]
    )

    assert result.exit_code == 2, result.output
    assert "no signing key for principal owner" in result.output
    assert "cruxible context use --principal owner" in result.output
    assert "cruxible principal add" in result.output
