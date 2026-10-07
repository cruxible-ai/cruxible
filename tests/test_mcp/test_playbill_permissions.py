"""Transport-tier and curation laws for the Cruxible MCP surface."""

from __future__ import annotations

import asyncio

import pytest

from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    check_permission,
    reset_permissions,
)


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(create_server().list_tools())}


_DEFAULT_PROFILE = {
    "cruxible_next",
    "cruxible_get",
    "cruxible_query",
    "cruxible_set",
    "cruxible_retire",
    "cruxible_write",
    "cruxible_proposal_list",
    "cruxible_proposal_review",
    "cruxible_proposal_approve",
    "cruxible_proposal_activate",
    "cruxible_orient",
    "cruxible_whoami",
    "cruxible_server_info",
}


def test_admin_default_profile_is_exactly_the_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()

    assert _tool_names() == _DEFAULT_PROFILE


def test_read_only_default_profile_keeps_only_its_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()

    assert _tool_names() == {
        name for name in _DEFAULT_PROFILE if TOOL_PERMISSIONS[name] == PermissionMode.READ_ONLY
    }


def test_the_write_verbs_replace_the_authoring_tools_in_the_default_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    names = _tool_names()

    assert {"cruxible_set", "cruxible_retire", "cruxible_write"} <= names
    assert not {name for name in names if name.startswith("cruxible_authoring_")}
    assert "cruxible_claim_retire" not in TOOL_PERMISSIONS
    # add stays a change of cruxible_write on MCP: no tool of its own.
    assert "cruxible_add" not in TOOL_PERMISSIONS
    for verb in ("set", "retire", "write"):
        assert TOOL_PERMISSIONS[f"cruxible_{verb}"] == PermissionMode.GOVERNED_WRITE


def test_full_profile_advertises_the_uncurated_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    reset_permissions()

    assert _tool_names() == set(TOOL_PERMISSIONS)


@pytest.mark.parametrize("profile", ["expert", "all", "state_authoring", "review"])
def test_retired_profile_names_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)

    with pytest.raises(ConfigError, match="Valid values: default, full"):
        create_server()


def test_permission_checks_fail_closed_for_unknown_and_higher_tier_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    check_permission("cruxible_body_store")
    with pytest.raises(PermissionDeniedError):
        check_permission("cruxible_proposal_approve_submit")
    with pytest.raises(PermissionDeniedError):
        check_permission("cruxible_init")
    with pytest.raises(ConfigError):
        check_permission("cruxible_evaluate")


def test_a_permission_denial_names_what_the_required_tier_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.errors import ErrorResponse, response_to_error
    from cruxible_core.server.errors import error_to_response

    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    with pytest.raises(PermissionDeniedError) as caught:
        check_permission("cruxible_proposal_approve_submit")

    message = str(caught.value)
    assert "requires GRAPH_WRITE mode" in message
    assert "plus submitting approvals and activating" in message
    status, body = error_to_response(caught.value)
    client_error = response_to_error(status, ErrorResponse.model_validate(body.model_dump()))
    assert str(client_error) == message
