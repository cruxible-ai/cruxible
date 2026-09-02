"""Shared fixtures for CLI tests.

The car-parts config and the populated legacy graph/instance fixtures built on
it left with the PC-F donor purge, together with the only suites that consumed
them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.server.registry import reset_registry


@pytest.fixture(autouse=True)
def reset_server_mode_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear server-mode env and caches between CLI tests."""
    monkeypatch.delenv("CRUXIBLE_REQUIRE_SERVER", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_client_cache()
    reset_registry()
    yield
    reset_client_cache()
    reset_registry()
