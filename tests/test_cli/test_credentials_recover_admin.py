"""CLI tests for local runtime admin credential recovery."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import cruxible_core.cli.commands.credentials as credential_commands
from cruxible_core.cli.main import cli
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    yield
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()


def _seed_admin_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_count: int = 1,
) -> tuple[Path, list[str]]:
    state_dir = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(state_dir))
    reset_registry()
    reset_runtime_credential_store()
    instance_ids: list[str] = []
    for index in range(instance_count):
        workspace_root = tmp_path / f"workspace-{index}"
        workspace_root.mkdir()
        registered = get_registry().create_governed_instance(workspace_root=workspace_root)
        get_runtime_credential_store().create_credential(
            instance_id=registered.record.instance_id,
            label=f"existing-admin-{index}",
            permission_mode=PermissionMode.ADMIN,
            created_by="runtime_bootstrap",
        )
        instance_ids.append(registered.record.instance_id)
    return state_dir, instance_ids


def _credential_rows(state_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(state_dir / "runtime_credentials.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                """
                SELECT credential_id, instance_id, label, permission_mode, created_by
                FROM runtime_credentials
                ORDER BY created_at, credential_id
                """
            ).fetchall()
        )
    finally:
        conn.close()


def _recovery_event_rows(state_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(state_dir / "runtime_credentials.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                """
                SELECT created_at, instance_id, credential_id, uid, hostname
                FROM runtime_recovery_events
                ORDER BY created_at, credential_id
                """
            ).fetchall()
        )
    finally:
        conn.close()


def test_recover_admin_mints_new_admin_and_records_audit(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    state_dir, (instance_id,) = _seed_admin_state(tmp_path, monkeypatch)

    result = runner.invoke(
        cli,
        ["credential", "recover-admin", "--state-dir", str(state_dir), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token"].startswith("crt_")
    assert payload["existing_credentials_revoked"] is False
    assert payload["credential"] == {
        "credential_id": payload["credential"]["credential_id"],
        "instance_id": instance_id,
        "label": "recovered-admin",
        "permission_mode": "admin",
        "created_at": payload["credential"]["created_at"],
        "created_by": "local_recovery",
        "revoked_at": None,
    }

    records = get_runtime_credential_store().list_for_instance(instance_id)
    assert len(records) == 2
    assert [record.created_by for record in records] == ["runtime_bootstrap", "local_recovery"]
    assert [record.permission_mode for record in records] == [
        PermissionMode.ADMIN,
        PermissionMode.ADMIN,
    ]

    rows = _credential_rows(state_dir)
    recovered = rows[-1]
    assert recovered["created_by"] == "local_recovery"
    assert recovered["permission_mode"] == "admin"

    events = _recovery_event_rows(state_dir)
    assert len(events) == 1
    assert events[0]["instance_id"] == instance_id
    assert events[0]["credential_id"] == payload["credential"]["credential_id"]
    assert events[0]["uid"] == os.getuid()
    assert events[0]["hostname"]


def test_recover_admin_refuses_non_owner_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    state_dir, _ = _seed_admin_state(tmp_path, monkeypatch)
    real_stat = os.stat

    def fake_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        stat_result = real_stat(path, follow_symlinks=follow_symlinks)
        values = list(stat_result)
        values[4] = 999_999
        return os.stat_result(values)

    monkeypatch.setattr(credential_commands.os, "getuid", lambda: 123_456)
    monkeypatch.setattr(credential_commands.os, "stat", fake_stat)

    result = runner.invoke(
        cli,
        ["credential", "recover-admin", "--state-dir", str(state_dir), "--json"],
    )

    assert result.exit_code == 2
    assert "State dir must be owned by invoking uid 123456" in result.output
    assert len(_credential_rows(state_dir)) == 1
    assert _recovery_event_rows(state_dir) == []


def test_recover_admin_requires_instance_id_for_multi_instance_db(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    state_dir, instance_ids = _seed_admin_state(tmp_path, monkeypatch, instance_count=2)

    result = runner.invoke(
        cli,
        ["credential", "recover-admin", "--state-dir", str(state_dir)],
    )

    assert result.exit_code == 2
    assert "Credentials DB contains multiple instance IDs; pass --instance-id." in result.output
    for instance_id in instance_ids:
        assert instance_id in result.output
    assert len(_credential_rows(state_dir)) == 2
    assert _recovery_event_rows(state_dir) == []


def test_recover_admin_refuses_busy_credentials_db(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    state_dir, _ = _seed_admin_state(tmp_path, monkeypatch)
    lock_conn = sqlite3.connect(state_dir / "runtime_credentials.db")
    lock_conn.execute("BEGIN IMMEDIATE")
    try:
        result = runner.invoke(
            cli,
            ["credential", "recover-admin", "--state-dir", str(state_dir)],
        )
    finally:
        lock_conn.rollback()
        lock_conn.close()

    assert result.exit_code == 2
    assert "Runtime credentials DB is locked" in result.output
    assert "Stop the Cruxible daemon" in result.output
    assert len(_credential_rows(state_dir)) == 1
    assert _recovery_event_rows(state_dir) == []


def test_recover_admin_refuses_server_mode_invocation(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "credential",
            "recover-admin",
            "--state-dir",
            str(tmp_path / "server-state"),
        ],
    )

    assert result.exit_code == 2
    assert "credential recover-admin is local-only" in result.output
    assert "--server-url/--server-socket" in result.output


def test_recovered_admin_token_authenticates_against_server(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    state_dir, _ = _seed_admin_state(tmp_path, monkeypatch)
    result = runner.invoke(
        cli,
        ["credential", "recover-admin", "--state-dir", str(state_dir), "--json"],
    )
    assert result.exit_code == 0, result.output
    token = json.loads(result.output)["token"]

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
