from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import Playbill, contracts
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
        "CRUXIBLE_NO_WORKSPACE",
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
    socket = tmp_path / ".b2-workspace.sock"
    _attach(workspace, instance_id="inst_workspace", server_socket=str(socket))

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


@pytest.mark.parametrize(
    ("server_url", "environ"),
    (
        ("https://daemon-b.example.test", {}),
        (None, {"CRUXIBLE_SERVER_URL": "https://daemon-b.example.test"}),
    ),
)
def test_foreign_transport_cannot_inherit_a_remembered_instance(
    tmp_path: Path,
    server_url: str | None,
    environ: dict[str, str],
) -> None:
    resolved = resolve_playbill_context(
        server_url=server_url,
        remembered={
            "server_url": "https://daemon-a.example.test",
            "instance_id": "inst_of_daemon_a",
        },
        environ=environ,
        cwd=tmp_path,
    )

    assert resolved.server_url == "https://daemon-b.example.test"
    assert resolved.instance_id is None
    assert resolved.instance_source == "local"
    assert resolved.instance_transport_mismatch is not None
    assert "context_instance_transport_mismatch" in resolved.instance_transport_mismatch
    assert "https://daemon-a.example.test" in resolved.instance_transport_mismatch
    assert "https://daemon-b.example.test" in resolved.instance_transport_mismatch


def test_remembered_instance_uses_its_explicitly_recorded_transport(tmp_path: Path) -> None:
    resolved = resolve_playbill_context(
        remembered={
            "server_url": "https://daemon-a.example.test",
            "instance_id": "inst_of_daemon_b",
            "instance_transport": "https://daemon-b.example.test",
        },
        environ={},
        cwd=tmp_path,
    )

    assert resolved.server_url == "https://daemon-a.example.test"
    assert resolved.instance_id is None
    assert resolved.instance_transport_mismatch is not None


def test_workspace_walk_stops_at_home(tmp_path: Path) -> None:
    ancestor = tmp_path / "Users"
    home = ancestor / "victim"
    project = home / "code" / "unrelated-project"
    project.mkdir(parents=True)
    _attach(
        ancestor,
        instance_id="inst_attacker",
        server_url="https://attacker.example.test",
    )

    resolved = resolve_playbill_context(
        remembered={
            "server_url": "https://mine.example.test",
            "instance_id": "inst_mine",
        },
        environ={},
        cwd=project,
        home=home,
    )

    assert resolved.workspace == project
    assert resolved.server_url == "https://mine.example.test"
    assert resolved.instance_id == "inst_mine"
    assert resolved.workspace_source == "local"


def test_invalid_ancestor_binding_is_skipped_with_a_warning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "a" / "b" / "c"
    project.mkdir(parents=True)
    (home / ".playbill").mkdir()
    (home / ".playbill" / "coverage.json").write_text("{not json", encoding="utf-8")

    resolved = resolve_playbill_context(
        remembered={
            "server_url": "https://mine.example.test",
            "instance_id": "inst_mine",
        },
        environ={},
        cwd=project,
        home=home,
    )

    assert resolved.instance_id == "inst_mine"
    assert len(resolved.warnings) == 1
    assert "skipped invalid ancestor workspace binding" in resolved.warnings[0]


