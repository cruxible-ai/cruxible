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
        check_permission("cruxible_list_snapshots")

    def test_graph_write_tool_in_read_only(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")
        # Creating a snapshot moves the instance head, invalidating every
        # outstanding apply guarded on the old one — a graph-write act.
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_create_snapshot")
        # Constraints and decision policies ARE active config: they change
        # how every later query and workflow is adjudicated, which is the
        # authority reload_config carries.
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_constraint")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_decision_policy")

    def test_required_override_lowers_requirement(self, monkeypatch):
        """The direct-write facades may replace the static tier for a call."""
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        # Static requirement (graph_write) denies...
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity")
        # ...but a config-declared governed_write requirement passes.
        check_permission("cruxible_add_entity", required_override=PermissionMode.GOVERNED_WRITE)

    def test_required_override_can_still_deny(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_add_entity", required_override=PermissionMode.GRAPH_WRITE)

    def test_required_override_requires_registered_tool(self, monkeypatch):
        """The override adjusts the tier; it never bypasses tool registration."""
        monkeypatch.setenv("CRUXIBLE_MODE", "admin")
        reset_permissions()
        init_permissions()
        with pytest.raises(ConfigError):
            check_permission("cruxible_not_a_tool", required_override=PermissionMode.READ_ONLY)

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
            check_permission("cruxible_instance_backup")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_restore")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_relocate")

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

    def test_state_pull_apply_requires_admin(self, monkeypatch):
        """Pull-apply replaces the active config and the whole graph — ADMIN, not governed."""
        for mode in ("governed_write", "graph_write"):
            monkeypatch.setenv("CRUXIBLE_MODE", mode)
            reset_permissions()
            init_permissions()
            with pytest.raises(PermissionDeniedError):
                check_permission("cruxible_state_pull_apply")

        monkeypatch.setenv("CRUXIBLE_MODE", "admin")
        reset_permissions()
        init_permissions()
        check_permission("cruxible_state_pull_apply")

    def test_admin_tool_denied_in_graph_write(self, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
        reset_permissions()
        init_permissions()
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_lock_workflow")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_backup")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_restore")
        with pytest.raises(PermissionDeniedError):
            check_permission("cruxible_instance_relocate")

    def test_admin_tool_in_admin(self):
        check_permission("cruxible_lock_workflow")
        check_permission("cruxible_reload_config")
        check_permission("cruxible_clone_snapshot")
        check_permission("cruxible_instance_backup")
        check_permission("cruxible_instance_restore")
        check_permission("cruxible_instance_relocate")
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

    def test_batch_direct_write_audits_the_operation_actually_invoked(self, monkeypatch):
        """The audit record names batch_direct_write, not add_entity/add_relationship.

        The facade used to gate on the two component tools, so every allow and
        deny record described an operation the caller never called, while the
        registered ``cruxible_batch_direct_write`` entry was never exercised.
        """
        from cruxible_client import contracts
        from cruxible_core.runtime import api

        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        init_permissions()

        with pytest.raises(PermissionDeniedError) as denied:
            api.batch_direct_write("inst-audit", contracts.BatchDirectWritePayload())

        assert denied.value.tool_name == "cruxible_batch_direct_write"
        output = self._log_buffer.getvalue()
        assert "cruxible_batch_direct_write" in output
        assert "cruxible_add_entity" not in output
        assert "cruxible_add_relationship" not in output

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

    def test_server_instructions_explain_relationship_state_semantics(self):
        """Agents receive the relationship truth-state model without reading docs."""
        server = create_server()
        instructions = server._mcp_server.instructions

        assert "`live` includes active direct/unreviewed relationships" in instructions
        assert "`accepted` includes only relationships approved through review" in instructions
        assert "`pending` includes staged relationships awaiting review" in instructions
        assert "`reviewable` includes both live and pending relationships" in instructions
        assert "Candidate-group members are review records" in instructions
        assert "do not approve" in instructions.lower()


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


