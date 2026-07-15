"""Tests for server startup configuration validation."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.server import app as server_app
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    get_server_log_path,
    is_volatile_state_path,
    validate_server_startup_settings,
    volatile_state_path_warnings,
)
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service import service_init
from tests.test_cli.conftest import CAR_PARTS_YAML


def test_default_localhost_without_auth_is_valid() -> None:
    validate_server_startup_settings({})


def test_loopback_ipv6_without_auth_is_valid() -> None:
    validate_server_startup_settings({"CRUXIBLE_HOST": "::1"})


def test_public_bind_without_auth_fails() -> None:
    with pytest.raises(ConfigError, match="non-loopback host without auth"):
        validate_server_startup_settings({"CRUXIBLE_HOST": "0.0.0.0"})


def test_public_bind_with_auth_and_bootstrap_secret_is_valid() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_HOST": "0.0.0.0",
            "CRUXIBLE_SERVER_AUTH": "true",
            "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "bootstrap-secret",
        }
    )


def test_auth_enabled_without_bootstrap_or_credentials_fails() -> None:
    with pytest.raises(ConfigError, match="requires CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"):
        validate_server_startup_settings({"CRUXIBLE_SERVER_AUTH": "true"})


def test_auth_enabled_with_runtime_credentials_is_valid() -> None:
    validate_server_startup_settings(
        {"CRUXIBLE_SERVER_AUTH": "true"},
        runtime_credentials_available=True,
    )


def test_auth_enabled_with_runtime_bootstrap_secret_is_valid() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_SERVER_AUTH": "true",
            "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "bootstrap-secret",
        }
    )


def test_public_bind_with_runtime_credentials_is_valid() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_HOST": "0.0.0.0",
            "CRUXIBLE_SERVER_AUTH": "true",
        },
        runtime_credentials_available=True,
    )


def test_auth_enabled_with_server_token_still_fails() -> None:
    with pytest.raises(ConfigError, match="requires CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"):
        validate_server_startup_settings(
            {
                "CRUXIBLE_SERVER_AUTH": "true",
                "CRUXIBLE_SERVER_TOKEN": "legacy-secret",
            }
        )


def test_auth_required_state_without_auth_fails() -> None:
    with pytest.raises(ConfigError, match="previously required auth"):
        validate_server_startup_settings({}, auth_required=True)


def test_auth_required_state_with_auth_is_valid_when_credentials_available() -> None:
    validate_server_startup_settings(
        {"CRUXIBLE_SERVER_AUTH": "true"},
        runtime_credentials_available=True,
        auth_required=True,
    )


def test_server_socket_skips_public_bind_check() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_SERVER_SOCKET": "/tmp/cruxible.sock",
            "CRUXIBLE_HOST": "0.0.0.0",
        }
    )


def test_get_runtime_bootstrap_secret_strips_whitespace() -> None:
    assert (
        get_runtime_bootstrap_secret({"CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "  bootstrap-secret  "})
        == "bootstrap-secret"
    )
    assert get_runtime_bootstrap_secret({"CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "   "}) is None


def test_server_log_path_defaults_under_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "server-state"

    assert (
        get_server_log_path({"CRUXIBLE_SERVER_STATE_DIR": str(state_dir)})
        == (state_dir / "logs" / "server.log").resolve()
    )


def test_server_log_path_uses_explicit_override(tmp_path: Path) -> None:
    log_path = tmp_path / "runtime" / "cruxible.log"

    assert get_server_log_path({"CRUXIBLE_SERVER_LOG_PATH": str(log_path)}) == log_path.resolve()


def test_volatile_state_path_detection() -> None:
    assert is_volatile_state_path("/tmp/cruxible-state")
    assert is_volatile_state_path("/var/tmp/cruxible-state")
    assert not is_volatile_state_path(Path.home() / ".cruxible" / "server")


def test_volatile_state_path_warnings_include_state_dir_and_instances() -> None:
    warnings = volatile_state_path_warnings(
        environ={"CRUXIBLE_SERVER_STATE_DIR": "/tmp/cruxible-server"},
        instance_locations=[
            ("inst_tmp", "/tmp/cruxible-server/instances/inst_tmp"),
            ("inst_durable", str(Path.home() / ".cruxible" / "instances" / "inst_durable")),
        ],
    )

    assert len(warnings) == 2
    assert "CRUXIBLE_SERVER_STATE_DIR resolves under a volatile temp path" in warnings[0]
    assert "Instance inst_tmp is registered under a volatile temp path" in warnings[1]


def test_run_server_fails_before_uvicorn_for_public_bind_without_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_runtime_credential_store()
    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)

    try:
        with pytest.raises(ConfigError, match="non-loopback host without auth"):
            server_app.run_server()
    finally:
        reset_runtime_credential_store()


def test_run_server_reaches_uvicorn_for_valid_public_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_runtime_credential_store()
    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.setenv("CRUXIBLE_PORT", "8123")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    try:
        server_app.run_server()
    finally:
        reset_runtime_credential_store()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8123


def test_run_server_refuses_hand_edited_materialized_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_registry()
    reset_runtime_credential_store()
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)
    instance = service_init(
        Path(registered.record.location),
        config_yaml=CAR_PARTS_YAML,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    ).instance
    active = instance.get_config_path()
    active.write_text(active.read_text() + "# hand edit\n")
    monkeypatch.setenv("CRUXIBLE_HOST", "127.0.0.1")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_ALLOW_CONFIG_INTEGRITY_MISMATCH", raising=False)

    try:
        with pytest.raises(ConfigError, match="ACTIVE CONFIG WAS HAND-EDITED"):
            server_app.run_server()
    finally:
        reset_registry()
        reset_runtime_credential_store()


def test_run_server_integrity_override_allows_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_registry()
    reset_runtime_credential_store()
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)
    instance = service_init(
        Path(registered.record.location),
        config_yaml=CAR_PARTS_YAML,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    ).instance
    active = instance.get_config_path()
    active.write_text(active.read_text() + "# hand edit\n")
    monkeypatch.setenv("CRUXIBLE_HOST", "127.0.0.1")
    monkeypatch.setenv("CRUXIBLE_ALLOW_CONFIG_INTEGRITY_MISMATCH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    try:
        server_app.run_server()
    finally:
        reset_registry()
        reset_runtime_credential_store()

    assert called["host"] == "127.0.0.1"


def test_run_server_warns_for_volatile_state_dir_and_instance_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_registry()
    reset_runtime_credential_store()
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)

    monkeypatch.setenv("CRUXIBLE_HOST", "127.0.0.1")
    monkeypatch.setenv("CRUXIBLE_PORT", "8126")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    try:
        server_app.run_server()
    finally:
        reset_registry()
        reset_runtime_credential_store()

    stderr = capsys.readouterr().err
    assert "CRUXIBLE_SERVER_STATE_DIR resolves under a volatile temp path" in stderr
    assert (
        f"Instance {registered.record.instance_id} is registered under a volatile temp path"
        in stderr
    )
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8126


def test_run_server_reaches_uvicorn_with_stored_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_registry()
    reset_runtime_credential_store()
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)
    get_runtime_credential_store().create_credential(
        instance_id=registered.record.instance_id,
        label="cloud-dispatch",
    )

    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.setenv("CRUXIBLE_PORT", "8124")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    try:
        server_app.run_server()
    finally:
        reset_registry()
        reset_runtime_credential_store()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8124


def test_run_server_fails_when_runtime_credentials_exist_but_auth_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_registry()
    reset_runtime_credential_store()
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)
    get_runtime_credential_store().create_credential(
        instance_id=registered.record.instance_id,
        label="local-admin",
    )

    monkeypatch.setenv("CRUXIBLE_HOST", "127.0.0.1")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)

    try:
        with pytest.raises(ConfigError, match="previously required auth"):
            server_app.run_server()
    finally:
        reset_registry()
        reset_runtime_credential_store()


def test_run_server_reaches_uvicorn_with_runtime_bootstrap_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_runtime_credential_store()
    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.setenv("CRUXIBLE_PORT", "8125")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    try:
        server_app.run_server()
    finally:
        reset_runtime_credential_store()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8125
