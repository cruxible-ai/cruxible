"""DP-0B tests for the schema-free daemon host and credential boundary."""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Iterator
from inspect import iscoroutinefunction
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.errors import (
    BootstrapError,
    FormatError,
    ReseedRequired,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.workspace_file import WorkspaceFileSourceRequest
from cruxible_core.documents.workspace_file import (
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
    workspace_binding_digest,
)
from cruxible_core.errors import ConfigError
from cruxible_core.governance.keys import (
    GeneratedKeyMaterial,
    generate_client_principal_key,
)
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.runtime import playbill_manager as playbill_manager_module
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import (
    GOVERNED_DAEMON_BACKEND,
    LOCAL_FILESYSTEM_BACKEND,
    get_registry,
    reset_registry,
)
from cruxible_core.server.routes.playbill import append_claim_attestation, run_procedure
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.procedures.procedure_runs import ProcedureRunRequest
from tests.test_ledger.test_activation import _sign


@pytest.fixture
def host_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    return TestClient(create_app())


@pytest.fixture
def authenticated_host_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    bootstrap_secret = "one-time-bootstrap-secret"
    state = tmp_path / "authenticated-server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", bootstrap_secret)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    try:
        with TestClient(create_app()) as client:
            yield client, bootstrap_secret
    finally:
        get_playbill_manager().clear()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()


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
        # Pinned to the row it allocated against: there was none.
        "coordinate": created.json()["coordinate"],
    }
    assert created.json()["coordinate"]["subject"] == "host:inst_dp0b_host"

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


def test_host_show_and_server_status_inspect_uninitialized_hosts_without_writing(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    get_registry()._insert_instance(
        backend=LOCAL_FILESYSTEM_BACKEND,
        location=str(tmp_path / "unrelated-local"),
        workspace_root=None,
    )
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_show_empty"},
    )
    assert created.status_code == 200
    record = get_registry().get("inst_show_empty")
    assert record is not None

    shown = host_client.get("/api/v1/inst_show_empty/host")
    assert shown.status_code == 200, shown.text
    assert shown.json() == {
        "tag": "playbill-host-inspection-v1",
        "instance_id": "inst_show_empty",
        "managed_root": str(Path(record.location).resolve()),
        "workspace_root": None,
        "floor_delivery": False,
        "compiler_coordinate": None,
        "compiler_revision": None,
        "compatibility": "uninitialized",
        "writable": False,
        "reason": None,
    }
    status = host_client.get("/api/v1/server/info")
    assert status.status_code == 200, status.text
    assert status.json()["instance_count"] == 1
    assert [row["instance_id"] for row in status.json()["hosts"]] == ["inst_show_empty"]
    from cruxible_core.compiler.compiler import (
        COMPILER_REVISION_LABELS,
        current_compiler_coordinate,
    )

    # The host reports whatever revision the daemon compiles under today.
    assert (
        status.json()["compiler_revision"]
        == (COMPILER_REVISION_LABELS[current_compiler_coordinate()])
    )
    assert not Path(record.location).exists()


def test_status_keeps_malformed_host_as_typed_reseed_row(
    host_client: TestClient,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_malformed_show"},
    )
    assert created.status_code == 200
    record = get_registry().get("inst_malformed_show")
    assert record is not None
    managed = Path(record.location)
    managed.mkdir(parents=True)
    trust = get_registry().state_root / "trust" / "inst_malformed_show.json"
    trust.parent.mkdir(parents=True)
    trust.write_text("not canonical trust data", encoding="utf-8")

    status = host_client.get("/api/v1/server/info")
    assert status.status_code == 200, status.text
    row = status.json()["hosts"][0]
    assert row["compatibility"] == "reseed_required"
    assert row["reason"]["code"] == "host_state_malformed"
    assert row["reason"]["repair_commands"] == ["cruxible host create"]


