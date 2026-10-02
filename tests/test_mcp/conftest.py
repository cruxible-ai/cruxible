"""Shared isolation fixtures for Playbill MCP tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry


@pytest.fixture(autouse=True)
def reset_mcp_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in (
        "CRUXIBLE_MODE",
        "CRUXIBLE_REQUIRE_SERVER",
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_SERVER_TOKEN",
        "CRUXIBLE_SERVER_AUTH",
        "CRUXIBLE_MCP_PROFILE",
        "CRUXIBLE_MCP_TOOLS",
        "CRUXIBLE_MCP_TOOL_ALLOWLIST",
        "CRUXIBLE_INSTANCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / ".server-state"))
    reset_client_cache()
    reset_permissions()
    reset_registry()
    # The credential store caches the state root it first saw; a store left by an
    # earlier test points at that test's deleted temporary directory.
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    yield
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()
    reset_client_cache()
