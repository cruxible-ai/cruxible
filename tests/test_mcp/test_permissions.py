"""Tests for MCP permission modes."""

from __future__ import annotations

import asyncio
import io
import sys

import pytest
import structlog
from mcp import types as mcp_types

from cruxible_core.errors import ConfigError, PermissionDeniedError
from cruxible_core.mcp.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    check_permission,
    get_current_mode,
    init_permissions,
    request_permission_scope,
    reset_permissions,
    validate_root_dir,
    validate_tool_permissions,
)
from cruxible_core.mcp.server import create_server, validate_runtime_tools
from cruxible_core.mcp.tool_prompts import TOOL_DESCRIPTIONS

# ── PermissionMode ────────────────────────────────────────────────────


class TestPermissionMode:
    def test_default_mode_is_admin(self, monkeypatch):
        monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
        reset_permissions()
        assert init_permissions() == PermissionMode.ADMIN

    def test_read_only_from_env(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        assert init_permissions() == PermissionMode.READ_ONLY

    def test_graph_write_from_env(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        assert init_permissions() == PermissionMode.GRAPH_WRITE

    def test_governed_write_from_env(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        assert init_permissions() == PermissionMode.GOVERNED_WRITE

    def test_admin_from_env(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "admin")
        reset_permissions()
        assert init_permissions() == PermissionMode.ADMIN

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "Read_Only")
        reset_permissions()
        assert init_permissions() == PermissionMode.READ_ONLY

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "bogus")
        reset_permissions()
        with pytest.raises(ConfigError, match="bogus"):
            init_permissions()

    def test_mode_caching(self, monkeypatch):
        """Second call returns cached value even if env changes."""
        assert get_current_mode() == PermissionMode.ADMIN
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        # Without reset, still returns cached ADMIN
        assert get_current_mode() == PermissionMode.ADMIN


# ── check_permission ──────────────────────────────────────────────────


class TestCheckPermission:
    def test_read_tool_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        # Should not raise
        check_permission("cruxible_schema")
        check_permission("cruxible_state_status")
        check_permission("cruxible_state_pull_preview")
        check_permission("cruxible_plan_workflow")
        check_permission("cruxible_stats")
        check_permission("cruxible_lint")
        check_permission("cruxible_inspect_entity")
        check_permission("cruxible_inspect_entity_history")
        check_permission("cruxible_inspect_overview")
        check_permission("cruxible_render_wiki")
        check_permission("cruxible_list_snapshots")

    def test_graph_write_tool_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")

    def test_governed_write_tool_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_propose_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_run_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_constraint")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_decision_policy")

    def test_write_tools_denied_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_lock_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_apply_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_state_publish")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_state_pull_apply")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_reload_config")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_create_snapshot")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_clone_snapshot")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_snapshot")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_restore")

    def test_graph_write_tool_in_graph_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        init_permissions()
        check_permission("cruxible_add_entity")
        check_permission("cruxible_apply_workflow")

    def test_governed_write_tools_in_governed_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        check_permission("cruxible_feedback")
        check_permission("cruxible_feedback_batch")
        check_permission("cruxible_feedback_from_query")
        check_permission("cruxible_run_workflow")
        check_permission("cruxible_test_workflow")
        check_permission("cruxible_propose_workflow")
        check_permission("cruxible_add_constraint")
        check_permission("cruxible_add_decision_policy")
        check_permission("cruxible_create_snapshot")
        check_permission("cruxible_state_pull_apply")

    def test_graph_write_tools_denied_in_governed_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_resolve_group")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_apply_workflow")

    def test_admin_tool_denied_in_graph_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_lock_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_snapshot")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_restore")

    def test_admin_tool_in_admin(self):
        check_permission("cruxible_lock_workflow")
        check_permission("cruxible_reload_config")
        check_permission("cruxible_clone_snapshot")
        check_permission("cruxible_instance_snapshot")
        check_permission("cruxible_instance_restore")
        check_permission("cruxible_state_publish")
        check_permission("cruxible_state_create_overlay")

    def test_denial_message_includes_modes(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE") as exc_info:
            check_permission("cruxible_add_entity")
        assert "READ_ONLY" in str(exc_info.value)

    def test_internal_operation_permission(self):
        """Runtime-owned internal operation gates can be stricter than public tools."""
        init_permissions(PermissionMode.READ_ONLY)
        check_permission("cruxible_init")
        with pytest.raises(PermissionDeniedError, match="ADMIN"):
            check_permission("cruxible_init_with_config")

    def test_unknown_tool_raises_config_error(self):
        """Misspelled tool name raises ConfigError, not KeyError."""
        with pytest.raises(ConfigError, match="cruxible_typo"):
            check_permission("cruxible_typo")


# ── Audit logging ────────────────────────────────────────────────────


class TestAuditLogging:
    @pytest.fixture(autouse=True)
    def capture_structlog(self):
        """Reconfigure structlog to write to a capturable StringIO buffer."""
        self._log_buffer = io.StringIO()
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.dev.ConsoleRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(file=self._log_buffer),
            cache_logger_on_first_use=False,
        )
        yield
        # Restore safe stderr default
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.add_log_level,
                structlog.dev.ConsoleRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=False,
        )

    def test_mutation_logged(self):
        """Calling check_permission for a GRAPH_WRITE tool emits structlog event."""
        check_permission("cruxible_add_entity", instance_id="test-instance")
        output = self._log_buffer.getvalue()
        assert "mutation_allowed" in output

    def test_read_not_logged(self):
        """Calling check_permission for a READ_ONLY tool emits no mutation event."""
        check_permission("cruxible_schema")
        output = self._log_buffer.getvalue()
        assert "mutation_allowed" not in output

    def test_denial_logged_as_warning(self, monkeypatch):
        """Blocked call emits warning-level log."""
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")
        output = self._log_buffer.getvalue()
        assert "permission_denied" in output