def test_status_keeps_other_hosts_when_one_inspection_raises_unexpectedly(
    host_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for instance_id in ("inst_healthy_row", "inst_unexpected_row"):
        created = host_client.post(
            "/api/v1/runtime/instances",
            json={"instance_id": instance_id},
        )
        assert created.status_code == 200, created.text
    broken = get_registry().get("inst_unexpected_row")
    assert broken is not None
    Path(broken.location).mkdir(parents=True)
    trust = get_registry().state_root / "trust" / "inst_unexpected_row.json"
    trust.parent.mkdir(parents=True)
    trust.write_text("present", encoding="utf-8")
    manager = get_playbill_manager()
    original_get = manager.get

    def get_with_one_failure(instance_id: str):
        if instance_id == "inst_unexpected_row":
            raise RuntimeError("unexpected inspection failure")
        return original_get(instance_id)

    monkeypatch.setattr(manager, "get", get_with_one_failure)

    status = host_client.get("/api/v1/server/info")

    assert status.status_code == 200, status.text
    rows = {row["instance_id"]: row for row in status.json()["hosts"]}
    assert rows["inst_healthy_row"]["compatibility"] == "uninitialized"
    assert rows["inst_unexpected_row"]["reason"]["code"] == "host_state_malformed"
    assert "RuntimeError" in rows["inst_unexpected_row"]["reason"]["detail"]


def test_git_advertising_write_routes_run_outside_the_event_loop() -> None:
    assert not iscoroutinefunction(append_claim_attestation)
    assert not iscoroutinefunction(run_procedure)


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


def test_workspace_dedupe_never_replaces_an_explicit_host_id(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    workspace = tmp_path / "deduped-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    first = host_api.create_playbill_host(
        instance_id="inst_workspace_owner",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    assert first.instance_id == "inst_workspace_owner"
    assert first.status == "created"

    repeated = host_api.create_playbill_host(
        instance_id="inst_workspace_owner",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    assert repeated.instance_id == "inst_workspace_owner"
    assert repeated.status == "already_exists"

    with pytest.raises(
        ConfigError,
        match=(
            "already attached to Cruxible host 'inst_workspace_owner'.*"
            "before creating 'inst_workspace_other'"
        ),
    ):
        host_api.create_playbill_host(
            instance_id="inst_workspace_other",
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )
    with pytest.raises(ConfigError, match="already attached to Cruxible host"):
        host_api.create_playbill_host(
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )

    registry = get_registry()
    monkeypatch.setattr(registry, "get_governed_instance_by_workspace_root", lambda _path: None)
    with pytest.raises(
        ConfigError,
        match="already attached to Cruxible host 'inst_workspace_owner'",
    ):
        host_api.create_playbill_host(
            instance_id="inst_workspace_race",
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )
    assert [item.instance_id for item in registry.list_governed_instances()] == [
        "inst_workspace_owner"
    ]
    assert not registry.governed_instance_location("inst_workspace_other").exists()
    assert not registry.governed_instance_location("inst_workspace_race").exists()


def test_host_registration_status_separates_remote_visibility_from_registration(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "registered-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_registration_status",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    local = host_api.playbill_host_workspace_registration(
        "inst_registration_status",
        expose_workspace_path=True,
    )
    assert local.status == "registered"
    assert local.workspace_path == str(workspace.resolve())

    remote = host_client.get("/api/v1/inst_registration_status/workspace-registration")
    assert remote.status_code == 200, remote.text
    assert remote.json()["status"] == "registered"
    assert remote.json()["workspace_path"] is None
    assert remote.json()["delivers_here"] is None


def test_registration_answers_delivers_here_over_tcp_without_echoing_paths(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """Single floor writer: a TCP client learns the daemon delivers its floor (precheck 1)."""

    workspace = tmp_path / "delivered-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_delivers_here",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    url = "/api/v1/inst_delivers_here/workspace-registration"

    here = host_client.get(url, params={"workspace_root": str(workspace)})
    assert here.status_code == 200, here.text
    assert here.json()["floor_delivery"] is True
    assert here.json()["delivers_here"] is True
    assert here.json()["workspace_path"] is None
    elsewhere = host_client.get(url, params={"workspace_root": str(tmp_path / "other")})
    assert elsewhere.json()["delivers_here"] is False
    get_registry().set_floor_delivery("inst_delivers_here", False)
    off = host_client.get(url, params={"workspace_root": str(workspace)})
    assert off.json()["delivers_here"] is False


def test_transport_credentials_do_not_initialize_playbill_or_a_legacy_graph(
    host_client: TestClient,
) -> None:
    created = host_client.post("/api/v1/runtime/instances", json={})
    instance_id = created.json()["instance_id"]
    record = get_registry().get(instance_id)
    assert record is not None

    # With auth off a credential authenticates nothing, so minting refuses
    # rather than inventing state or latching the state root.
    credential = host_client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json={"principal_id": "automation", "permission_mode": "governed_write"},
    )
    assert credential.status_code == 409, credential.text
    assert credential.json()["error_code"] == "runtime_credential.auth_off"
    assert not Path(record.location).exists()

    uninitialized = host_client.get(f"/api/v1/{instance_id}/head")
    assert uninitialized.status_code == 409
    assert "not initialized" in uninitialized.text
    assert not Path(record.location).exists()


def test_pre_pc_hr_nested_instance_requires_reseed(host_client: TestClient) -> None:
    registered = get_registry().create_governed_instance_with_id("inst_legacy_nested")
    (Path(registered.record.location) / ".cruxible/playbill-v1").mkdir(parents=True)

    with pytest.raises(ReseedRequired, match="cruxible.instance.reseed_required"):
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
        "/api/v1/inst_archive_pair/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200
    managed_root.rename(tmp_path / "archived-instance")
    get_playbill_manager().clear()

    with pytest.raises(ReseedRequired):
        get_playbill_manager().get("inst_archive_pair")
    with pytest.raises(ReseedRequired):
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
    other_state = tmp_path / "other-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(other_state))

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
        f"/api/v1/{instance_id}/init",
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


def test_playbill_init_retry_is_idempotent_only_for_the_exact_bootstrap_request(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    del host_client
    host_api.create_playbill_host(instance_id="inst_exact_init_retry")
    record = get_registry().get("inst_exact_init_retry")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "exact-init-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    first = playbill_api.playbill_init(
        "inst_exact_init_retry",
        principals=(owner.principal,),
    )
    retry = playbill_api.playbill_init(
        "inst_exact_init_retry",
        principals=(owner.principal,),
    )
    assert retry == first

    different_owner = generate_client_principal_key(
        tmp_path / "different-init-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    with pytest.raises(BootstrapError, match="different principal set"):
        playbill_api.playbill_init(
            "inst_exact_init_retry",
            principals=(different_owner.principal,),
        )


def _init_owner(tmp_path: Path, instance_id: str, custody: str) -> GeneratedKeyMaterial:
    host_api.create_playbill_host(instance_id=instance_id)
    record = get_registry().get(instance_id)
    assert record is not None
    return generate_client_principal_key(
        tmp_path / custody,
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )


def test_initialization_installs_no_provider_and_writes_no_candidate(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    del host_client
    owner = _init_owner(tmp_path, "inst_unseeded", "unseeded-owner")
    first = playbill_api.playbill_init(
        "inst_unseeded",
        principals=(owner.principal,),
    )

    instance = get_playbill_manager().get("inst_unseeded")
    assert instance.proposal_evidence().list_admissions() == ()
    assert instance.accepted_history()[-1].sequence == 0
    assert "providers/cruxible-provider-workspace.json" not in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )

    retry = playbill_api.playbill_init(
        "inst_unseeded",
        principals=(owner.principal,),
    )
    assert retry == first
    assert instance.proposal_evidence().list_admissions() == ()


def test_independent_approval_init_creates_no_provider_proposal(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    governed_id = host_client.post(
        "/api/v1/runtime/instances", json={"instance_id": "inst_unseeded_independent"}
    ).json()["instance_id"]
    record = get_registry().get(governed_id)
    assert record is not None
    managed_root = Path(record.location)
    owner = generate_client_principal_key(
        tmp_path / "unseeded-independent-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "unseeded-independent-reviewer",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    payload = {
        "principals": [
            owner.principal.model_dump(mode="json"),
            reviewer.principal.model_dump(mode="json"),
        ],
        "require_independent_approval": True,
    }

    accepted = host_client.post(f"/api/v1/{governed_id}/init", json=payload)
    assert accepted.status_code == 200, accepted.text
    retry = host_client.post(f"/api/v1/{governed_id}/init", json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json() == accepted.json()
    instance = get_playbill_manager().get(governed_id)
    assert instance.proposal_evidence().list_admissions() == ()
    assert instance.inspect().approval_policy_mode == "independent_approval_required"


def _read_workspace_path(reader: WorkspaceFileReader, root: Path, relative_path: str) -> None:
    request = WorkspaceFileSourceRequest(
        logical_source="workspace.docs",
        workspace_binding_digest=workspace_binding_digest(
            instance_id=reader.instance_id, canonical_root=root
        ),
        relative_path=relative_path,
        coordinate_type="workspace-snapshot-v1",
        coordinate={"revision": "working"},
        selector_type="workspace-file-v1",
        selector={"document": "docs"},
    )
    reader.read(
        request,
        run_id="RUN-state-root",
        admission_binding_digest="sha256:" + "1" * 64,
        occurrence_path="source:read",
        policy_coordinate=AcceptedCoordinate(
            git_oid="a" * 64,
            semantic_root="sha256:" + "b" * 64,
            generation_root="sha256:" + "c" * 64,
            compiler_digest="sha256:" + "d" * 64,
        ),
        resolved_max_bytes=1024,
        derived_request_digest="sha256:" + "2" * 64,
        read_at=utc_now(),
    )


def test_every_daemon_state_root_path_is_refused_even_inside_an_allowed_root(
    host_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client
    host_api.create_playbill_host(instance_id="inst_state_root_denied")
    record = get_registry().get("inst_state_root_denied")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "state-root-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    playbill_api.playbill_init("inst_state_root_denied", principals=(owner.principal,))

    manager = get_playbill_manager()
    state_root = get_registry().state_root
    allowed_root = state_root.parent.resolve(strict=True)
    operator = manager.provider_runtime_operator()
    monkeypatch.setattr(
        operator,
        "config",
        operator.config.model_copy(update={"workspace_allowed_roots": (str(allowed_root),)}),
    )
    leaked = (
        state_root / "trust" / "inst_state_root_denied.json",
        state_root / "daemon" / "provider-secrets" / "realm.json",
        state_root / "daemon" / "provider-runtime.json",
        state_root / "instances" / "inst_other_tenant" / "ledger",
    )
    for path in leaked:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"daemon secret")
    reader = manager.workspace_file_reader("inst_state_root_denied")

    relatives = tuple(path.resolve().relative_to(allowed_root).as_posix() for path in leaked)
    for relative in relatives:
        with pytest.raises(WorkspaceFileReadRefused) as caught:
            _read_workspace_path(reader, allowed_root, relative)
        assert caught.value.path_class == "managed_root", relative

    # The registry freezes its state root, so a later environment move must not
    # unprotect the substrate the instance actually lives in.
    moved_root = tmp_path / "moved-state"
    moved_root.mkdir()
    monkeypatch.setattr(playbill_manager_module, "get_server_state_root", lambda: moved_root)
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    moved_reader = manager.workspace_file_reader("inst_state_root_denied")
    for relative in relatives:
        with pytest.raises(WorkspaceFileReadRefused) as caught:
            _read_workspace_path(moved_reader, allowed_root, relative)
        assert caught.value.path_class == "managed_root", relative


def test_unavailable_workspace_configuration_reaches_run_service_as_typed_absence(
    host_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del host_client

    class ServiceReached(Exception):
        pass

    def unavailable_reader(_instance_id: str) -> None:
        raise WorkspaceFileReadRefused("binding", "configured root is unavailable")

    manager = SimpleNamespace(
        get=lambda _instance_id: object(),
        provider_runtime_operator=lambda: object(),
        workspace_file_reader=unavailable_reader,
    )
    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: manager)
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(playbill_api, "_actor_context", lambda: object())
    # The stub instance has no accepted Procedure to tier the run by.
    monkeypatch.setattr(playbill_api, "procedure_run_target_rung", lambda *_args: 0)

    def service(*_args: object, **kwargs: object) -> None:
        assert kwargs["workspace_file_reader"] is None
        raise ServiceReached

    monkeypatch.setattr(playbill_api, "service_run_playbill_procedure", service)
    with pytest.raises(ServiceReached):
        playbill_api.playbill_procedure_run(
            "inst_unavailable_workspace",
            "non-workspace-procedure",
            request=ProcedureRunRequest(input={}),
        )


def test_an_initialized_host_attaches_a_worktree_in_place(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """Q16: attachment is not fixed at host create; nothing is archived or rebuilt."""

    del host_client
    host_api.create_playbill_host(instance_id="inst_unattached_initialized")
    record = get_registry().get("inst_unattached_initialized")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "unattached-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    playbill_api.playbill_init(
        "inst_unattached_initialized",
        principals=(owner.principal,),
    )
    before = get_playbill_manager().get("inst_unattached_initialized").accepted_coordinate()
    workspace = tmp_path / "late-workspace"
    subprocess.run(["git", "init", "-b", "main", str(workspace)], check=True, capture_output=True)

    result = host_api.create_playbill_host(
        instance_id="inst_unattached_initialized",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    assert result.status == "already_exists"
    attached = get_registry().get("inst_unattached_initialized")
    assert attached is not None and attached.workspace_root == str(workspace.resolve())
    instance = get_playbill_manager().get("inst_unattached_initialized")
    assert instance.accepted_coordinate() == before
    assert instance.settled_workspace_advertisement().workspace_path == str(workspace.resolve())


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
    assert initialized.workspace_advertisement.advertised_refs == (
        "refs/remotes/cruxible-ledger/accepted",
    )
    local_branches = subprocess.run(
        ["git", "-C", str(workspace), "for-each-ref", "--format=%(refname)", "refs/heads"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    stored = playbill_api.playbill_store_body(
        "inst_attached",
        content_base64=base64.b64encode(b"review candidate\n").decode("ascii"),
    )
    proposed = playbill_api.playbill_propose_document(
        "inst_attached",
        shell=DocumentShell(
            identity="document:review-candidate",
            document_kind="design",
            title="Review candidate",
            media_type="text/plain",
            body_digest=stored.digest,
            authority=DocumentAuthority(required_tier="graph_write"),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        proposal_name="review-candidate",
    )
    proposal_id = proposed.proposal["admission"]["proposal_id"]
    proposal_key = proposal_id.removeprefix("sha256:")
    instance = get_playbill_manager().get("inst_attached")
    # Writes queue the advertisement; settling waits for it and reports it.
    assert proposed.workspace_advertisement.status == "scheduled"
    assert proposed.workspace_advertisement.workspace_path == str(workspace.resolve())
    assert instance.settled_workspace_advertisement().advertised_refs == (
        "refs/remotes/cruxible-ledger/accepted",
        f"refs/remotes/cruxible-ledger/proposals/{proposal_key}",
    )
    remote_branches = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--remotes", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert remote_branches == [
        "refs/remotes/cruxible-ledger/accepted",
        f"refs/remotes/cruxible-ledger/proposals/{proposal_key}",
    ]
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == local_branches
    )
    candidate_digest = proposed.proposal["evaluation"]["candidate_digest"]
    signed = _sign(owner, candidate_digest, instance.accepted_coordinate().semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="operator",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        activated_by="operator",
    )
    assert activated.status == "accepted"
    assert activated.workspace_advertisement.status == "scheduled"
    assert instance.settled_workspace_advertisement().advertised_refs == (
        "refs/remotes/cruxible-ledger/accepted",
    )
    assert subprocess.run(
        ["git", "-C", str(workspace), "branch", "--remotes", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines() == ["refs/remotes/cruxible-ledger/accepted"]
    remote_url = subprocess.run(
        ["git", "-C", str(workspace), "remote", "get-url", "cruxible-ledger"],
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
        ["git", "-C", str(workspace), "config", "--local", "--get", "remote.cruxible-ledger.url"],
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
    assert proposed.workspace_advertisement.status == "scheduled"
    settled = get_playbill_manager().get("inst_rce_regression").settled_workspace_advertisement()
    assert settled.status == "failed"
    assert settled.failure_code == "remote_conflict"
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
    host_client = host_client
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
        f"/api/v1/{solo_id}/init",
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
        f"/api/v1/{governed_id}/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ],
            "require_independent_approval": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    retry = host_client.post(
        f"/api/v1/{governed_id}/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ],
            "require_independent_approval": True,
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == accepted.json()
    assert accepted.json()["approval_policy_mode"] == "independent_approval_required"
    instance = get_playbill_manager().get(governed_id)
    assert len(instance.proposal_evidence().list_admissions()) == 0
    assert instance.inspect().approval_policy_mode == "independent_approval_required"
    assert instance._verified_genesis.approval_policy.mode == "independent_approval_required"


def test_authenticated_bootstrap_binds_owner_to_credential_identity(
    tmp_path: Path,
    authenticated_host_client: tuple[TestClient, str],
) -> None:
    client, bootstrap_secret = authenticated_host_client
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
        f"/api/v1/{instance_id}/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ],
        },
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    assert managed_root.is_dir()
    assert not (managed_root / ".cruxible" / "state.db").exists()


def test_host_show_enforces_initialization_scope_and_path_privacy(
    tmp_path: Path,
    authenticated_host_client: tuple[TestClient, str],
) -> None:
    client, bootstrap_secret = authenticated_host_client
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}
    for instance_id in ("inst_scoped_show", "inst_other_show"):
        created = client.post(
            "/api/v1/runtime/instances",
            json={"instance_id": instance_id},
            headers=bootstrap_headers,
        )
        assert created.status_code == 200, created.text

    operator_view = client.get(
        "/api/v1/inst_scoped_show/host",
        headers=bootstrap_headers,
    )
    assert operator_view.status_code == 200, operator_view.text
    assert operator_view.json()["managed_root"] is not None

    claimed = client.post(
        "/api/v1/inst_scoped_show/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text
    scoped_headers = {"Authorization": f"Bearer {claimed.json()['token']}"}

    preinit = client.get(
        "/api/v1/inst_scoped_show/host",
        headers=scoped_headers,
    )
    assert preinit.status_code == 403, preinit.text

    record = get_registry().get("inst_scoped_show")
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "scoped-show-owner",
        principal_id="bootstrap-admin",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    initialized = client.post(
        "/api/v1/inst_scoped_show/init",
        # Scope and path privacy, not seeding: this host takes init's explicit opt-out.
        json={"principals": [owner.principal.model_dump(mode="json")]},
        headers=scoped_headers,
    )
    assert initialized.status_code == 200, initialized.text

    own = client.get(
        "/api/v1/inst_scoped_show/host",
        headers=scoped_headers,
    )
    assert own.status_code == 200, own.text
    assert own.json()["managed_root"] is None
    assert own.json()["compatibility"] == "writable"

    cross_instance = client.get(
        "/api/v1/inst_other_show/host",
        headers=scoped_headers,
    )
    assert cross_instance.status_code == 403, cross_instance.text


def test_a_daemon_allocates_more_than_one_host_per_bootstrap_secret(
    authenticated_host_client: tuple[TestClient, str],
) -> None:
    """Card 98: claiming the bootstrap credential must not make the daemon a one-shot.

    Every other credential on a daemon is instance-scoped and is rejected by
    `require_unscoped_operator`, so gating host creation on an UNCLAIMED
    bootstrap secret meant that after the first `credential claim-bootstrap` no
    credential could allocate a second host. The only repair was restarting the
    daemon, which mints a fresh secret at the same path and takes every hosted
    instance offline -- not something a control plane can do to add a tenant.
    """

    client, bootstrap_secret = authenticated_host_client
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}
    first = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_first_tenant"},
        headers=bootstrap_headers,
    )
    assert first.status_code == 200, first.text
    claimed = client.post(
        f"/api/v1/{first.json()['instance_id']}/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text

    second = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_second_tenant"},
        headers=bootstrap_headers,
    )

    assert second.status_code == 200, second.text
    assert second.json()["instance_id"] == "inst_second_tenant"
    # The claim is once per host (Q16): the second host claims its own first
    # ADMIN credential with the same secret, and no host claims twice.
    second_claim = client.post(
        "/api/v1/inst_second_tenant/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert second_claim.status_code == 200, second_claim.text
    assert second_claim.json()["instance_id"] == "inst_second_tenant"
    reclaimed = client.post(
        f"/api/v1/{first.json()['instance_id']}/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert reclaimed.status_code == 401, reclaimed.text
    assert reclaimed.json()["error_code"] == "runtime_bootstrap.secret_already_claimed"


def test_an_instance_scoped_credential_creating_a_host_is_told_what_to_present(
    authenticated_host_client: tuple[TestClient, str],
) -> None:
    """The remaining refusal names the sanctioned way to add a host."""

    client, bootstrap_secret = authenticated_host_client
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}
    created = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_scope_repair"},
        headers=bootstrap_headers,
    )
    assert created.status_code == 200, created.text
    claimed = client.post(
        "/api/v1/inst_scope_repair/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text

    refused = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_from_a_tenant"},
        headers={"Authorization": f"Bearer {claimed.json()['token']}"},
    )

    assert refused.status_code == 403, refused.text
    payload = refused.json()
    assert payload["error_type"] == "DaemonOperationScopeError"
    assert payload["context"]["credential_scope"] == "inst_scope_repair"
    # The sanctioned way to add a host, and the fact card 98 was missing: the
    # bootstrap secret is still the credential AFTER it has been claimed.
    assert "bootstrap secret" in payload["message"]
    assert "after `credential claim-bootstrap`" in payload["message"]
    assert payload["repair"]["arguments"]["accepted_credentials"] == [
        "bootstrap secret",
        "daemon-scope token",
    ]


def test_a_workspace_moves_between_hosts_through_the_detach_verb(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """Card 86: the registry rollback that does exactly this had no sanctioned door.

    A worktree belongs to one host -- a UNIQUE index on (backend,
    workspace_root) -- and re-binding it named two repairs: "archive/rebuild
    that host", which is not a verb, and "choose another Git worktree", which
    splits a repository in two.
    """

    del host_client
    workspace = tmp_path / "moving-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_first_host",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    with pytest.raises(ConfigError, match="workspace detach --instance-id inst_first_host"):
        host_api.create_playbill_host(
            instance_id="inst_second_host",
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )

    detached = host_api.playbill_host_workspace_detach(
        "inst_first_host",
        workspace_attachment_authorized=True,
    )
    assert detached.status == "detached"
    assert detached.workspace_root == str(workspace.resolve())

    moved = host_api.create_playbill_host(
        instance_id="inst_second_host",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    assert moved.status == "created"
    registry = get_registry()
    assert registry.get("inst_first_host").workspace_root is None  # type: ignore[union-attr]
    assert registry.get("inst_second_host").workspace_root == str(  # type: ignore[union-attr]
        workspace.resolve()
    )

    # Repeating it is not an error; there is simply nothing left to release.
    repeated = host_api.playbill_host_workspace_detach(
        "inst_first_host",
        workspace_attachment_authorized=True,
    )
    assert repeated.status == "not_registered"
    assert repeated.workspace_root is None


def test_detaching_a_workspace_needs_the_local_socket_that_attaching_needs(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """The daemon has to be able to prove the path it is being asked about."""

    del host_client
    workspace = tmp_path / "socket-guarded-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_socket_guarded",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    with pytest.raises(ConfigError, match="local\n?\\s*Unix socket"):
        host_api.playbill_host_workspace_detach("inst_socket_guarded")

    assert get_registry().get("inst_socket_guarded").workspace_root is not None  # type: ignore[union-attr]


def test_a_detach_refuses_while_the_host_still_registers_a_declared_block(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """Card 88's one guarantee: a detach never strands a registered block.

    Nothing stops a detach from stranding declared markers except this refusal:
    the instance is never told the workspace left, so if the detach goes through
    under live registrations the page keeps markers no host owns, and that is the
    state with no repair from inside the workspace. The host's Cruxible state is
    supplied through the manager's declared testing seam, so the registration
    under test is a real declaration written by `block repin`'s service.
    """

    from cruxible_core.service.proposals.publications import service_depublish_playbill_block
    from tests.test_authoring.test_block_sync_service import _declared_claim_block

    del host_client
    workspace = tmp_path / "declared-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_publishing_host",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    declared_state = tmp_path / "declared-state"
    declared_state.mkdir()
    instance, _owner, _coordinator, _actor, _intent_id, stamp, _landed = _declared_claim_block(
        declared_state
    )
    get_playbill_manager().register("inst_publishing_host", instance)

    with pytest.raises(ConfigError) as refusal:
        host_api.playbill_host_workspace_detach(
            "inst_publishing_host",
            workspace_attachment_authorized=True,
        )

    message = str(refusal.value)
    assert f"{stamp.source_id}#{stamp.block_id}" in message
    assert "cruxible block depublish" in message
    # Refused means refused: the worktree is still this host's.
    assert get_registry().get("inst_publishing_host").workspace_root is not None  # type: ignore[union-attr]

    service_depublish_playbill_block(
        instance,
        source_id=stamp.source_id,
        block_id=stamp.block_id,
    )

    detached = host_api.playbill_host_workspace_detach(
        "inst_publishing_host",
        workspace_attachment_authorized=True,
    )
    assert detached.status == "detached"
    assert get_registry().get("inst_publishing_host").workspace_root is None  # type: ignore[union-attr]


def test_a_host_that_cannot_be_opened_refuses_a_detach_instead_of_reading_as_empty(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """An unreadable host is not a host that published nothing.

    Only "Cruxible was never initialized here" means there are no registrations
    to strand. Every other way of failing to open the host means they could not
    be READ, and treating that as an empty set would let a transient fault
    permit exactly the detach this refusal exists to prevent.
    """

    del host_client
    workspace = tmp_path / "unreadable-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha1", str(workspace)],
        check=True,
        capture_output=True,
    )
    host_api.create_playbill_host(
        instance_id="inst_unreadable_host",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    manager = get_playbill_manager()
    manager.clear()

    def _fail(instance_id: str):  # type: ignore[no-untyped-def]
        raise FormatError("persisted Cruxible trust root is malformed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(manager), "get", staticmethod(_fail))
        with pytest.raises(ConfigError, match="could not be opened"):
            host_api.playbill_host_workspace_detach(
                "inst_unreadable_host",
                workspace_attachment_authorized=True,
            )

    assert get_registry().get("inst_unreadable_host").workspace_root is not None  # type: ignore[union-attr]

    # And the one case that genuinely means "published nothing" still detaches.
    manager.clear()
    detached = host_api.playbill_host_workspace_detach(
        "inst_unreadable_host",
        workspace_attachment_authorized=True,
    )
    assert detached.status == "detached"


def test_a_decommissioned_host_reports_decommissioned_not_writable(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = "inst_decommissioned_show"
    created = host_client.post("/api/v1/runtime/instances", json={"instance_id": instance_id})
    assert created.status_code == 200, created.text
    record = get_registry().get(instance_id)
    assert record is not None
    owner = generate_client_principal_key(
        tmp_path / "decommission-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    initialized = host_client.post(
        f"/api/v1/{instance_id}/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200, initialized.text
    assert host_client.get(f"/api/v1/{instance_id}/host").json()["writable"] is True

    route = f"/api/v1/{instance_id}/instance/decommission"
    previewed = host_client.post(route, json={"reason": "superseded by a fresh host"})
    assert previewed.status_code == 200, previewed.text
    ended = host_client.post(
        route,
        json={
            "reason": "superseded by a fresh host",
            "dry_run": False,
            "at": previewed.json()["coordinate"]["git_oid"],
        },
    )
    assert ended.status_code == 200, ended.text

    shown = host_client.get(f"/api/v1/{instance_id}/host")
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["compatibility"] == "decommissioned"
    assert body["writable"] is False
    assert body["reason"]["code"] == "instance_decommissioned"
    assert "superseded by a fresh host" in body["reason"]["detail"]

    status = host_client.get("/api/v1/server/info")
    assert status.status_code == 200, status.text
    (host,) = [row for row in status.json()["hosts"] if row["instance_id"] == instance_id]
    assert host["compatibility"] == "decommissioned"
    assert host["writable"] is False


def test_detach_accepts_the_instance_admin_and_the_bootstrap_operator(
    authenticated_host_client: tuple[TestClient, str],
) -> None:
    """Both credentials that can own a host reach the detach verb on an auth-on daemon.

    Before, the instance admin was refused as daemon-scope (403) and the
    bootstrap secret was not accepted on the route at all (401), so nothing
    could detach. The TestClient is not a local-socket caller, so each accepted
    credential lands on the socket refusal, past authorization.
    """

    client, bootstrap_secret = authenticated_host_client
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}
    for instance_id in ("inst_detach_own", "inst_detach_other"):
        created = client.post(
            "/api/v1/runtime/instances",
            json={"instance_id": instance_id},
            headers=bootstrap_headers,
        )
        assert created.status_code == 200, created.text
    claimed = client.post(
        "/api/v1/inst_detach_own/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text
    scoped_headers = {"Authorization": f"Bearer {claimed.json()['token']}"}

    for headers in (scoped_headers, bootstrap_headers):
        detached = client.post("/api/v1/inst_detach_own/workspace-detach", headers=headers)
        assert detached.status_code == 400, detached.text
        assert "local Unix socket" in detached.json()["message"]

    cross_instance = client.post(
        "/api/v1/inst_detach_other/workspace-detach", headers=scoped_headers
    )
    assert cross_instance.status_code == 403, cross_instance.text
