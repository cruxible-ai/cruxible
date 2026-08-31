"""Transport-tier and curation laws for the Playbill MCP surface."""

from __future__ import annotations

import asyncio

import pytest

from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, check_permission, reset_permissions


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(create_server().list_tools())}


def test_read_only_default_profile_advertises_curated_reads_and_hides_legacy_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    names = _tool_names()

    assert "cruxible_playbill_authoring_status" in names
    assert "cruxible_playbill_authoring_example" in names
    assert "cruxible_playbill_authoring_bind" not in names
    assert "cruxible_playbill_search" in names
    assert "cruxible_playbill_since" in names
    assert "cruxible_playbill_export_floor" in names
    assert "cruxible_playbill_whoami" in names
    assert "cruxible_playbill_proposal_list" in names
    assert "cruxible_playbill_list_claims" not in names
    assert "cruxible_playbill_get_claim" not in names
    assert "cruxible_playbill_get_claim_type" not in names
    assert "cruxible_playbill_get_document" not in names
    assert "cruxible_playbill_authoring_compile" not in names
    assert "cruxible_playbill_store_body" not in names
    assert "cruxible_playbill_submit_approval" not in names
    assert "cruxible_playbill_init" not in names


def test_admin_default_profile_is_exactly_the_writer_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()

    assert _tool_names() == {
        "cruxible_version",
        "cruxible_server_info",
        "cruxible_playbill_authoring_create",
        "cruxible_playbill_authoring_example",
        "cruxible_playbill_authoring_get",
        "cruxible_playbill_authoring_resume",
        "cruxible_playbill_authoring_list_pending",
        "cruxible_playbill_authoring_compile",
        "cruxible_playbill_authoring_bind",
        "cruxible_playbill_authoring_preflight",
        "cruxible_playbill_authoring_submit",
        "cruxible_playbill_authoring_status",
        "cruxible_playbill_authoring_prepare_publication",
        "cruxible_playbill_authoring_confirm_insertion",
        "cruxible_playbill_authoring_abandon_insertion",
        "cruxible_playbill_discover",
        "cruxible_playbill_search",
        "cruxible_playbill_since",
        "cruxible_playbill_curation_list",
        "cruxible_playbill_audit",
        "cruxible_playbill_curation_overrule",
        "cruxible_playbill_curation_accept_fixed",
        "cruxible_playbill_curation_suppress",
        "cruxible_playbill_expand",
        "cruxible_playbill_source_context",
        "cruxible_playbill_resolve_coverage",
        "cruxible_playbill_workspace_source_compile",
        "cruxible_playbill_workspace_source_check",
        "cruxible_playbill_workspace_coverage_resolve",
        "cruxible_playbill_workspace_coverage_status",
        "cruxible_playbill_seed_plan",
        "cruxible_playbill_export_floor",
        "cruxible_playbill_workspace_floor_export",
        "cruxible_playbill_workspace_floor_status",
        "cruxible_playbill_whoami",
        "cruxible_playbill_proposal_list",
        "cruxible_playbill_proposal_readmit",
        "cruxible_playbill_policies_in_force",
        "cruxible_playbill_claim_type_migrate",
        "cruxible_playbill_claim_retire",
        "cruxible_playbill_claim_attest",
        "cruxible_playbill_claim_attest_new_capture",
        "cruxible_playbill_submit_approval",
        "cruxible_playbill_activate",
    }


@pytest.mark.parametrize("profile", ["full", "all", "expert"])
def test_expert_profile_aliases_advertise_the_uncurated_surface(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
    reset_permissions()

    names = _tool_names()

    assert names == set(TOOL_PERMISSIONS)


def test_state_authoring_and_review_profiles_separate_proposal_from_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    reset_permissions()
    authoring = _tool_names()
    assert "cruxible_playbill_propose_document" in authoring
    assert "cruxible_playbill_authoring_compile" in authoring
    assert "cruxible_playbill_authoring_bind" in authoring
    assert "cruxible_playbill_claim_type_migrate" in authoring
    assert "cruxible_playbill_claim_retire" in authoring
    assert "cruxible_playbill_proposal_readmit" in authoring
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