class TestConfigPathConfinement:
    """``config_path`` on the sibling entrypoints, not just ``validate``.

    ``validate`` was confined in the preceding batch, but ``init_local`` and
    ``reload_config`` take the same caller-supplied ``config_path`` and read (and
    for reload, ACTIVATE) whatever it points at. Both were unconfined, so the
    escape ``validate`` closed stayed open next door.
    """

    _CONFIG = (
        "version: '1.0'\n"
        "name: confinement\n"
        "entity_types:\n"
        "  Actor:\n"
        "    properties:\n"
        "      actor_id: {type: string, primary_key: true}\n"
        "relationships: []\n"
    )

    @staticmethod
    def _confine(monkeypatch, root):
        monkeypatch.setenv("CRUXIBLE_ALLOWED_ROOTS", str(root.resolve()))
        reset_permissions()
        init_permissions()

    def test_init_local_refuses_out_of_root_config_path(self, monkeypatch, tmp_path):
        from cruxible_core.runtime import api

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "config.yaml"
        secret.write_text(self._CONFIG)

        self._confine(monkeypatch, allowed)
        with pytest.raises(ConfigError, match="not under any allowed root"):
            api.init_local(str(allowed / "inst"), config_path=str(secret))

    def test_init_local_refusal_is_identical_for_a_missing_config_path(self, monkeypatch, tmp_path):
        """No oracle: the refusal does not reveal whether the file is there."""
        from cruxible_core.runtime import api

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        present = outside / "config.yaml"
        present.write_text(self._CONFIG)
        absent = outside / "absent.yaml"

        self._confine(monkeypatch, allowed)

        def _message(path):
            with pytest.raises(ConfigError) as excinfo:
                api.init_local(str(allowed / "inst"), config_path=str(path))
            return str(excinfo.value).replace(str(path), "<TARGET>")

        assert _message(present) == _message(absent)

    def test_init_local_accepts_an_in_root_config_path(self, monkeypatch, tmp_path):
        """The confinement is a fence, not a wall."""
        from cruxible_core.runtime import api
        from cruxible_core.runtime.instance_manager import get_manager

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        config = allowed / "config.yaml"
        config.write_text(self._CONFIG)

        self._confine(monkeypatch, allowed)
        get_manager().clear()
        try:
            result = api.init_local(str(allowed / "inst"), config_path=str(config))
            assert result.status == "initialized"
        finally:
            get_manager().clear()

    def test_reload_config_refuses_out_of_root_config_path(self, monkeypatch, tmp_path):
        """Repointing the ACTIVE config is the same escape with a write behind it."""
        from cruxible_core.runtime import api
        from cruxible_core.runtime.instance_manager import get_manager

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        config = allowed / "config.yaml"
        config.write_text(self._CONFIG)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "config.yaml"
        secret.write_text(self._CONFIG)

        self._confine(monkeypatch, allowed)
        get_manager().clear()
        try:
            instance = api.init_local(
                str(allowed / "inst"),
                config_path=str(config),
            )
            with pytest.raises(ConfigError, match="not under any allowed root"):
                api.reload_config(instance.instance_id, config_path=str(secret))
        finally:
            get_manager().clear()


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

    def test_contextvar_cannot_raise_process_ceiling(self, monkeypatch):
        """A request credential narrows CRUXIBLE_MODE but cannot raise it."""
        monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
        reset_permissions()
        with request_permission_scope(PermissionMode.ADMIN):
            assert get_current_mode() == PermissionMode.GOVERNED_WRITE

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


# ── Lifecycle verbs over the MCP path ─────────────────────────────────


class TestLifecycleVerbMcpPath:
    """One lifecycle verb, driven through the real MCP handler.

    The service and HTTP surfaces are covered elsewhere; this pins that the MCP
    handler is wired to the same gated facade rather than to an unguarded call —
    the tier is enforced and the required reason is enforced, over the transport
    an agent actually uses.
    """

    @staticmethod
    def _seeded_instance(tmp_path):
        from cruxible_core.cli.instance import CruxibleInstance
        from cruxible_core.graph.types import EntityInstance, RelationshipInstance, mint_claim_id
        from cruxible_core.runtime.instance_manager import get_manager
        from tests.test_cli.conftest import CAR_PARTS_YAML

        (tmp_path / "config.yaml").write_text(CAR_PARTS_YAML)
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        graph = instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1001",
                properties={"part_number": "BP-1001", "name": "Pads", "category": "brakes"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={
                    "vehicle_id": "V-1",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
        claim_id = mint_claim_id()
        graph.add_relationship(
            RelationshipInstance(
                claim_id=claim_id,
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
            )
        )
        instance.save_graph(graph)
        instance_id = str(tmp_path)
        get_manager().register(instance_id, instance)
        return instance, instance_id, claim_id

    def test_retract_claim_over_mcp_enforces_tier_and_required_reason(
        self, tmp_path, governed_client
    ):
        from cruxible_core.graph.assertion_state import relationship_assertion_from_metadata
        from cruxible_core.mcp import handlers

        instance, instance_id, claim_id = self._seeded_instance(tmp_path)

        # Below GRAPH_WRITE the verb is denied outright.
        init_permissions(PermissionMode.GOVERNED_WRITE)
        with pytest.raises(PermissionDeniedError):
            handlers.handle_retract_claim(instance_id, claim_id, "not permitted at this tier")

        reset_permissions()
        init_permissions(PermissionMode.GRAPH_WRITE)
        # At tier, an EMPTY reason is still refused: adjudication without a
        # reason is the corpus starving itself, so it is not a formatting nit.
        with pytest.raises(ConfigError, match="requires a non-empty reason"):
            handlers.handle_retract_claim(instance_id, claim_id, "   ")

        stored = instance.load_graph().find_relationship_by_claim_id(claim_id)
        assert stored is not None
        assert relationship_assertion_from_metadata(stored.metadata).lifecycle.status == "active"

        # ...and with both satisfied it settles, receipted.
        result = handlers.handle_retract_claim(instance_id, claim_id, "withdrawn by the supplier")
        assert result.action == "retract"
        assert result.receipt_id is not None
        settled = instance.load_graph().find_relationship_by_claim_id(claim_id)
        assert settled is not None
        settled_status = relationship_assertion_from_metadata(settled.metadata).lifecycle.status
        assert settled_status == "retracted"
