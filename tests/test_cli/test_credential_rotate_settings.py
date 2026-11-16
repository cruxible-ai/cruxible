"""S5: credential rotate --key-dir replaces only the token being rotated."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.authoring.principal_settings import read_principal_settings
from cruxible_core.cli.main import cli
from cruxible_core.cli.principal_settings import write_principal_settings

URL, INSTANCE, CRED = "https://cred.example.test", "inst_cred", "rcred_0123456789abcdef"
OLD = f"crt_{CRED}_old-secret"
NEW = f"crt_{CRED}_new-secret"


def _settings(key_dir: Path, token: str) -> Path:
    key_dir.mkdir()
    return write_principal_settings(
        key_dir,
        ctx_obj={"server_url": URL},
        instance_id=INSTANCE,
        principal_id="reviewer",
        private_key_path=key_dir / "reviewer.ed25519",
        token=token,
        written_by="cruxible principal add",
    )


@pytest.fixture
def rotations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    calls: list[str] = []
    metadata = contracts.RuntimeCredentialMetadata(
        credential_id=CRED,
        instance_id=INSTANCE,
        principal_id="reviewer",
        label="reviewer",
        permission_mode="governed_write",
        created_at="2026-11-16T00:00:00Z",
    )

    class Client:
        def list_runtime_credentials(self, instance_id: str) -> Any:
            return SimpleNamespace(credentials=(metadata,))

        def rotate_runtime_credential(self, instance_id: str, credential_id: str, **_: Any) -> Any:
            calls.append(credential_id)
            return contracts.RuntimeCredentialResult(
                status="rotated", credential=metadata, token=NEW
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: Client())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.credentials.sign_principal_mint", lambda **_: None
    )
    return calls


def _rotate(key_dir: Path) -> Any:
    return CliRunner().invoke(
        cli,
        [
            "--server-url",
            URL,
            "--instance-id",
            INSTANCE,
            "credential",
            "rotate",
            CRED,
            "--key-dir",
            str(key_dir),
            "--commit",
            "--at",
            "sha256:" + "0" * 64,
        ],
    )


def test_the_token_being_rotated_is_replaced_in_place(rotations: list[str], tmp_path: Path) -> None:
    path = _settings(tmp_path / "keys", OLD)

    result = _rotate(tmp_path / "keys")

    assert result.exit_code == 0, result.output
    assert rotations == [CRED]
    assert read_principal_settings(path).token == NEW
    assert NEW not in result.output and f"Token written to {path}" in result.output


def test_a_file_holding_another_token_refuses_naming_it(
    rotations: list[str], tmp_path: Path
) -> None:
    path = _settings(tmp_path / "keys", "crt_rcred_ffffffffffffffff_other")

    result = _rotate(tmp_path / "keys")

    assert result.exit_code != 0
    assert str(path) in result.output and CRED in result.output
    assert rotations == []  # refused before anything was revoked
    assert read_principal_settings(path).token == "crt_rcred_ffffffffffffffff_other"


def test_a_newer_token_written_meanwhile_is_never_overwritten(
    rotations: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2: rotation A must not overwrite the token rotation B wrote after A began."""

    from cruxible_core.cli.principal_settings import set_principal_settings_token

    path = _settings(tmp_path / "keys", OLD)
    newer = f"crt_{CRED}_from-rotation-b"
    from cruxible_core.cli.commands import _common

    rotating = _common._get_client()
    original = rotating.rotate_runtime_credential

    def interleaved(instance_id: str, credential_id: str, **options: Any) -> Any:
        result = original(instance_id, credential_id, **options)
        # Rotation B lands its own token in the file while A is in flight.
        set_principal_settings_token(tmp_path / "keys", newer, replaces=OLD)
        return result

    monkeypatch.setattr(rotating, "rotate_runtime_credential", interleaved)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: rotating)

    result = _rotate(tmp_path / "keys")

    assert result.exit_code == 0, result.output
    assert read_principal_settings(path).token == newer  # B's token survives
    assert "no longer holds the token this rotation replaced" in result.output


def test_the_write_compares_the_exact_token_not_the_credential(tmp_path: Path) -> None:
    from cruxible_core.cli.principal_settings import (
        SettingsTokenMismatch,
        set_principal_settings_token,
    )

    path = _settings(tmp_path / "keys", f"crt_{CRED}_held")
    # Same credential, another token than the one this rotation replaces.
    with pytest.raises(SettingsTokenMismatch):
        set_principal_settings_token(tmp_path / "keys", NEW, replaces=OLD)
    # Another credential's token.
    with pytest.raises(SettingsTokenMismatch):
        set_principal_settings_token(
            tmp_path / "keys", NEW, replaces="crt_rcred_ffffffffffffffff_x"
        )
    assert read_principal_settings(path).token == f"crt_{CRED}_held"
    set_principal_settings_token(tmp_path / "keys", NEW, replaces=f"crt_{CRED}_held")
    assert read_principal_settings(path).token == NEW
