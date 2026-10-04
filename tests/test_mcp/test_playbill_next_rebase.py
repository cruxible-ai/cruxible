"""MCP reaches next and authoring rebase the way the CLI and SDK do."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.mcp import handlers
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode
from cruxible_core.service.discovery.next import validate_playbill_next_request


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


def test_local_next_sends_a_request_the_served_route_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    requests: list[Any] = []
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_next",
        lambda instance_id, *, request: requests.append((instance_id, request)) or "queue",
    )

    assert handlers.handle_playbill_next("inst_test", limit=5) == "queue"

    ((instance_id, request),) = requests
    assert instance_id == "inst_test"
    parsed = validate_playbill_next_request(request)
    assert parsed.limit == 5
    assert parsed.workspace_observation is not None


def test_rebase_reaches_the_served_rebase_route(monkeypatch: pytest.MonkeyPatch) -> None:
    rebased: list[tuple[str, str]] = []
    view = object()

    class StubClient:
        def rebase_authoring_intent(self, instance_id: str, intent_id: str) -> object:
            rebased.append((instance_id, intent_id))
            return view

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    assert handlers.handle_playbill_authoring_rebase("inst_test", "intent-1") is view
    assert rebased == [("inst_test", "intent-1")]


def test_next_is_a_read_and_rebase_writes_at_the_preflight_tier() -> None:
    assert TOOL_PERMISSIONS["cruxible_next"] == PermissionMode.READ_ONLY
    assert (
        TOOL_PERMISSIONS["cruxible_authoring_rebase"]
        == TOOL_PERMISSIONS["cruxible_authoring_preflight"]
        == PermissionMode.GOVERNED_WRITE
    )
