"""Tests for MCP permission modes."""

from __future__ import annotations

import asyncio
import io
import sys

import pytest
import structlog

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

# ── PermissionMode ────────────────────────────────────────────────────


class TestPermissionMode:
    def test_default_mode_is_admin(self):
        assert get_current_mode() == PermissionMode.ADMIN

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
        check_permission("cruxible_world_status")
        check_permission("cruxible_world_pull_preview")
        check_permission("cruxible_plan_workflow")

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

    def test_admin_tool_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_ingest")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_lock_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_apply_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_world_publish")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_world_pull_apply")

    def test_graph_write_tool_in_graph_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        init_permissions()
        check_permission("cruxible_add_entity")

    def test_governed_write_tools_in_governed_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        check_permission("cruxible_feedback")
        check_permission("cruxible_feedback_batch")
        check_permission("cruxible_run_workflow")
        check_permission("cruxible_propose_workflow")

    def test_graph_write_tools_denied_in_governed_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_resolve_group")

    def test_admin_tool_in_graph_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_ingest")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_lock_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_apply_workflow")

    def test_admin_tool_in_admin(self):
        check_permission("cruxible_ingest")
        check_permission("cruxible_lock_workflow")
        check_permission("cruxible_apply_workflow")
        check_permission("cruxible_world_publish")
        check_permission("cruxible_world_create_overlay")
        check_permission("cruxible_world_pull_apply")

    def test_denial_message_includes_modes(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError, match="GRAPH_WRITE") as exc_info:
            check_permission("cruxible_add_entity")
        assert "READ_ONLY" in str(exc_info.value)

    def test_required_mode_override(self):
        """required_mode overrides TOOL_PERMISSIONS lookup."""
        # cruxible_init is READ_ONLY in TOOL_PERMISSIONS
        # But with required_mode=ADMIN, it should check against ADMIN
        init_permissions(PermissionMode.READ_ONLY)
        with pytest.raises(PermissionDeniedError, match="ADMIN"):
            check_permission(
                "cruxible_init",
                required_mode=PermissionMode.ADMIN,
            )

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

    def test_validate_runtime_tools_succeeds(self):
        """validate_runtime_tools runs without error from sync context."""
        server = create_server()
        validate_runtime_tools(server)


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
