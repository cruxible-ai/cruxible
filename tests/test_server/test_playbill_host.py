"""DP-0B tests for the schema-free daemon host and credential boundary."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.errors import PlaybillReseedRequired
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry, reset_registry


@pytest.fixture
def host_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    return TestClient(create_app())


def test_host_allocation_is_idempotent_and_creates_no_semantic_state(
    host_client: TestClient,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_host"},
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "instance_id": "inst_dp0b_host",
        "status": "created",
    }

    record = get_registry().get("inst_dp0b_host")
    assert record is not None
    assert record.backend == GOVERNED_DAEMON_BACKEND
    assert record.workspace_root is None
    assert not Path(record.location).exists()

    repeated = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_host"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "already_exists"
    assert not Path(record.location).exists()


def test_remote_http_host_cannot_attach_a_daemon_local_workspace(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )

    refused = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_remote_path", "workspace_root": str(workspace)},
    )

    assert refused.status_code == 400
    assert "directly through the local Unix socket" in refused.text
    assert get_registry().get("inst_remote_path") is None


def test_transport_credentials_do_not_initialize_playbill_or_a_legacy_graph(
    host_client: TestClient,
) -> None:
    created = host_client.post("/api/v1/runtime/instances", json={})
    instance_id = created.json()["instance_id"]
    record = get_registry().get(instance_id)
    assert record is not None

    credential = host_client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json={"label": "automation", "permission_mode": "governed_write"},
    )
    assert credential.status_code == 200, credential.text
    assert credential.json()["credential"]["instance_id"] == instance_id
    assert not Path(record.location).exists()

    uninitialized = host_client.get(f"/api/v1/{instance_id}/playbill/documents")
    assert uninitialized.status_code == 409
    assert "not initialized" in uninitialized.text
    assert not Path(record.location).exists()


def test_pre_pc_hr_nested_instance_requires_reseed(host_client: TestClient) -> None:
    registered = get_registry().create_governed_instance_with_id("inst_legacy_nested")
    (Path(registered.record.location) / ".cruxible/playbill-v1").mkdir(parents=True)

    with pytest.raises(PlaybillReseedRequired, match="playbill.instance.reseed_required"):
        get_playbill_manager().get("inst_legacy_nested")


def test_managed_root_and_trust_root_must_be_archived_together(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_archive_pair"},
    )
    record = get_registry().get(created.json()["instance_id"])
    assert record is not None
    managed_root = Path(record.location)
    owner = generate_client_principal_key(
        tmp_path / "archive-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    initialized = host_client.post(
        "/api/v1/inst_archive_pair/playbill/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200
    managed_root.rename(tmp_path / "archived-instance")
    get_playbill_manager().clear()

    with pytest.raises(PlaybillReseedRequired):
        get_playbill_manager().get("inst_archive_pair")
    with pytest.raises(PlaybillReseedRequired):
        get_playbill_manager().initialize(
            "inst_archive_pair",
            client_principals=(owner.principal,),
        )


def test_registry_state_root_is_frozen_for_instance_and_trust_paths(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    registry = get_registry()
    original_root = registry.state_root
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "other-state"))

    record = registry.create_governed_instance_with_id("inst_frozen_state").record

    assert Path(record.location).is_relative_to(original_root)
    assert not Path(record.location).is_relative_to(tmp_path / "other-state")


def test_playbill_bootstrap_is_the_first_semantic_write(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_bootstrap"},
    )
    instance_id = created.json()["instance_id"]
    record = get_registry().get(instance_id)
    assert record is not None
    managed_root = Path(record.location)
    assert not managed_root.exists()

    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    initialized = host_client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200, initialized.text
    assert initialized.json()["instance_id"] == instance_id
    assert initialized.json()["approval_policy_mode"] == "self_approval_allowed"
    assert managed_root.is_dir()
    assert not (managed_root / ".cruxible" / "state.db").exists()
    trust_directory = tmp_path / "server-state" / "trust"
    assert (trust_directory / "inst_dp0b_bootstrap.json").is_file()
    assert trust_directory.stat().st_mode & 0o777 == 0o700


def test_attached_bootstrap_inherits_sha1_and_advertises_genesis(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "cruxible.sock"))
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    refused = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_attached_http", "workspace_root": str(workspace)},
    )
    assert refused.status_code == 400

    created = host_api.create_playbill_host(
        instance_id="inst_attached",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    assert created.status == "created"
    record = get_registry().get("inst_attached")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "attached-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(workspace,),
    )

    initialized = playbill_api.playbill_init(
        "inst_attached",
        principals=(owner.principal,),
        workspace_attachment_authorized=True,
    )

    assert get_playbill_manager().get("inst_attached").descriptor.git_object_format == "sha1"
    assert initialized.workspace_advertisement.status == "updated"
    assert initialized.workspace_advertisement.advertised_refs == ("refs/remotes/playbill/main",)
    remote_url = subprocess.run(
        ["git", "-C", str(workspace), "remote", "get-url", "playbill"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_url.endswith("ledger.git")


def test_propose_document_never_executes_workspace_instead_of_ssh_command(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    del host_client
    workspace = tmp_path / "rce-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_rce_regression",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    record = get_registry().get("inst_rce_regression")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "rce-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(workspace,),
    )
    initialized = playbill_api.playbill_init(
        "inst_rce_regression",
        principals=(owner.principal,),
        workspace_attachment_authorized=True,
    )
    assert initialized.workspace_advertisement.status == "updated"

    ledger_url = subprocess.run(
        ["git", "-C", str(workspace), "config", "--local", "--get", "remote.playbill.url"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    daemon_uid_marker = tmp_path / "daemon-uid"
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "url.ssh://attacker.invalid/x.insteadOf",
            ledger_url,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "core.sshCommand",
            f"/bin/sh -c 'id > {daemon_uid_marker}'",
        ],
        check=True,
        capture_output=True,
    )

    stored = playbill_api.playbill_store_body(
        "inst_rce_regression",
        content_base64=base64.b64encode(b"security boundary\n").decode("ascii"),
    )
    proposed = playbill_api.playbill_propose_document(
        "inst_rce_regression",
        shell=DocumentShell(
            identity="document:rce-regression",
            document_kind="design",
            title="RCE regression",
            media_type="text/plain",
            body_digest=stored.digest,
            authority=DocumentAuthority(required_tier="graph_write"),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        proposal_name="rce-regression",
    )

    assert proposed.proposal["admission"]["proposal_id"]
    assert proposed.workspace_advertisement.status == "failed"
    assert proposed.workspace_advertisement.failure_code == "remote_conflict"
    assert not daemon_uid_marker.exists()


def test_failed_init_rolls_back_a_new_workspace_attachment(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    workspace = tmp_path / "rollback-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(instance_id="inst_rollback")
    record = get_registry().get("inst_rollback")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "rollback-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )

    def fail_initialize(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated initialization failure")

    monkeypatch.setattr(get_playbill_manager(), "initialize", fail_initialize)
    with pytest.raises(RuntimeError, match="simulated initialization failure"):
        playbill_api.playbill_init(
            "inst_rollback",
            principals=(owner.principal,),
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )

    rolled_back = get_registry().get("inst_rollback")
    assert rolled_back is not None
    assert rolled_back.workspace_root is None


def test_init_survives_an_advertiser_that_raises(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    workspace = tmp_path / "raising-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_raising_advertiser",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    record = get_registry().get("inst_raising_advertiser")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "raising-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise MemoryError("simulated advertiser failure")

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.advertise_workspace_refs",
        explode,
    )
    initialized = playbill_api.playbill_init(
        "inst_raising_advertiser",
        principals=(owner.principal,),
        workspace_attachment_authorized=True,
    )

    assert initialized.workspace_advertisement.status == "failed"
    assert initialized.workspace_advertisement.failure_code == "unexpected_failure"
    assert get_playbill_manager().get("inst_raising_advertiser") is not None


def test_independent_approval_init_requires_and_accepts_a_second_ordinary_principal(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    solo_id = host_client.post(
        "/api/v1/runtime/instances", json={"instance_id": "inst_solo_refusal"}
    ).json()["instance_id"]
    solo_record = get_registry().get(solo_id)
    assert solo_record is not None
    solo_root = Path(solo_record.location)
    owner = generate_client_principal_key(
        tmp_path / "solo-owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(solo_root,),
    )
    refused = host_client.post(
        f"/api/v1/{solo_id}/playbill/init",
        json={
            "principals": [owner.principal.model_dump(mode="json")],
            "require_independent_approval": True,
        },
    )
    assert refused.status_code == 409
    assert "independent approval requires at least two" in refused.text

    governed_id = host_client.post(
        "/api/v1/runtime/instances", json={"instance_id": "inst_independent"}
    ).json()["instance_id"]
    governed_record = get_registry().get(governed_id)
    assert governed_record is not None
    governed_root = Path(governed_record.location)
    reviewer = generate_client_principal_key(
        tmp_path / "independent-reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(governed_root,),
    )
    accepted = host_client.post(
        f"/api/v1/{governed_id}/playbill/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ],
            "require_independent_approval": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["approval_policy_mode"] == "independent_approval_required"
    instance = get_playbill_manager().get(governed_id)
    assert instance.inspect().approval_policy_mode == "independent_approval_required"
    assert instance._verified_genesis.approval_policy.mode == "independent_approval_required"


def test_authenticated_bootstrap_binds_owner_to_credential_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "authenticated-server-state"
    bootstrap_secret = "one-time-bootstrap-secret"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_dir))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", bootstrap_secret)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    client = TestClient(create_app())
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}

    allocated = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_authenticated_bootstrap"},
        headers=bootstrap_headers,
    )
    assert allocated.status_code == 200, allocated.text
    instance_id = allocated.json()["instance_id"]

    claimed = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text
    admin_headers = {"Authorization": f"Bearer {claimed.json()['token']}"}

    record = get_registry().get(instance_id)
    assert record is not None
    managed_root = Path(record.location)
    owner = generate_client_principal_key(
        tmp_path / "authenticated-owner-custody",
        principal_id="bootstrap-admin",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "authenticated-reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    initialized = client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ]
        },
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    assert managed_root.is_dir()
    assert not (managed_root / ".cruxible" / "state.db").exists()
