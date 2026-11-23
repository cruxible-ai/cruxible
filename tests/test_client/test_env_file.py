"""Unit and SDK tests for Cruxible env-file loading (CRUXIBLE_ENV_FILE / --env-file)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from cruxible_client.authoring.env_file import (
    ENV_FILE_ENV,
    ENV_FILE_ERROR_CODE,
    ENV_FILE_MAX_BYTES,
    EnvFileError,
    apply_env_file,
    merge_env_file,
    read_env_file,
    resolve_env_file_path,
)
from cruxible_client.authoring.sdk import Cruxible
from cruxible_core.cli.principal_settings import write_principal_settings
from cruxible_core.governance.keys import generate_client_principal_key


def _write_file(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_read_env_file_parses_write_principal_settings_output(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys" / "reviewer"
    key_dir.mkdir(parents=True)
    key_path = key_dir / "reviewer.ed25519"
    key_path.write_text("dummy", encoding="utf-8")
    socket_path = tmp_path / "daemon with spaces.sock"

    settings_path = write_principal_settings(
        key_dir,
        ctx_obj={"server_socket": str(socket_path)},
        instance_id="inst_abc123",
        principal_id="reviewer",
        private_key_path=key_path,
        token="tok_secret_value_123",
        written_by="cruxible principal add",
    )

    parsed = read_env_file(settings_path)
    assert parsed == {
        "CRUXIBLE_SERVER_SOCKET": str(socket_path.resolve()),
        "CRUXIBLE_INSTANCE_ID": "inst_abc123",
        "CRUXIBLE_PRINCIPAL_ID": "reviewer",
        "CRUXIBLE_PRINCIPAL_KEY": str(key_path.resolve()),
        "CRUXIBLE_SERVER_BEARER_TOKEN": "tok_secret_value_123",
    }


def test_read_env_file_accepts_bare_assignments_and_comments(tmp_path: Path) -> None:
    env_file = _write_file(
        tmp_path / "cruxible.env",
        """
        # Leading comment
        CRUXIBLE_SERVER_URL=https://crux.example.test  # inline comment
        export CRUXIBLE_INSTANCE_ID="inst_42"
        CRUXIBLE_MODE='graph_write'
        CRUXIBLE_MCP_PROFILE=core
        """,
        mode=0o644,  # No bearer token, so 0644 is allowed.
    )

    parsed = read_env_file(env_file)
    assert parsed == {
        "CRUXIBLE_SERVER_URL": "https://crux.example.test",
        "CRUXIBLE_INSTANCE_ID": "inst_42",
        "CRUXIBLE_MODE": "graph_write",
        "CRUXIBLE_MCP_PROFILE": "core",
    }


def test_read_env_file_refuses_loose_permissions_when_token_present(tmp_path: Path) -> None:
    secret = "tok_never_echo_this_999"
    env_file = _write_file(
        tmp_path / "cruxible.env",
        (
            "export CRUXIBLE_INSTANCE_ID='inst_1'\n"
            f"export CRUXIBLE_SERVER_BEARER_TOKEN={shlex.quote(secret)}\n"
        ),
        mode=0o644,
    )

    with pytest.raises(EnvFileError) as exc_info:
        read_env_file(env_file)

    message = str(exc_info.value)
    assert ENV_FILE_ERROR_CODE in message
    assert "chmod 600" in message
    assert secret not in message

    env_file.chmod(0o600)
    assert read_env_file(env_file)["CRUXIBLE_SERVER_BEARER_TOKEN"] == secret


def test_read_env_file_never_leaks_token_on_syntax_error(tmp_path: Path) -> None:
    secret = "tok_unclosed_secret_value"
    env_file = _write_file(
        tmp_path / "cruxible.env",
        f"export CRUXIBLE_SERVER_BEARER_TOKEN='{secret}\n",
        mode=0o600,
    )

    with pytest.raises(EnvFileError) as exc_info:
        read_env_file(env_file)

    message = str(exc_info.value)
    assert ENV_FILE_ERROR_CODE in message
    assert "CRUXIBLE_SERVER_BEARER_TOKEN" in message
    assert secret not in message


def test_read_env_file_refuses_symlink_directory_missing_oversized_and_invalid_utf8(
    tmp_path: Path,
) -> None:
    real_file = _write_file(tmp_path / "real.env", "CRUXIBLE_INSTANCE_ID=inst_1\n")
    link = tmp_path / "link.env"
    link.symlink_to(real_file)

    with pytest.raises(EnvFileError, match="symbolic link"):
        read_env_file(link)

    with pytest.raises(EnvFileError, match="does not exist"):
        read_env_file(tmp_path / "missing.env")

    with pytest.raises(EnvFileError, match="regular file"):
        read_env_file(tmp_path)

    oversized = tmp_path / "huge.env"
    oversized.write_bytes(b"#" * (ENV_FILE_MAX_BYTES + 1))
    oversized.chmod(0o600)
    with pytest.raises(EnvFileError, match="limit"):
        read_env_file(oversized)

    bad_utf8 = tmp_path / "bad.env"
    bad_utf8.write_bytes(b"\xff\xfeCRUXIBLE_INSTANCE_ID=inst_1\n")
    bad_utf8.chmod(0o600)
    with pytest.raises(EnvFileError, match="UTF-8"):
        read_env_file(bad_utf8)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("# only comments\n\n", "sets no Cruxible variables"),
        ("PATH=/example/evil\n", "not allowed"),
        ("LD_PRELOAD=/example/lib.so\n", "not allowed"),
        ("CRUXIBLE_ENV_FILE=/example/other.env\n", "not allowed"),
        ("CRUXIBLE_CLI_CONTEXT_PATH=/example/ctx.json\n", "not allowed"),
        ("not an assignment\n", "expected KEY=VALUE"),
        ("CRUXIBLE_INSTANCE_ID=\n", "one non-empty value"),
        ('CRUXIBLE_INSTANCE_ID=""\n', "one non-empty value"),
        ("CRUXIBLE_INSTANCE_ID=two words\n", "one non-empty value"),
        (
            "CRUXIBLE_SERVER_URL=https://a.test\nCRUXIBLE_SERVER_SOCKET=/example/a.sock\n",
            "cannot set both",
        ),
    ],
)
def test_read_env_file_refuses_disallowed_or_malformed_contents(
    tmp_path: Path, content: str, match: str
) -> None:
    env_file = _write_file(tmp_path / "bad.env", content, mode=0o600)
    with pytest.raises(EnvFileError, match=match):
        read_env_file(env_file)


def test_resolve_env_file_path_distinguishes_explicit_empty_from_env_empty() -> None:
    assert resolve_env_file_path(None, environ={}) is None
    assert resolve_env_file_path(None, environ={ENV_FILE_ENV: "   "}) is None
    assert resolve_env_file_path(None, environ={ENV_FILE_ENV: "/example/a.env"}) == Path(
        "/example/a.env"
    )
    with pytest.raises(EnvFileError, match="must not be empty"):
        resolve_env_file_path("", environ={ENV_FILE_ENV: "/example/a.env"})


def test_merge_env_file_precedence_and_transport_and_principal_guards(tmp_path: Path) -> None:
    env_file = _write_file(
        tmp_path / "owner.env",
        "\n".join(
            [
                "export CRUXIBLE_SERVER_SOCKET='/example/from-file.sock'",
                "export CRUXIBLE_INSTANCE_ID='inst_from_file'",
                "export CRUXIBLE_PRINCIPAL_ID='owner'",
                "export CRUXIBLE_PRINCIPAL_KEY='/keys/owner.ed25519'",
                "export CRUXIBLE_SERVER_BEARER_TOKEN='tok_owner'",
                "export CRUXIBLE_MODE='graph_write'",
            ]
        )
        + "\n",
    )

    # 1. Existing env var wins; existing URL blocks file's socket.
    merged = merge_env_file(
        env_file,
        base_environ={
            "CRUXIBLE_SERVER_URL": "https://override.test",
            "CRUXIBLE_MODE": "read",
        },
    )
    assert "CRUXIBLE_SERVER_SOCKET" not in merged
    assert "CRUXIBLE_MODE" not in merged
    assert merged["CRUXIBLE_INSTANCE_ID"] == "inst_from_file"
    assert merged["CRUXIBLE_PRINCIPAL_ID"] == "owner"
    assert merged["CRUXIBLE_PRINCIPAL_KEY"] == "/keys/owner.ed25519"
    assert merged["CRUXIBLE_SERVER_BEARER_TOKEN"] == "tok_owner"

    # 2. Explicit transport flag blocks file's transport.
    merged_transport = merge_env_file(env_file, base_environ={}, explicit_transport=True)
    assert "CRUXIBLE_SERVER_SOCKET" not in merged_transport
    assert merged_transport["CRUXIBLE_INSTANCE_ID"] == "inst_from_file"

    # 3. Different principal (explicit or env) skips owner's id, key, and bearer token.
    for kwargs in (
        {"base_environ": {}, "explicit_principal": "reviewer"},
        {"base_environ": {"CRUXIBLE_PRINCIPAL_ID": "reviewer"}},
    ):
        merged_other = merge_env_file(env_file, **kwargs)  # type: ignore[arg-type]
        assert merged_other["CRUXIBLE_INSTANCE_ID"] == "inst_from_file"
        assert "CRUXIBLE_PRINCIPAL_ID" not in merged_other
        assert "CRUXIBLE_PRINCIPAL_KEY" not in merged_other
        assert "CRUXIBLE_SERVER_BEARER_TOKEN" not in merged_other

    # 4. Matching explicit principal keeps CRUXIBLE_PRINCIPAL_ID paired with its key.
    merged_same = merge_env_file(env_file, base_environ={}, explicit_principal="owner")
    assert merged_same["CRUXIBLE_PRINCIPAL_ID"] == "owner"
    assert merged_same["CRUXIBLE_PRINCIPAL_KEY"] == "/keys/owner.ed25519"
    assert merged_same["CRUXIBLE_SERVER_BEARER_TOKEN"] == "tok_owner"


def test_apply_env_file_restores_previous_environment(tmp_path: Path) -> None:
    env_file = _write_file(
        tmp_path / "cruxible.env",
        "CRUXIBLE_INSTANCE_ID=inst_loaded\nCRUXIBLE_MODE=admin\n",
    )
    target: dict[str, str] = {"CRUXIBLE_MODE": "read"}

    loaded_path, restore = apply_env_file(env_file, environ=target)
    assert loaded_path == env_file.resolve()
    assert target == {"CRUXIBLE_MODE": "read", "CRUXIBLE_INSTANCE_ID": "inst_loaded"}

    restore()
    assert target == {"CRUXIBLE_MODE": "read"}


def test_sdk_connect_env_file_is_isolated_and_supports_two_principals_in_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "daemon.sock"
    env_files: dict[str, Path] = {}
    for principal in ("owner", "reviewer"):
        key_dir = tmp_path / "keys" / principal
        generated = generate_client_principal_key(
            key_dir, principal_id=principal, kind="ordinary", forbidden_roots=()
        )
        env_files[principal] = write_principal_settings(
            key_dir,
            ctx_obj={"server_socket": str(socket_path)},
            instance_id="inst_dual",
            principal_id=principal,
            private_key_path=generated.private_key_path,
            token=f"tok_{principal}",
            written_by="test",
        )

    for key in list(os.environ):
        if key.startswith("CRUXIBLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "no-context.json"))
    before_env = dict(os.environ)

    created: list[dict[str, object]] = []

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str | None = None,
            socket_path: str | None = None,
            token: str | None = None,
            principal_id: str | None = None,
        ) -> None:
            self.base_url = base_url
            self.socket_path = socket_path
            self.token = token
            self.principal_id = principal_id
            created.append(
                {
                    "base_url": base_url,
                    "socket_path": socket_path,
                    "token": token,
                    "principal_id": principal_id,
                }
            )

        def whoami(self, _instance_id: str) -> object:
            return SimpleNamespace(
                actor_id=self.principal_id,
                coordinate=SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "git_oid": "a" * 40,
                        "semantic_root": "sha256:" + "b" * 64,
                        "generation_root": "sha256:" + "c" * 64,
                        "compiler_digest": "sha256:" + "d" * 64,
                    }
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("cruxible_client.authoring.sdk.CruxibleClient", FakeClient)
    monkeypatch.setattr(
        "cruxible_client.authoring.sdk.client_compatibility.check_daemon_compatibility",
        lambda _client: None,
    )

    owner_cx = Cruxible.connect(env_file=env_files["owner"])
    reviewer_cx = Cruxible.connect(env_file=env_files["reviewer"])

    # os.environ was never mutated.
    assert dict(os.environ) == before_env

    assert created == [
        {
            "base_url": None,
            "socket_path": str(socket_path.resolve()),
            "token": "tok_owner",
            "principal_id": "owner",
        },
        {
            "base_url": None,
            "socket_path": str(socket_path.resolve()),
            "token": "tok_reviewer",
            "principal_id": "reviewer",
        },
    ]

    # Each connection resolves its own isolated signing key, including across .at().
    assert owner_cx.signer().signer_id == "owner"
    assert reviewer_cx.signer().signer_id == "reviewer"
    assert reviewer_cx.at(reviewer_cx.coordinate).signer().signer_id == "reviewer"

    # Explicit principal_id differing from env_file never sends owner's bearer token.
    created.clear()
    other_cx = Cruxible.connect(env_file=env_files["owner"], principal_id="auditor")
    assert created[0]["principal_id"] == "auditor"
    assert created[0]["token"] is None
    other_cx.close()

    # CRUXIBLE_ENV_FILE works when env_file= is omitted, and explicit token/target override it.
    created.clear()
    monkeypatch.setenv(ENV_FILE_ENV, str(env_files["reviewer"]))
    override_cx = Cruxible.connect(
        target="https://explicit.example.test",
        token=SecretStr("tok_explicit"),
    )
    assert created[0] == {
        "base_url": "https://explicit.example.test",
        "socket_path": None,
        "token": "tok_explicit",
        "principal_id": "reviewer",
    }
    override_cx.close()
    owner_cx.close()
    reviewer_cx.close()