# ── Validation ────────────────────────────────────────────────────────


class TestValidation:
    def test_validate_exact_match_succeeds(self):
        validate_tool_permissions(list(TOOL_PERMISSIONS.keys()))

    def test_validate_missing_permission_raises(self):
        tools = list(TOOL_PERMISSIONS.keys()) + ["cruxible_new_tool"]
        with pytest.raises(ConfigError, match="cruxible_new_tool"):
            validate_tool_permissions(tools)

    def test_validate_stale_permission_raises(self):
        tools = [t for t in TOOL_PERMISSIONS if t != "cruxible_init"]
        with pytest.raises(ConfigError, match="cruxible_init"):
            validate_tool_permissions(tools)

    def test_tool_permissions_matches_fastmcp(self):
        """Permission map matches actual FastMCP tool registrations."""
        server = create_server()
        tools = asyncio.run(server.list_tools())
        actual = {t.name for t in tools}
        assert actual == set(TOOL_PERMISSIONS.keys())

    def test_tools_list_filters_by_read_only_mode(self, monkeypatch):
        """READ_ONLY sessions only advertise callable read tools."""
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()

        server = create_server()
        tools = asyncio.run(server.list_tools())
        actual = {tool.name for tool in tools}

        assert actual
        assert all(TOOL_PERMISSIONS[name] <= PermissionMode.READ_ONLY for name in actual)
        assert "cruxible_query" in actual
        assert "cruxible_batch_direct_write" not in actual
        assert "cruxible_lock_workflow" not in actual
        validate_runtime_tools(server)

    def test_tools_list_filters_by_profile(self, monkeypatch):
        """MCP profiles advertise a focused subset without changing registrations."""
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "review")
        reset_permissions()

        server = create_server()
        actual = {tool.name for tool in asyncio.run(server.list_tools())}

        assert "cruxible_query" in actual
        assert "cruxible_feedback" in actual
        assert "cruxible_batch_direct_write" not in actual
        assert "cruxible_state_publish" not in actual
        validate_runtime_tools(server)

    def test_tools_list_filters_by_state_authoring_profile(self, monkeypatch):
        """State authoring profile exposes graph/workflow tools but not review tools."""
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
        reset_permissions()

        server = create_server()
        actual = {tool.name for tool in asyncio.run(server.list_tools())}

        assert "cruxible_query" in actual
        assert "cruxible_batch_direct_write" in actual
        assert "cruxible_add_relationship" in actual
        assert "cruxible_apply_workflow" in actual
        assert "cruxible_feedback" not in actual
        assert "cruxible_propose_group" not in actual
        assert "cruxible_state_publish" not in actual
        validate_runtime_tools(server)

    def test_tools_list_filters_by_explicit_allowlist(self, monkeypatch):
        """Explicit allowlists produce the smallest intended catalog."""
        monkeypatch.setenv(
            "CRUXIBLE_MCP_TOOLS",
            "cruxible_query,cruxible_get_entity",
        )
        reset_permissions()

        server = create_server()
        actual = {tool.name for tool in asyncio.run(server.list_tools())}

        assert actual == {"cruxible_query", "cruxible_get_entity"}
        validate_runtime_tools(server)

    def test_protocol_tools_list_filters_by_mode_profile_and_allowlist(self, monkeypatch):
        """Low-level MCP tools/list handler applies the advertised catalog filter."""
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
        monkeypatch.setenv(
            "CRUXIBLE_MCP_TOOLS",
            "cruxible_query,cruxible_batch_direct_write,cruxible_lock_workflow,cruxible_feedback",
        )
        reset_permissions()

        server = create_server()
        handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
        actual = {tool.name for tool in result.root.tools}

        assert actual == {"cruxible_query", "cruxible_batch_direct_write"}
        validate_runtime_tools(server)

    def test_validate_runtime_tools_succeeds(self):
        """validate_runtime_tools runs without error from sync context."""
        server = create_server()
        validate_runtime_tools(server)

    def test_tool_prompt_descriptions_cover_every_registered_tool(self):
        """Every MCP tool has a non-coding-client prompt description."""
        server = create_server()
        tools = asyncio.run(server.list_tools())
        actual = {tool.name for tool in tools}

        assert set(TOOL_DESCRIPTIONS) == set(TOOL_PERMISSIONS)
        assert actual == set(TOOL_PERMISSIONS)
        for tool in tools:
            assert tool.description is not None
            assert tool.description.startswith("Use when ")