def test_invalid_binding_at_cwd_is_still_a_refusal(tmp_path: Path) -> None:
    (tmp_path / ".playbill").mkdir()
    (tmp_path / ".playbill" / "coverage.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(PlaybillContextResolutionError, match="not valid JSON"):
        resolve_playbill_context(environ={}, cwd=tmp_path, home=tmp_path)


def test_no_workspace_escape_uses_the_remembered_context(tmp_path: Path) -> None:
    _attach(
        tmp_path,
        instance_id="inst_workspace",
        server_url="https://workspace.example.test",
    )

    resolved = resolve_playbill_context(
        remembered={
            "server_url": "https://global.example.test",
            "instance_id": "inst_global",
        },
        environ={"CRUXIBLE_NO_WORKSPACE": "1"},
        cwd=tmp_path,
    )

    assert resolved.workspace_attached is False
    assert resolved.server_url == "https://global.example.test"
    assert resolved.instance_id == "inst_global"


def test_cli_warns_when_skipping_an_invalid_ancestor_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_target_env(monkeypatch)
    home = tmp_path / "home"
    project = home / "project" / "nested"
    project.mkdir(parents=True)
    (home / ".playbill").mkdir()
    (home / ".playbill" / "coverage.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.chdir(project)
    save_cli_context(
        CliContextState(
            server_url="https://global.example.test",
            instance_id="inst_global",
        )
    )

    result = CliRunner().invoke(cli, ["context", "show", "--json"])

    assert result.exit_code == 0, result.output
    assert "warning: skipped invalid ancestor workspace binding" in result.stderr
    assert json.loads(result.stdout)["instance_id"] == "inst_global"


def test_cli_no_workspace_option_bypasses_an_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_target_env(monkeypatch)
    _attach(
        tmp_path,
        instance_id="inst_workspace",
        server_url="https://workspace.example.test",
    )
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.chdir(tmp_path)
    save_cli_context(
        CliContextState(
            server_url="https://global.example.test",
            instance_id="inst_global",
        )
    )

    result = CliRunner().invoke(cli, ["--no-workspace", "context", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["instance_id"] == "inst_global"
    assert payload["workspace_attached"] is False


def test_disagreeing_coverage_and_catalog_roots_refuse(tmp_path: Path) -> None:
    coverage_root = tmp_path / "coverage-root"
    sources_root = coverage_root / "sources-root"
    project = sources_root / "project"
    project.mkdir(parents=True)
    _attach(
        coverage_root,
        instance_id="inst_coverage",
        server_url="https://coverage.example.test",
    )
    (sources_root / ".playbill").mkdir()
    (sources_root / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\ncatalog_kind: portable\nentries: []\n",
        encoding="utf-8",
    )

    with pytest.raises(PlaybillContextResolutionError) as raised:
        resolve_playbill_context(environ={}, cwd=project, home=tmp_path)

    message = str(raised.value)
    assert "workspace_binding_conflict" in message
    assert str(coverage_root / ".playbill" / "coverage.json") in message
    assert str(sources_root / ".playbill" / "sources.yaml") in message
    assert "repair:" in message


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


def test_context_show_reports_typed_daemon_config_disagreement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_target_env(monkeypatch)
    workspace = tmp_path / "workspace"
    socket = tmp_path / "daemon.sock"
    _attach(workspace, instance_id="inst_workspace", server_socket=str(socket))
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))

    class StubClient:
        def playbill_host_workspace_registration(
            self, instance_id: str
        ) -> contracts.PlaybillHostWorkspaceRegistrationV1:
            assert instance_id == "inst_workspace"
            return contracts.PlaybillHostWorkspaceRegistrationV1(
                instance_id=instance_id,
                status="not_registered",
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands.context._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(cli, ["context", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["workspace_config_attachment"]["status"] == "attached"
    assert payload["daemon_host_registration"]["status"] == "not_registered"
    assert payload["attachment_disagreement"] == {
        "tag": "playbill-workspace-attachment-disagreement-v1",
        "code": "daemon_registration_missing",
        "detail": "workspace config is attached but the daemon host is not registered",
    }


def test_sdk_connect_consumes_the_shared_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_target_env(monkeypatch)
    workspace = tmp_path / "workspace"
    socket = tmp_path / ".b2-workspace.sock"
    _attach(workspace, instance_id="inst_workspace", server_socket=str(socket))
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
    assert connection["socket_path"] == str(socket)
    assert playbill._instance_id == "inst_workspace"
    assert playbill._workspace == workspace
