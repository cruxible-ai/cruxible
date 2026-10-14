"""MCP reaches next the way the CLI and SDK do."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.mcp import handlers
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode


def test_next_observes_the_mcp_workspace_and_stamps_the_default_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    sent: dict[str, Any] = {}
    answer = object()

    class StubClient:
        def next(self, instance_id: str, **kwargs: Any) -> object:
            sent.update(kwargs, instance_id=instance_id)
            return answer

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    result = handlers.handle_playbill_next("inst_test", since_result_digest="sha256:" + "a" * 64)

    assert result is answer
    assert sent["instance_id"] == "inst_test"
    assert sent["access_profile"]["permitted_access_classes"] == ["instance", "public"]
    assert sent["evaluation_time"].endswith("Z")
    assert sent["workspace_observation"]["tag"] == "playbill-next-workspace-observation-v1"
    assert sent["since_result_digest"] == "sha256:" + "a" * 64
    assert sent["limit"] is None and sent["cursor"] is None


def test_next_is_a_read() -> None:
    assert TOOL_PERMISSIONS["cruxible_next"] == PermissionMode.READ_ONLY
    assert "cruxible_authoring_rebase" not in TOOL_PERMISSIONS