# ── Allowed roots ─────────────────────────────────────────────────────


class TestAllowedRoots:
    def test_allowed_roots_permits_valid_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", str(tmp_path))
        reset_permissions()
        init_permissions()
        # Should not raise
        validate_root_dir(str(tmp_path / "subdir"))

    def test_allowed_roots_blocks_invalid_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", "/opt/data")
        reset_permissions()
        init_permissions()
        with pytest.raises(ConfigError, match="not under any allowed root"):
            validate_root_dir(str(tmp_path))

    def test_allowed_roots_denial_does_not_leak_paths(self, monkeypatch, tmp_path):
        """Error message must not expose the actual allowed root paths."""
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", "/opt/secret-data")
        reset_permissions()
        init_permissions()
        with pytest.raises(ConfigError) as exc_info:
            validate_root_dir(str(tmp_path))
        assert "/opt/secret-data" not in str(exc_info.value)

    def test_allowed_roots_unset_allows_all(self, tmp_path):
        # No CRUXIBLE_ALLOWED_ROOTS set
        validate_root_dir(str(tmp_path))

    def test_allowed_roots_empty_raises(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", "")
        reset_permissions()
        with pytest.raises(ConfigError, match="set but empty"):
            init_permissions()

    def test_allowed_roots_relative_path_raises(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", "relative/path")
        reset_permissions()
        with pytest.raises(ConfigError, match="relative path"):
            init_permissions()


# ── ContextVar isolation ──────────────────────────────────────────────


class TestContextVarIsolation:
    def test_concurrent_modes_isolated(self):
        """Two async tasks with different scopes don't interfere."""
        init_permissions(PermissionMode.ADMIN)
        results: dict[str, PermissionMode] = {}

        async def task_a():
            with request_permission_scope(PermissionMode.READ_ONLY):
                await asyncio.sleep(0.01)
                results["a"] = get_current_mode()

        async def task_b():
            with request_permission_scope(PermissionMode.GRAPH_WRITE):
                await asyncio.sleep(0.01)
                results["b"] = get_current_mode()

        async def run():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(run())
        assert results["a"] == PermissionMode.READ_ONLY
        assert results["b"] == PermissionMode.GRAPH_WRITE

    def test_contextvar_fallback_to_env(self, monkeypatch):
        """No scope set → falls back to CRUXIBLE_MODE env var."""
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        assert get_current_mode() == PermissionMode.GRAPH_WRITE

    def test_contextvar_overrides_env(self, monkeypatch):
        """Scope set → takes precedence over env var; reverts after exit."""
        monkeypatch.setenv("CRUXIBLE_MODE", "admin")
        reset_permissions()
        with request_permission_scope(PermissionMode.READ_ONLY):
            assert get_current_mode() == PermissionMode.READ_ONLY
        assert get_current_mode() == PermissionMode.ADMIN

    def test_check_permission_uses_contextvar(self):
        """Within READ_ONLY scope, read tool passes, write tool raises."""
        init_permissions(PermissionMode.ADMIN)
        with request_permission_scope(PermissionMode.READ_ONLY):
            check_permission("cruxible_schema")  # should not raise
            with pytest.raises(PermissionDeniedError):
                check_permission("cruxible_add_entity")

    def test_nested_scope_restores_outer(self):
        """Inner scope exits → outer scope's mode is restored, not global default."""
        init_permissions(PermissionMode.ADMIN)
        with request_permission_scope(PermissionMode.GRAPH_WRITE):
            assert get_current_mode() == PermissionMode.GRAPH_WRITE
            with request_permission_scope(PermissionMode.READ_ONLY):
                assert get_current_mode() == PermissionMode.READ_ONLY
            # After inner scope exits, outer scope (GRAPH_WRITE) is restored
            assert get_current_mode() == PermissionMode.GRAPH_WRITE
        # After all scopes exit, global default (ADMIN) is restored
        assert get_current_mode() == PermissionMode.ADMIN
