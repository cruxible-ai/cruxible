"""Tests for server startup configuration validation."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.server import app as server_app
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    get_server_token,
    validate_server_startup_settings,
)
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry


def test_default_localhost_without_auth_is_valid() -> None:
    validate_server_startup_settings({})


def test_loopback_ipv6_without_auth_is_valid() -> None:
    validate_server_startup_settings({"CRUXIBLE_HOST": "::1"})


def test_public_bind_without_auth_fails() -> None:
    with pytest.raises(ConfigError, match="non-loopback host without auth"):
        validate_server_startup_settings({"CRUXIBLE_HOST": "0.0.0.0"})


def test_public_bind_with_auth_and_token_is_valid() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_HOST": "0.0.0.0",
            "CRUXIBLE_SERVER_AUTH": "true",
            "CRUXIBLE_SERVER_TOKEN": "secret",
        }
    )


def test_auth_enabled_without_token_fails() -> None:
    with pytest.raises(ConfigError, match="requires CRUXIBLE_SERVER_TOKEN"):
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


def test_auth_enabled_with_blank_token_fails() -> None:
    with pytest.raises(ConfigError, match="requires CRUXIBLE_SERVER_TOKEN"):
        validate_server_startup_settings(
            {
                "CRUXIBLE_SERVER_AUTH": "true",
                "CRUXIBLE_SERVER_TOKEN": "   ",
            }
        )


def test_server_socket_skips_public_bind_check() -> None:
    validate_server_startup_settings(
        {
            "CRUXIBLE_SERVER_SOCKET": "/tmp/cruxible.sock",
            "CRUXIBLE_HOST": "0.0.0.0",
        }
    )


def test_get_server_token_strips_whitespace() -> None:
    assert get_server_token({"CRUXIBLE_SERVER_TOKEN": "  secret  "}) == "secret"
    assert get_server_token({"CRUXIBLE_SERVER_TOKEN": "   "}) is None


def test_get_runtime_bootstrap_secret_strips_whitespace() -> None:
    assert (
        get_runtime_bootstrap_secret({"CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "  bootstrap-secret  "})
        == "bootstrap-secret"
    )
    assert get_runtime_bootstrap_secret({"CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "   "}) is None


def test_main_fails_before_uvicorn_for_public_bind_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="non-loopback host without auth"):
        server_app.main()


def test_main_reaches_uvicorn_for_valid_public_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.setenv("CRUXIBLE_PORT", "8123")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_SERVER_TOKEN", "secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    server_app.main()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8123


def test_main_reaches_uvicorn_with_stored_runtime_credentials(
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
        server_app.main()
    finally:
        reset_registry()
        reset_runtime_credential_store()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8124


def test_main_reaches_uvicorn_with_runtime_bootstrap_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_HOST", "0.0.0.0")
    monkeypatch.setenv("CRUXIBLE_PORT", "8125")
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture_run))

    server_app.main()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8125
