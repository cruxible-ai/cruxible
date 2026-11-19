"""Shared fixtures for CLI tests.

The car-parts config and the populated legacy graph/instance fixtures built on
it left with the PC-F donor purge, together with the only suites that consumed
them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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


@pytest.fixture(autouse=True)
def world_stub_needs_a_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Init and attach write the world stub from vocabulary reads only a real client serves.

    A CLI fake answers none of them (orient, get_batch), so against a fake the
    stub step writes a placeholder; the real render is covered by the server
    and floor-delivery tests.
    """

    from cruxible_client import CruxibleClient
    from cruxible_client.authoring.world_stub import write_world_stub
    from cruxible_core.cli.commands import _common, playbill

    real = playbill._write_world_stub

    def write(workspace: Any, instance_id: str) -> None:
        if isinstance(_common._get_client(), CruxibleClient):
            real(workspace, instance_id)
            return
        write_world_stub(workspace, "# playbill-world-stub-v2: written against a CLI fake\n")

    monkeypatch.setattr(playbill, "_write_world_stub", write)
