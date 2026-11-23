"""CLI integration tests for ``--env-file`` and ``CRUXIBLE_ENV_FILE``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client.authoring.env_file import ENV_FILE_ENV, ENV_FILE_ERROR_CODE
from cruxible_core.cli.main import cli
from cruxible_core.cli.principal_settings import write_principal_settings
from cruxible_core.mcp.daemon import daemon_child_environment


def _clean_cruxible_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("CRUXIBLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "no-context.json"))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _make_principal_env(tmp_path: Path, principal: str, *, token: str | None = None) -> Path:
    key_dir = tmp_path / "keys" / principal
    key_dir.mkdir(parents=True)
    key_path = key_dir / f"{principal}.ed25519"
    key_path.write_text("dummy-key", encoding="utf-8")
    key_path.chmod(0o600)
    return write_principal_settings(
        key_dir,
        ctx_obj={"server_socket": str(tmp_path / "daemon.sock")},
        instance_id="inst_envfile",
        principal_id=principal,
        private_key_path=key_path,
        token=token,
        written_by="test",
    )


def test_cli_env_file_flag_and_env_var_load_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_cruxible_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    env_file = _make_principal_env(tmp_path, "reviewer", token="tok_reviewer_cli")

    runner = CliRunner()
    before = dict(os.environ)

    result = runner.invoke(cli, ["--env-file", str(env_file), "context", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["server_socket"] == str((tmp_path / "daemon.sock").resolve())
    assert payload["instance_id"] == "inst_envfile"
    assert payload["principal_id"] == "reviewer"
    assert payload["principal_source"] == "env_file"
    assert payload["principal_settings"] == str(env_file.resolve())
    # CliRunner invocation restored os.environ completely.
    assert dict(os.environ) == before

    # CRUXIBLE_ENV_FILE works without passing --env-file.
    monkeypatch.setenv(ENV_FILE_ENV, str(env_file))
    before_env = dict(os.environ)
    result_env = runner.invoke(cli, ["context", "show", "--json"])
    assert result_env.exit_code == 0, result_env.output
    payload_env = json.loads(result_env.output)
    assert payload_env["instance_id"] == "inst_envfile"
    assert payload_env["principal_id"] == "reviewer"
    assert payload_env["principal_source"] == "env_file"
    assert dict(os.environ) == before_env


def test_cli_explicit_flags_override_env_file_and_isolate_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_cruxible_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    owner_env = _make_principal_env(tmp_path, "owner", token="tok_owner_secret")
    other_sock = tmp_path / "other.sock"

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--env-file",
            str(owner_env),
            "--server-socket",
            str(other_sock),
            "--principal-id",
            "reviewer",
            "context",
            "show",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["server_socket"] == str(other_sock)
    assert payload["instance_id"] == "inst_envfile"
    assert payload["principal_id"] == "reviewer"
    assert payload["principal_source"] == "explicit"
    assert payload["principal_settings"] is None


def test_cli_env_file_errors_and_empty_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_cruxible_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    missing = runner.invoke(cli, ["--env-file", str(tmp_path / "missing.env"), "context", "show"])
    assert missing.exit_code != 0
    assert ENV_FILE_ERROR_CODE in missing.output

    explicit_empty = runner.invoke(cli, ["--env-file", "", "context", "show"])
    assert explicit_empty.exit_code != 0
    assert ENV_FILE_ERROR_CODE in explicit_empty.output

    # Empty CRUXIBLE_ENV_FILE in the environment is treated as unset.
    monkeypatch.setenv(ENV_FILE_ENV, "")
    ignored = runner.invoke(cli, ["context", "show", "--json"])
    assert ignored.exit_code == 0, ignored.output


def test_daemon_child_environment_strips_cruxible_env_file() -> None:
    child_env = daemon_child_environment(
        {
            "PATH": "/usr/bin",
            "CRUXIBLE_ENV_FILE": "/example/owner/cruxible.env",
            "CRUXIBLE_SERVER_BEARER_TOKEN": "tok_secret",
            "CRUXIBLE_DEBUG": "1",
        }
    )
    assert "CRUXIBLE_ENV_FILE" not in child_env
    assert "CRUXIBLE_SERVER_BEARER_TOKEN" not in child_env
    assert child_env["CRUXIBLE_DEBUG"] == "1"
