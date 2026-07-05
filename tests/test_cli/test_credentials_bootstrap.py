"""CLI tests for runtime credential bootstrap and management."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def test_credential_claim_bootstrap_reads_secret_file_and_prints_token_once(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "bootstrap.secret"
    secret_file.write_text(" bootstrap-secret\n")
    captured: dict[str, str] = {}

    class StubClient:
        def claim_runtime_bootstrap(self, instance_id: str, bootstrap_secret: str):
            captured["instance_id"] = instance_id
            captured["bootstrap_secret"] = bootstrap_secret
            return contracts.RuntimeCredentialBootstrapResult(
                credential_id="rcred_bootstrap",
                instance_id=instance_id,
                permission_mode="admin",
                token="crt_rcred_bootstrap_secret",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "credential",
            "claim-bootstrap",
            "--secret-file",
            str(secret_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "instance_id": "inst_123",
        "bootstrap_secret": "bootstrap-secret",
    }
    assert "Bootstrap claimed." in result.output
    assert result.output.count("crt_rcred_bootstrap_secret") == 1
    assert "export CRUXIBLE_SERVER_BEARER_TOKEN=<token>" in result.output


def test_credential_claim_bootstrap_requires_secret(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)

    class StubClient:
        def claim_runtime_bootstrap(self, instance_id: str, bootstrap_secret: str):
            raise AssertionError("claim should not be attempted without a secret")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "credential",
            "claim-bootstrap",
        ],
    )

    assert result.exit_code == 2
    assert "Provide --secret-file or set CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET." in result.output


def test_credential_command_requires_server_mode(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["credential", "list"])

    assert result.exit_code == 2
    assert "Local mutation disabled for credential list; use server mode." in result.output


def test_credential_claim_bootstrap_second_claim_renders_refusal(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    from cruxible_client.errors import AuthenticationError

    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    class StubClient:
        def __init__(self) -> None:
            self.claims = 0

        def claim_runtime_bootstrap(self, instance_id: str, bootstrap_secret: str):
            self.claims += 1
            if self.claims > 1:
                raise AuthenticationError("Invalid bootstrap secret")
            return contracts.RuntimeCredentialBootstrapResult(
                credential_id="rcred_bootstrap",
                instance_id=instance_id,
                permission_mode="admin",
                token="crt_rcred_bootstrap_secret",
            )

    stub = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: stub)
    args = [
        "--server-url",
        "http://server",
        "--instance-id",
        "inst_123",
        "credential",
        "claim-bootstrap",
    ]

    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "Error: AuthenticationError: Invalid bootstrap secret" in second.output
    assert "Traceback" not in second.output


def test_credential_claim_bootstrap_wrong_secret_renders_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    from cruxible_client.errors import AuthenticationError

    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "wrong-secret")

    class StubClient:
        def claim_runtime_bootstrap(self, instance_id: str, bootstrap_secret: str):
            raise AuthenticationError("Invalid bootstrap secret")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "credential",
            "claim-bootstrap",
        ],
    )

    assert result.exit_code == 1
    assert "Error: AuthenticationError: Invalid bootstrap secret" in result.output
    assert "Traceback" not in result.output


def test_credential_mint_list_and_revoke_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    created_at = "2026-06-01T12:00:00Z"
    revoked_at = "2026-06-01T12:05:00Z"
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self) -> None:
            self.records: list[contracts.RuntimeCredentialMetadata] = []

        def create_runtime_credential(
            self,
            instance_id: str,
            *,
            label: str,
            permission_mode: contracts.RuntimeCredentialPermissionMode = "admin",
        ):
            captured["created"] = {
                "instance_id": instance_id,
                "label": label,
                "permission_mode": permission_mode,
            }
            credential = contracts.RuntimeCredentialMetadata(
                credential_id="rcred_dispatch",
                instance_id=instance_id,
                label=label,
                permission_mode=permission_mode,
                created_at=created_at,
                created_by="rcred_admin",
                revoked_at=None,
            )
            self.records = [credential]
            return contracts.RuntimeCredentialResult(credential=credential, token="crt_dispatch")

        def list_runtime_credentials(self, instance_id: str):
            captured["listed_instance_id"] = instance_id
            return contracts.RuntimeCredentialListResult(credentials=self.records)

        def revoke_runtime_credential(self, instance_id: str, credential_id: str):
            captured["revoked"] = {
                "instance_id": instance_id,
                "credential_id": credential_id,
            }
            credential = self.records[0].model_copy(update={"revoked_at": revoked_at})
            self.records = [credential]
            return contracts.RuntimeCredentialResult(credential=credential)

    stub = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: stub)
    prefix = ["--server-url", "http://server", "--instance-id", "inst_123", "credential"]

    minted = runner.invoke(
        cli,
        [*prefix, "mint", "--label", "dispatch", "--mode", "graph_write"],
    )
    listed = runner.invoke(cli, [*prefix, "list"])
    revoked = runner.invoke(cli, [*prefix, "revoke", "rcred_dispatch"])
    listed_after_revoke = runner.invoke(cli, [*prefix, "list"])

    assert minted.exit_code == 0, minted.output
    assert minted.output.count("crt_dispatch") == 1
    assert listed.exit_code == 0, listed.output
    assert "rcred_dispatch\tgraph_write\tactive\tdispatch" in listed.output
    assert revoked.exit_code == 0, revoked.output
    assert "Credential revoked." in revoked.output
    assert listed_after_revoke.exit_code == 0, listed_after_revoke.output
    assert "rcred_dispatch\tgraph_write\trevoked\tdispatch" in listed_after_revoke.output
    assert captured["created"] == {
        "instance_id": "inst_123",
        "label": "dispatch",
        "permission_mode": "graph_write",
    }
    assert captured["revoked"] == {
        "instance_id": "inst_123",
        "credential_id": "rcred_dispatch",
    }


def test_server_mode_init_bootstrap_uses_hosted_kit_route(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubClient:
        def init(self, **_kwargs: object):
            raise AssertionError("plain init route should not be used")

        def init_hosted_instance(self, **kwargs: object):
            captured.update(kwargs)
            return contracts.HostedInstanceInitResult(
                instance_id="inst_hosted",
                status="initialized",
                source_type="kit",
                source_ref="kev-reference",
                warnings=[],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "init", "--kit", "kev-reference", "--bootstrap"],
    )

    assert result.exit_code == 0, result.output
    assert captured["source_type"] == "kit"
    assert captured["kit_refs"] == ["kev-reference"]
    assert "Instance ID: inst_hosted" in result.output
    assert "Active instance: inst_hosted" in result.output


def test_init_bootstrap_requires_server_mode(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["init", "--kit", "kev-reference", "--bootstrap"])

    assert result.exit_code == 2
    assert "--bootstrap requires server mode." in result.output


def test_init_bootstrap_requires_kit(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--server-url", "http://server", "init", "--bootstrap"])

    assert result.exit_code == 2
    assert "--bootstrap requires --kit." in result.output


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--config", "cruxible.yaml"],
        ["--data-dir", "data"],
        ["--root-dir", "."],
    ],
)
def test_init_bootstrap_rejects_local_init_options(
    runner: CliRunner,
    extra_args: list[str],
) -> None:
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "init",
            "--kit",
            "kev-reference",
            "--bootstrap",
            *extra_args,
        ],
    )

    assert result.exit_code == 2
    assert (
        "--bootstrap uses hosted kit init and accepts only --kit and --activate." in result.output
    )


def test_server_mode_init_bootstrap_auth_error_guides_next_steps(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    from cruxible_client.errors import AuthenticationError

    class StubClient:
        def init_hosted_instance(self, **_kwargs: object):
            raise AuthenticationError("Unauthorized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "init", "--kit", "kev-reference", "--bootstrap"],
    )

    assert result.exit_code == 1
    assert "Server auth rejected hosted bootstrap init." in result.output
    assert "bootstrap secret was already claimed" in result.output
    assert "cruxible credential claim-bootstrap" in result.output
    assert "CRUXIBLE_SERVER_BEARER_TOKEN to that ADMIN token" in result.output
    assert "cruxible credential mint" in result.output
    assert "CRUXIBLE_SERVER_BEARER_TOKEN to the BOOTSTRAP secret" in result.output
    assert "Traceback" not in result.output


def test_server_mode_plain_kit_init_auth_error_points_to_bootstrap_path(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    from cruxible_client.errors import AuthenticationError

    class StubClient:
        def init(self, **_kwargs: object):
            raise AuthenticationError("Unauthorized")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "init", "--kit", "kev-reference"],
    )

    assert result.exit_code == 2
    assert "cruxible init --kit kev-reference --bootstrap" in result.output
    assert "cruxible credential claim-bootstrap" in result.output


def test_server_start_generates_bootstrap_secret_and_writes_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.setattr("cruxible_core.server.app.run_server", _capture)
    secret_file = tmp_path / "bootstrap.secret"

    result = runner.invoke(
        cli,
        ["server", "start", "--bootstrap-secret-file", str(secret_file)],
    )

    assert result.exit_code == 0, result.output
    generated = os.environ["CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"]
    assert generated
    assert generated not in result.output
    assert secret_file.read_text().strip() == generated
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert f"Wrote bootstrap secret file: {secret_file} (0600)" in result.output
    assert "cruxible init --kit <ref> --bootstrap" in result.output
    assert "credential claim-bootstrap --secret-file" in result.output
    assert captured == {"host": None, "port": None, "state_dir": None, "socket_path": None}
