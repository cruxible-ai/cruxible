"""The Provider lane precondition: a private runtime directory for the control sockets.

A daemon whose state root is too long for its control sockets, with neither
TMPDIR nor XDG_RUNTIME_DIR set (Linux containers, cron, ssh sessions), cannot
run installed Provider code. It still starts, warns once naming the cause and
the repair, `server status` keeps reporting it, and every Provider-backed
refusal carries the same cause and repair as an operator-environment refusal.
No message carries a path: a path would echo a home directory the operator did
not type.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_client.contracts.repairs import HandEditRepair
from cruxible_core.cli.main import cli
from cruxible_core.errors import ProviderLaneRefusedError
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.providers.provider_outcomes import map_provider_refusal
from cruxible_core.providers.provider_process_leases import (
    PROVIDER_RUNTIME_DIRECTORY_REPAIR,
    PROVIDER_RUNTIME_DIRECTORY_UNAVAILABLE,
    ProviderLocalRuntimeRefused,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from cruxible_core.server import app as server_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.errors import error_to_response
from cruxible_core.server.registry import reset_registry
from cruxible_core.service.procedures.provider_installation import _refuse_unavailable_lane

CODE = PROVIDER_RUNTIME_DIRECTORY_UNAVAILABLE


def _without_runtime_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Unset both per-user runtime variables; return a state root too long for sockets."""

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return tmp_path / ("long-state-root-" + "x" * 110)


def _assert_cause_and_repair(text: str) -> None:
    assert CODE in text
    assert "Unix socket limit" in text
    assert "is not set, so no private per-user runtime directory is configured" in text
    assert PROVIDER_RUNTIME_DIRECTORY_REPAIR in text
    # No path fragment: the state root, the home directory, any absolute path.
    assert "/" not in text, text
    assert "long-state-root" not in text


@pytest.fixture
def reset_daemon() -> Iterator[None]:
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_playbill_manager().clear()
    yield
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def test_the_operator_refuses_every_provider_operation_with_the_cause_and_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(_without_runtime_directory(monkeypatch, tmp_path))

    assert operator.process_leases is None
    assert operator.unavailable_code == CODE
    assert operator.environment_refusal is not None
    _assert_cause_and_repair(str(operator.environment_refusal))
    assert operator.lane_status()[:2] == ("unavailable", CODE)

    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    for call in (
        lambda: invoker.bind_provider(occurrence=object()),
        lambda: invoker.invoke_provider(),
    ):
        with pytest.raises(ProviderLocalRuntimeRefused) as caught:
            call()
        assert caught.value.code == CODE
        _assert_cause_and_repair(str(caught.value))
        assert caught.value.details["reason"] == {
            "code": CODE,
            "detail": operator.unavailable_reason,
        }

    # A Procedure or Line run's outcome: an operator-environment failure.
    outcome = map_provider_refusal(CODE, message="m", detail={})
    assert (outcome.outcome_class, outcome.attribution) == ("operational", "deployment")


def test_kit_add_refuses_typed_as_an_environment_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(_without_runtime_directory(monkeypatch, tmp_path))

    with pytest.raises(ProviderLaneRefusedError) as caught:
        _refuse_unavailable_lane(operator)

    _assert_cause_and_repair(str(caught.value))
    status, body = error_to_response(caught.value)
    assert status == 503
    assert body.error_code == CODE
    assert isinstance(body.repair, HandEditRepair)
    assert body.repair.hand_edit.required_change == PROVIDER_RUNTIME_DIRECTORY_REPAIR
    _assert_cause_and_repair(body.message)


def test_server_start_warns_naming_the_cause_and_repair_then_serves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reset_daemon: None,
) -> None:
    state_root = _without_runtime_directory(monkeypatch, tmp_path)
    served: dict[str, object] = {}
    # Recorded so the teardown removes what run_server writes into os.environ.
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "false")
    monkeypatch.setenv("CRUXIBLE_HOST", "localhost")
    monkeypatch.setenv("CRUXIBLE_PORT", "1")
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    monkeypatch.setitem(
        sys.modules, "uvicorn", SimpleNamespace(run=lambda *_a, **kw: served.update(kw))
    )

    server_app.run_server(host="127.0.0.1", port=8127, auth=True)

    # Warned, not refused: the daemon went on to serve.
    assert served["port"] == 8127
    (warning,) = (line for line in capsys.readouterr().err.splitlines() if "Provider lane" in line)
    assert warning.startswith("Warning: Provider lane unavailable")
    _assert_cause_and_repair(warning)


def test_server_status_keeps_reporting_the_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_daemon: None
) -> None:
    monkeypatch.setenv(
        "CRUXIBLE_STATE_ROOT", str(_without_runtime_directory(monkeypatch, tmp_path))
    )
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))

    with TestClient(server_app.create_app()) as transport:
        client = CruxibleClient(base_url="http://cruxible-daemon")
        client._client = transport  # type: ignore[assignment]
        monkeypatch.setattr(
            "cruxible_core.cli.commands.server._get_client", lambda state_root=None: client
        )
        result = CliRunner().invoke(
            cli, ["--server-url", "http://cruxible-daemon", "server", "status"]
        )

    assert result.exit_code == 0, result.output
    assert "Provider lane: unavailable" in result.output
    (reason,) = (
        line for line in result.output.splitlines() if line.startswith("Provider lane reason")
    )
    _assert_cause_and_repair(reason)
