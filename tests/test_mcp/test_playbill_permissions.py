"""Transport-tier and curation laws for the Playbill MCP surface."""

from __future__ import annotations

import asyncio

import pytest

from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import check_permission, reset_permissions


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(create_server().list_tools())}


def test_read_only_advertises_reads_but_hides_all_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    names = _tool_names()

    assert "cruxible_playbill_get_document" in names
    assert "cruxible_playbill_explain" in names
    assert "cruxible_playbill_authoring_status" in names
    assert "cruxible_playbill_search" in names
    assert "cruxible_playbill_authoring_compile" not in names
    assert "cruxible_playbill_store_body" not in names
    assert "cruxible_playbill_submit_approval" not in names
    assert "cruxible_playbill_init" not in names


def test_state_authoring_and_review_profiles_separate_proposal_from_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    reset_permissions()
    authoring = _tool_names()
    assert "cruxible_playbill_propose_document" in authoring
    assert "cruxible_playbill_authoring_compile" in authoring
    assert "cruxible_playbill_authoring_submit" in authoring
    assert "cruxible_playbill_authoring_confirm_insertion" in authoring
    assert "cruxible_playbill_authoring_abandon_insertion" in authoring
    assert "cruxible_playbill_submit_approval" not in authoring
    assert "cruxible_playbill_activate" not in authoring

    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "review")
    reset_permissions()
    review = _tool_names()
    assert "cruxible_playbill_propose_document" not in review
    assert "cruxible_playbill_authoring_compile" not in review
    assert "cruxible_playbill_submit_approval" in review
    assert "cruxible_playbill_activate" in review


def test_permission_checks_fail_closed_for_unknown_and_higher_tier_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    check_permission("cruxible_playbill_store_body")
    with pytest.raises(PermissionDeniedError):
        check_permission("cruxible_playbill_submit_approval")
    with pytest.raises(PermissionDeniedError):
        check_permission("cruxible_playbill_init")
    with pytest.raises(ConfigError):
        check_permission("cruxible_query")
