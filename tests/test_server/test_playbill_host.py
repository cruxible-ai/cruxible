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
    PlaybillBootstrapError,
    PlaybillReseedRequired,
    ProposalIntegrityError,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.workspace_file import WorkspaceFileSourceRequestV1
from cruxible_core.errors import ConfigError
from cruxible_core.playbill.keys import (
    GeneratedKeyMaterial,
    generate_client_principal_key,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.workspace_file import (
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
    workspace_binding_digest,
)
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.runtime import playbill_manager as playbill_manager_module
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry, reset_registry
from cruxible_core.server.routes.playbill import append_claim_attestation, run_procedure
from cruxible_core.service.playbill_procedure_runs import ProcedureRunRequestV2
from tests.support.provider_seed import write_workspace_seed_config
from tests.test_playbill.test_activation import _sign


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
def seeded_host_client(host_client: TestClient, tmp_path: Path) -> TestClient:
    """Host client whose daemon config pins the real adapter checkout.

    Tests that are about seeding keep the real local materialization. Without the
    sibling ``cruxible-providers`` checkout they skip naming the follow-on card
    rather than pretending the seed law was exercised.
    """

    write_workspace_seed_config(tmp_path / "server-state")
    get_playbill_manager().clear()
    return host_client


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


def test_host_show_and_server_status_inspect_uninitialized_hosts_without_writing(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    get_registry().get_or_create_local_instance(tmp_path / "unrelated-local")
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_show_empty"},
    )
    assert created.status_code == 200
    record = get_registry().get("inst_show_empty")
    assert record is not None

    shown = host_client.get("/api/v1/inst_show_empty/playbill/host")
    assert shown.status_code == 200, shown.text
    assert shown.json() == {
        "tag": "playbill-host-inspection-v1",
        "instance_id": "inst_show_empty",
        "managed_root": str(Path(record.location).resolve()),
        "workspace_root": None,
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
    assert status.json()["compiler_revision"] == "p2-b4-u2"
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
    assert row["reason"]["repair_commands"] == ["cruxible playbill host create"]


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
            "already attached to Playbill host 'inst_workspace_owner'.*"
            "before creating 'inst_workspace_other'"
        ),
    ):
        host_api.create_playbill_host(
            instance_id="inst_workspace_other",
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )
    with pytest.raises(ConfigError, match="already attached to Playbill host"):
        host_api.create_playbill_host(
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )

    registry = get_registry()
    monkeypatch.setattr(registry, "get_governed_instance_by_workspace_root", lambda _path: None)
    with pytest.raises(
        ConfigError,
        match="already attached to Playbill host 'inst_workspace_owner'",
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

    remote = host_client.get("/api/v1/inst_registration_status/playbill/workspace-registration")
    assert remote.status_code == 200, remote.text
    assert remote.json()["status"] == "registered"
    assert remote.json()["workspace_path"] is None


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
        json={"principals": [owner.principal.model_dump(mode="json")], "seed": False},
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
    other_state = tmp_path / "other-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(other_state))
    write_workspace_seed_config(other_state)

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
        json={"principals": [owner.principal.model_dump(mode="json")], "seed": False},
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
    seeded_host_client: TestClient,
    tmp_path: Path,
) -> None:
    del seeded_host_client
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
    assert first.provider_seed is not None
    assert first.provider_seed.status == "already_current"

    different_owner = generate_client_principal_key(
        tmp_path / "different-init-owner",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(Path(record.location),),
    )
    with pytest.raises(PlaybillBootstrapError, match="different principal set"):
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


