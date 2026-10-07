"""Every MCP tool acts on the server's configured instance unless a call names one."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from cruxible_client import contracts
from cruxible_client.errors import DaemonOperationScopeError, ErrorResponse, response_to_error
from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.target import require_instance_id

_COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 40,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _whoami() -> contracts.WhoAmI:
    return contracts.WhoAmI(
        actor_id="agent-a",
        credential_label="agent-a",
        actor_id_source="runtime_credential",
        authenticated=True,
        credential_permission_mode="governed_write",
        principal_registration_status="active",
        active_principal_ids=["agent-a"],
        coordinate=_COORDINATE,
        can_author=True,
        authoring_refusal=None,
    )


def _host(instance_id: str) -> contracts.HostInspection:
    return contracts.HostInspection(
        instance_id=instance_id,
        managed_root=None,
        workspace_root=None,
        compatibility="writable",
        writable=True,
    )


class _ScopedClient:
    """A daemon answering an instance-scoped credential."""

    def __init__(self) -> None:
        self.whoami_calls: list[str] = []

    def server_info(self) -> contracts.ServerInfoResult:
        raise DaemonOperationScopeError("cruxible_server_info", "inst_scoped")

    def version(self) -> str:
        return "9.9.9"

    def show_host(self, instance_id: str) -> contracts.HostInspection:
        return _host(instance_id)

    def whoami(self, instance_id: str) -> contracts.WhoAmI:
        self.whoami_calls.append(instance_id)
        return _whoami()


def _call(name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    return asyncio.run(exercise())


def test_explicit_instance_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "inst_env")

    assert require_instance_id("inst_explicit") == "inst_explicit"
    assert require_instance_id() == "inst_env"


def _bind_workspace(root, **fields: str) -> None:
    (root / ".cruxible").mkdir()
    (root / ".cruxible" / "coverage.json").write_text(
        json.dumps({"tag": "playbill-coverage-workspace-config-v2", **fields}),
        encoding="utf-8",
    )


def test_no_configured_instance_names_the_repair(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ConfigError, match="CRUXIBLE_INSTANCE_ID=<instance id>"):
        require_instance_id()


def test_workspace_binding_selects_an_instance_on_the_adapters_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    socket = tmp_path / "d.sock"
    _bind_workspace(tmp_path, server_socket=str(socket), instance_id="inst_bound")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(socket))

    assert require_instance_id() == "inst_bound"
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "inst_env")
    assert require_instance_id() == "inst_env"
    assert require_instance_id("inst_explicit") == "inst_explicit"


def test_workspace_binding_on_another_daemon_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _bind_workspace(tmp_path, server_socket=str(tmp_path / "other.sock"), instance_id="inst_bound")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "d.sock"))

    with pytest.raises(ConfigError, match="selects instance inst_bound"):
        require_instance_id()
    # With no transport in its environment the adapter's daemon is the binding's.
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET")
    assert require_instance_id() == "inst_bound"


def test_an_incomplete_workspace_binding_does_not_select_an_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _bind_workspace(tmp_path, instance_id="inst_bound")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))

    with pytest.raises(ConfigError, match="No Cruxible instance selected"):
        require_instance_id()


def test_remembered_cli_context_never_retargets_the_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / "client-context.json").write_text(
        json.dumps({"instance_id": "inst_remembered"}), encoding="utf-8"
    )
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "client-context.json"))
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))

    with pytest.raises(ConfigError, match="No Cruxible instance selected"):
        require_instance_id()


def test_whoami_without_instance_id_reports_the_configured_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "inst_env")
    client = _ScopedClient()
    monkeypatch.setattr(handlers, "_get_client", lambda: client)

    is_error, text = _call("cruxible_whoami", {})

    assert not is_error, text
    payload = json.loads(text)
    assert payload["instance_id"] == "inst_env"
    assert payload["adapter_version"] == __version__
    assert payload["daemon_version"] == "9.9.9"
    assert payload["identity"]["actor_id"] == "agent-a"
    assert client.whoami_calls == ["inst_env"]


def test_instance_tool_without_any_instance_fails_with_the_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "_get_client", lambda: _ScopedClient())

    is_error, text = _call("cruxible_whoami", {})

    assert is_error
    assert "CRUXIBLE_INSTANCE_ID" in text


def test_server_info_answers_an_instance_scoped_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "_get_client", lambda: _ScopedClient())

    result = handlers.handle_server_info()

    assert result.scope == "instance"
    assert result.instance_id == "inst_scoped"
    assert (result.adapter_version, result.daemon_version) == (__version__, "9.9.9")
    assert result.daemon is None
    assert result.host is not None and result.host.instance_id == "inst_scoped"
    assert result.identity is not None and result.identity.actor_id == "agent-a"


def test_server_info_keeps_daemon_fields_for_a_daemon_scope_caller(
    monkeypatch: pytest.MonkeyPatch, mcp_daemon: object
) -> None:
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "inst_env")

    result = handlers.handle_server_info()

    assert result.scope == "daemon"
    assert result.instance_id == "inst_env"
    assert result.adapter_version == result.daemon_version == __version__
    assert result.daemon is not None
    assert result.host is None and result.identity is None


def test_client_decodes_the_daemon_scope_refusal_with_its_scope() -> None:
    exc = response_to_error(
        403,
        ErrorResponse(
            error_type="DaemonOperationScopeError",
            message="scoped",
            context={"operation": "cruxible_server_info", "credential_scope": "inst_scoped"},
        ),
    )

    assert isinstance(exc, DaemonOperationScopeError)
    assert exc.credential_scope == "inst_scoped"
    assert exc.operation == "cruxible_server_info"


def test_a_malformed_workspace_binding_selects_nothing_and_breaks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / ".cruxible").mkdir()
    (tmp_path / ".cruxible" / "coverage.json").write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ConfigError, match="No Cruxible instance selected"):
        require_instance_id()

    (tmp_path / ".cruxible" / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "server_socket": str(tmp_path / "bad\x00path.sock"),
                "instance_id": "inst_bound",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "d.sock"))
    with pytest.raises(ConfigError, match="No Cruxible instance selected"):
        require_instance_id()


def test_a_binding_socket_under_an_unknown_home_selects_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _bind_workspace(
        tmp_path, server_socket="~no_such_user_cruxible/d.sock", instance_id="inst_bound"
    )
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(tmp_path / "d.sock"))

    with pytest.raises(ConfigError, match="No Cruxible instance selected"):
        require_instance_id()
