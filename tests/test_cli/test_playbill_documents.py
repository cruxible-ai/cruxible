"""PB-E CLI canonical Document read parity."""

from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.playbill.coverage.middleware import CoverageWorkspaceConfigV2

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_allocates_and_remembers_a_playbill_host(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    class StubClient:
        def create_playbill_host(
            self, *, instance_id: str | None = None
        ) -> contracts.PlaybillHostResult:
            assert instance_id == "inst_cli_host"
            return contracts.PlaybillHostResult(instance_id=instance_id, status="created")

    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_cli_host",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"instance_id": "inst_cli_host"' in result.stdout
    assert '"instance_id": "inst_cli_host"' in context_path.read_text()


def test_cli_init_remembers_the_initialized_instance(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    class StubClient:
        def init_playbill(
            self,
            instance_id: str,
            *,
            principals: list[dict[str, object]],
            operating_profile: str,
            require_independent_approval: bool,
            seed: bool = True,
            git_object_format: str | None = None,
        ) -> contracts.PlaybillInitResult:
            assert instance_id == "inst_cli_init"
            assert principals[0]["principal_id"] == "operator"
            assert operating_profile == "local"
            assert require_independent_approval is False
            assert seed is True
            return contracts.PlaybillInitResult(
                instance_id=instance_id,
                coordinate=COORDINATE,
                trust_root={},
                recovery_posture="normal",
                approval_policy_mode="self_approval_allowed",
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
            )

    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli_init",
            "playbill",
            "init",
            "--key-dir",
            str(tmp_path / "custody"),
            "--principal-id",
            "operator",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(context_path.read_text())["instance_id"] == "inst_cli_init"


def test_cli_init_writes_an_explicit_remote_workspace_config(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace)],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(tmp_path)

    class StubClient:
        def init_playbill(
            self,
            instance_id: str,
            *,
            principals: list[dict[str, object]],
            operating_profile: str,
            require_independent_approval: bool,
            workspace_root: str | None = None,
            seed: bool = True,
            git_object_format: str | None = None,
        ) -> contracts.PlaybillInitResult:
            assert workspace_root is None
            return contracts.PlaybillInitResult(
                instance_id=instance_id,
                coordinate=COORDINATE,
                trust_root={},
                recovery_posture="normal",
                approval_policy_mode="self_approval_allowed",
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
            )

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.example.test",
            "--instance-id",
            "inst_cli_init",
            "playbill",
            "init",
            "--workspace",
            str(workspace),
            "--key-dir",
            str(tmp_path / "custody"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    config = json.loads((workspace / ".playbill" / "coverage.json").read_text())
    assert config["server_url"] == "https://playbill.example.test"
    assert config["instance_id"] == "inst_cli_init"
    assert config["floor_output"]["format"] == "playbill-floor-export-v2"


def test_unix_socket_host_attach_uses_the_containing_git_worktree(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace)],
        check=True,
        capture_output=True,
    )
    nested = workspace / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    calls: list[str | None] = []

    class StubClient:
        def create_playbill_host(
            self,
            *,
            instance_id: str | None = None,
            workspace_root: str | None = None,
        ) -> contracts.PlaybillHostResult:
            calls.append(workspace_root)
            return contracts.PlaybillHostResult(instance_id=instance_id or "inst", status="created")

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(tmp_path / "cruxible.sock"),
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_socket",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [str(workspace.resolve())]
    config = json.loads((workspace / ".playbill" / "coverage.json").read_text())
    assert config["server_socket"] == str(tmp_path / "cruxible.sock")
    assert config["instance_id"] == "inst_socket"
    assert "server_url" not in config
    assert "token" not in json.dumps(config).casefold()


def test_explicit_remote_workspace_config_never_sends_the_client_path(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace)],
        check=True,
        capture_output=True,
    )

    class StubClient:
        def create_playbill_host(
            self,
            *,
            instance_id: str | None = None,
            workspace_root: str | None = None,
        ) -> contracts.PlaybillHostResult:
            assert workspace_root is None
            return contracts.PlaybillHostResult(instance_id=instance_id or "inst", status="created")

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.example.test",
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_remote",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    config = json.loads((workspace / ".playbill" / "coverage.json").read_text())
    CoverageWorkspaceConfigV2.model_validate(config)
    assert config == {
        "floor_output": {
            "format": "playbill-floor-export-v2",
            "tag": "playbill-floor-output-v1",
        },
        "instance_id": "inst_remote",
        "server_url": "https://playbill.example.test",
        "tag": "playbill-coverage-workspace-config-v2",
    }


def test_cli_lists_documents_with_their_canonical_coordinate(monkeypatch) -> None:
    class StubClient:
        def list_playbill_documents(self, instance_id: str) -> contracts.PlaybillDocumentList:
            assert instance_id == "inst_cli"
            return contracts.PlaybillDocumentList(
                coordinate=COORDINATE,
                documents=[
                    contracts.PlaybillDocumentView(
                        coordinate=COORDINATE,
                        envelope={
                            "identity": "document:design",
                            "path": "documents/design.json",
                        },
                        facts=[],
                    )
                ],
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "playbill",
            "document",
            "list",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "document:design  documents/design.json" in result.stdout
    assert f"Coordinate: {COORDINATE.git_oid}" in result.stdout


def test_document_example_is_local_and_model_constructed(monkeypatch) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("example must not call the daemon")),
    )
    result = CliRunner().invoke(
        cli,
        ["playbill", "document", "propose", "--example", "document"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["identity"] == "document:replace-me"
    assert payload["body_digest"] == "sha256:" + "0" * 64
    assert payload["governance_scope"]
    assert payload["lifecycle"]["revision"] == 1