def test_unconfigured_seed_refuses_init_unless_the_opt_out_is_explicit(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    """Without --no-seed the F3 refusal stands; the opt-out is never implied."""

    del host_client
    owner = _init_owner(tmp_path, "inst_seed_refusal", "seed-refusal-owner")
    with pytest.raises(ProposalIntegrityError, match="seed_materializations"):
        playbill_api.playbill_init(
            "inst_seed_refusal",
            principals=(owner.principal,),
        )

    opted_out = playbill_api.playbill_init(
        "inst_seed_refusal",
        principals=(owner.principal,),
        seed=False,
    )
    assert opted_out.provider_seed is not None
    assert opted_out.provider_seed.status == "unseeded"


def test_opting_out_of_the_seed_names_its_repair_and_writes_no_candidate(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    del host_client
    owner = _init_owner(tmp_path, "inst_unseeded", "unseeded-owner")
    first = playbill_api.playbill_init(
        "inst_unseeded",
        principals=(owner.principal,),
        seed=False,
    )

    seed_row = first.provider_seed
    assert seed_row is not None
    assert seed_row.status == "unseeded"
    assert seed_row.repair == "configure_seed_materializations_then_playbill_provider_seed"
    assert seed_row.changed_paths == ()
    assert seed_row.proposal_id is None
    assert seed_row.candidate_digest is None
    assert seed_row.approval_required is False
    assert seed_row.accepted_coordinate == first.coordinate

    instance = get_playbill_manager().get("inst_unseeded")
    assert instance.proposal_evidence().list_admissions() == ()
    assert instance.accepted_history()[-1].sequence == 0
    assert "providers/cruxible-provider-workspace.json" not in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )

    retry = playbill_api.playbill_init(
        "inst_unseeded",
        principals=(owner.principal,),
        seed=False,
    )
    assert retry == first
    assert instance.proposal_evidence().list_admissions() == ()


def test_independent_approval_init_honours_the_seed_opt_out(
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
        "seed": False,
    }

    accepted = host_client.post(f"/api/v1/{governed_id}/playbill/init", json=payload)
    assert accepted.status_code == 200, accepted.text
    seed_row = accepted.json()["provider_seed"]
    assert seed_row["status"] == "unseeded"
    assert seed_row["repair"] == "configure_seed_materializations_then_playbill_provider_seed"
    assert "proposal_id" not in seed_row and "candidate_digest" not in seed_row

    retry = host_client.post(f"/api/v1/{governed_id}/playbill/init", json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json() == accepted.json()
    instance = get_playbill_manager().get(governed_id)
    assert instance.proposal_evidence().list_admissions() == ()
    assert instance.inspect().approval_policy_mode == "independent_approval_required"


def _read_workspace_path(reader: WorkspaceFileReader, root: Path, relative_path: str) -> None:
    request = WorkspaceFileSourceRequestV1(
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
    playbill_api.playbill_init("inst_state_root_denied", principals=(owner.principal,), seed=False)

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

    def service(*_args: object, **kwargs: object) -> None:
        assert kwargs["workspace_file_reader"] is None
        raise ServiceReached

    monkeypatch.setattr(playbill_api, "service_run_playbill_procedure", service)
    with pytest.raises(ServiceReached):
        playbill_api.playbill_procedure_run(
            "inst_unavailable_workspace",
            "non-workspace-procedure",
            request=ProcedureRunRequestV2(input={}),
        )


def test_workspace_attachment_after_init_names_archive_and_rebuild_repair(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
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
        seed=False,
    )
    workspace = tmp_path / "late-workspace"
    subprocess.run(["git", "init", "-b", "main", str(workspace)], check=True, capture_output=True)

    with pytest.raises(
        ConfigError,
        match="inst_unattached_initialized.*archive/rebuild.*before init.*re-seed",
    ):
        host_api.create_playbill_host(
            instance_id="inst_unattached_initialized",
            workspace_root=str(workspace),
            workspace_attachment_authorized=True,
        )


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
        seed=False,
    )

    assert get_playbill_manager().get("inst_attached").descriptor.git_object_format == "sha1"
    assert initialized.workspace_advertisement.status == "updated"
    assert initialized.workspace_advertisement.advertised_refs == (
        "refs/remotes/playbill/accepted",
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
    assert proposed.workspace_advertisement.advertised_refs == (
        "refs/remotes/playbill/accepted",
        f"refs/remotes/playbill/proposals/{proposal_key}",
    )
    remote_branches = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--remotes", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert remote_branches == [
        "refs/remotes/playbill/accepted",
        f"refs/remotes/playbill/proposals/{proposal_key}",
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
    instance = get_playbill_manager().get("inst_attached")
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
    assert activated.workspace_advertisement.advertised_refs == ("refs/remotes/playbill/accepted",)
    assert subprocess.run(
        ["git", "-C", str(workspace), "branch", "--remotes", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines() == ["refs/remotes/playbill/accepted"]
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
        seed=False,
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
            seed=False,
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
        seed=False,
    )

    assert initialized.workspace_advertisement.status == "failed"
    assert initialized.workspace_advertisement.failure_code == "unexpected_failure"
    assert get_playbill_manager().get("inst_raising_advertiser") is not None


def test_independent_approval_init_requires_and_accepts_a_second_ordinary_principal(
    seeded_host_client: TestClient,
    tmp_path: Path,
) -> None:
    host_client = seeded_host_client
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
    retry = host_client.post(
        f"/api/v1/{governed_id}/playbill/init",
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
    assert accepted.json()["provider_seed"]["status"] == "pending"
    assert accepted.json()["approval_policy_mode"] == "independent_approval_required"
    instance = get_playbill_manager().get(governed_id)
    assert len(instance.proposal_evidence().list_admissions()) == 1
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
        f"/api/v1/{instance_id}/playbill/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ],
            "seed": False,
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
        "/api/v1/inst_scoped_show/playbill/host",
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
        "/api/v1/inst_scoped_show/playbill/host",
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
        "/api/v1/inst_scoped_show/playbill/init",
        # Scope and path privacy, not seeding: this host takes init's explicit opt-out.
        json={"principals": [owner.principal.model_dump(mode="json")], "seed": False},
        headers=scoped_headers,
    )
    assert initialized.status_code == 200, initialized.text

    own = client.get(
        "/api/v1/inst_scoped_show/playbill/host",
        headers=scoped_headers,
    )
    assert own.status_code == 200, own.text
    assert own.json()["managed_root"] is None
    assert own.json()["compatibility"] == "writable"

    cross_instance = client.get(
        "/api/v1/inst_other_show/playbill/host",
        headers=scoped_headers,
    )
    assert cross_instance.status_code == 403, cross_instance.text
