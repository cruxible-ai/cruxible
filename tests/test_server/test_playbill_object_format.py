"""The ledger's Git object format: SHA-1 by default, inherited when attached."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_core.playbill.instance import DEFAULT_GIT_OBJECT_FORMAT, PlaybillInstance
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    with TestClient(create_app()) as client:
        yield client
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _owner(tmp_path: Path, label: str, *, managed: Path | None = None) -> object:
    return generate_client_principal_key(
        tmp_path / f"custody-{label}",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=() if managed is None else (managed,),
    ).principal


def _ledger_object_format(instance_id: str) -> str:
    instance = get_playbill_manager().get(instance_id)
    ledger = Path(instance.inspect().storage_directories["ledger"])
    completed = subprocess.run(
        ["git", f"--git-dir={ledger}", "rev-parse", "--show-object-format"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_the_library_default_is_sha1() -> None:
    """Common Git viewers do not recognize a SHA-256 repository."""

    assert DEFAULT_GIT_OBJECT_FORMAT == "sha1"


def test_an_unattached_init_writes_a_sha1_ledger(daemon: TestClient, tmp_path: Path) -> None:
    host_api.create_playbill_host(instance_id="inst_default")

    playbill_api.playbill_init(
        "inst_default",
        principals=(_owner(tmp_path, "default"),),  # type: ignore[arg-type]
        seed=False,
    )

    descriptor = get_playbill_manager().get("inst_default").descriptor
    assert descriptor.git_object_format == "sha1"
    assert _ledger_object_format("inst_default") == "sha1"


def test_an_explicit_sha256_request_is_honoured(daemon: TestClient, tmp_path: Path) -> None:
    host_api.create_playbill_host(instance_id="inst_sha256")

    playbill_api.playbill_init(
        "inst_sha256",
        principals=(_owner(tmp_path, "sha256"),),  # type: ignore[arg-type]
        seed=False,
        git_object_format="sha256",
    )

    descriptor = get_playbill_manager().get("inst_sha256").descriptor
    assert descriptor.git_object_format == "sha256"
    assert _ledger_object_format("inst_sha256") == "sha256"


def _sha256_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "sha256-workspace"
    subprocess.run(
        ["git", "init", "-b", "main", "--object-format=sha256", str(workspace)],
        check=True,
        capture_output=True,
    )
    return workspace


def test_an_attached_workspace_still_wins_over_the_default(
    daemon: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "cruxible.sock"))
    workspace = _sha256_workspace(tmp_path)
    host_api.create_playbill_host(
        instance_id="inst_inherit",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )

    playbill_api.playbill_init(
        "inst_inherit",
        principals=(_owner(tmp_path, "inherit"),),  # type: ignore[arg-type]
        workspace_attachment_authorized=True,
        seed=False,
    )

    assert get_playbill_manager().get("inst_inherit").descriptor.git_object_format == "sha256"


def test_a_format_contradicting_the_workspace_refuses_before_writing_state(
    daemon: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "cruxible.sock"))
    workspace = _sha256_workspace(tmp_path)
    created = host_api.create_playbill_host(
        instance_id="inst_conflict",
        workspace_root=str(workspace),
        workspace_attachment_authorized=True,
    )
    managed = Path(get_registry().get(created.instance_id).location)  # type: ignore[union-attr]

    with pytest.raises(PlaybillBootstrapError) as refused:
        playbill_api.playbill_init(
            "inst_conflict",
            principals=(_owner(tmp_path, "conflict"),),  # type: ignore[arg-type]
            workspace_attachment_authorized=True,
            seed=False,
            git_object_format="sha1",
        )

    assert "object_format_mismatch" in str(refused.value)
    assert "repair:" in str(refused.value)
    assert not managed.exists()


def test_a_sha256_instance_reopens_unchanged(tmp_path: Path) -> None:
    """A descriptor written before this ruling keeps its pinned format."""

    from tests.test_playbill._support import initialize_local

    instance, _owner_material = initialize_local(tmp_path, object_format="sha256")
    assert instance.descriptor.git_object_format == "sha256"

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.descriptor.git_object_format == "sha256"
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


def test_the_init_request_carries_the_format_over_the_wire() -> None:
    from cruxible_core.server.playbill_request_models import PlaybillInitRequest

    default = PlaybillInitRequest(principals=())
    explicit = PlaybillInitRequest(principals=(), git_object_format="sha256")

    assert default.git_object_format is None
    assert explicit.git_object_format == "sha256"
