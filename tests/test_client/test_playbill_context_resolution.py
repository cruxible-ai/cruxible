from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import Playbill
from cruxible_client.authoring.context import (
    PlaybillContextResolutionError,
    resolve_playbill_context,
)
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_VERSION
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import cli


def _attach(
    root: Path,
    *,
    instance_id: str,
    server_url: str | None = None,
    server_socket: str | None = None,
) -> None:
    (root / ".playbill").mkdir(parents=True)
    (root / ".playbill" / "coverage.json").write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "instance_id": instance_id,
                "server_url": server_url,
                "server_socket": server_socket,
                "rules": [],
            }
        ),
        encoding="utf-8",
    )


def _clear_target_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_PLAYBILL_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)


def _catalog(root: Path) -> None:
    (root / ".playbill" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )


def test_two_cwds_resolve_their_own_workspace_before_one_global_slot(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _attach(first, instance_id="inst_first", server_url="https://first.example.test")
    _attach(second, instance_id="inst_second", server_url="https://second.example.test")
    remembered = {
        "server_url": "https://global.example.test",
        "instance_id": "inst_global",
    }

    first_result = resolve_playbill_context(
        remembered=remembered,
        environ={},
        cwd=first / "nested",
    )
    second_result = resolve_playbill_context(
        remembered=remembered,
        environ={},
        cwd=second,
    )

    assert (first_result.server_url, first_result.instance_id) == (
        "https://first.example.test",
        "inst_first",
    )
    assert (second_result.server_url, second_result.instance_id) == (
        "https://second.example.test",
        "inst_second",
    )
    assert first_result.transport_source == first_result.instance_source == "workspace"
    assert second_result.transport_source == second_result.instance_source == "workspace"


def test_target_components_use_independent_explicit_env_workspace_global_precedence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _attach(workspace, instance_id="inst_workspace", server_socket="/tmp/workspace.sock")

    resolved = resolve_playbill_context(
        server_url="https://explicit.example.test",
        workspace=workspace,
        remembered={
            "server_url": "https://global.example.test",
            "instance_id": "inst_global",
        },
        environ={"CRUXIBLE_INSTANCE_ID": "inst_environment"},
    )

    assert resolved.server_url == "https://explicit.example.test"
    assert resolved.server_socket is None
    assert resolved.instance_id == "inst_environment"
    assert resolved.transport_source == "explicit"
    assert resolved.instance_source == "environment"
    assert resolved.workspace_source == "explicit"


def test_incomplete_coverage_config_does_not_retarget_global_context(tmp_path: Path) -> None:
    (tmp_path / ".playbill").mkdir()
    (tmp_path / ".playbill" / "coverage.json").write_text(
        '{"tag":"playbill-coverage-workspace-config-v2","rules":[]}',
        encoding="utf-8",
    )

    resolved = resolve_playbill_context(
        remembered={
            "server_url": "https://global.example.test",
            "instance_id": "inst_global",
        },
        environ={},
        cwd=tmp_path,
    )

    assert not resolved.workspace_attached
    assert resolved.transport_source == resolved.instance_source == "remembered"


def test_workspace_binding_symlink_escape_names_the_selected_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.json"
    (workspace / ".playbill").mkdir(parents=True)
    outside.write_text(
        '{"server_url":"https://outside.example.test","instance_id":"inst_outside"}',
        encoding="utf-8",
    )
    os.symlink(outside, workspace / ".playbill" / "coverage.json")

    with pytest.raises(PlaybillContextResolutionError, match="selected from.*escapes workspace"):
        resolve_playbill_context(environ={}, cwd=workspace)


def test_cli_context_show_reports_environment_override_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_target_env(monkeypatch)
    workspace = tmp_path / "workspace"
    _attach(workspace, instance_id="inst_workspace", server_url="https://workspace.example.test")
    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", "https://environment.example.test")
    monkeypatch.chdir(workspace)
    save_cli_context(CliContextState(instance_id="inst_global"))

    result = CliRunner().invoke(cli, ["context", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["server_url"] == "https://environment.example.test"
    assert payload["transport_source"] == "environment"
    assert payload["instance_id"] == "inst_workspace"
    assert payload["instance_source"] == "workspace"
    assert payload["workspace"] == str(workspace)
    assert payload["workspace_attached"] is True


def test_sdk_connect_consumes_the_shared_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_target_env(monkeypatch)
    workspace = tmp_path / "workspace"
    _attach(workspace, instance_id="inst_workspace", server_socket="/tmp/workspace.sock")
    _catalog(workspace)
    context_path = tmp_path / "context.json"
    context_path.write_text(
        '{"server_url":"https://global.example.test","instance_id":"inst_global"}',
        encoding="utf-8",
    )
    connection: dict[str, object] = {}

    class StubClient:
        def __init__(self, **values: object) -> None:
            connection.update(values)

        def version(self) -> str:
            return AUTHORING_SDK_VERSION

        def close(self) -> None:
            pass

    monkeypatch.setattr("cruxible_client.authoring.sdk.CruxibleClient", StubClient)
    monkeypatch.setattr(Playbill, "refresh", lambda self: None)

    playbill = Playbill.connect(context=context_path, workspace=workspace)

    assert connection["base_url"] is None
    assert connection["socket_path"] == "/tmp/workspace.sock"
    assert playbill._instance_id == "inst_workspace"
    assert playbill._workspace == workspace
