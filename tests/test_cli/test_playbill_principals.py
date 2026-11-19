"""Owner-governed principal onboarding never exports private key custody."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.cli.main import cli

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_principal_add_keeps_private_key_client_side_and_proposes_public_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    custody = tmp_path / "reviewer-custody"
    submitted: list[dict[str, Any]] = []

    class StubClient:
        def orient(self, instance_id: str, **values: Any) -> Any:
            assert instance_id == "inst_principals" and values["section"] == "principals"
            return SimpleNamespace(principals=(), truncated=False, next_cursor=None)

        def propose_principal_change(
            self,
            instance_id: str,
            *,
            principal: dict[str, Any],
            proposal_name: str,
            **_: object,
        ) -> contracts.ProposalInspection:
            assert (instance_id, proposal_name) == ("inst_principals", "add-reviewer")
            submitted.append(principal)
            return contracts.ProposalInspection(
                proposal={"proposal_id": "sha256:" + "5" * 64},
                accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://principals.example.test",
            "--instance-id",
            "inst_principals",
            "principal",
            "add",
            "reviewer",
            "--kind",
            "ordinary",
            "--key-dir",
            str(custody),
            "--name",
            "Add Reviewer",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert submitted[0]["principal_id"] == "reviewer"
    assert submitted[0]["kind"] == "ordinary"
    assert "private" not in json.dumps(submitted[0])
    assert "PRIVATE KEY" not in result.output
    private_key = custody / "reviewer.ed25519"
    assert private_key.is_file()
    assert stat.S_IMODE(private_key.stat().st_mode) == 0o600
    assert (custody / "reviewer.ed25519.pub").is_file()
    assert result.stderr == (
        "target: inst_principals @ https://principals.example.test (explicit)\n"
    )


def test_cli_principal_add_rejects_existing_identity_before_generating_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    custody = tmp_path / "reviewer-custody"

    class StubClient:
        def orient(self, instance_id: str, **values: Any) -> Any:
            assert values["section"] == "principals"
            reviewer = PrincipalRecord(
                principal_id="reviewer", public_key="ab" * 32, kind="ordinary"
            )
            return SimpleNamespace(principals=(reviewer,), truncated=False, next_cursor=None)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://principals.example.test",
            "--instance-id",
            "inst_principals",
            "principal",
            "add",
            "reviewer",
            "--kind",
            "ordinary",
            "--key-dir",
            str(custody),
            "--name",
            "duplicate",
        ],
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert not custody.exists()


def test_cli_principal_add_refuses_daemon_kind() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "principal",
            "add",
            "bad",
            "--kind",
            "daemon",
            "--key-dir",
            "/outside/workspace",
            "--name",
            "bad",
        ],
    )

    assert result.exit_code != 0
    assert "'daemon' is not one of 'ordinary', 'recovery'" in result.output


def test_rotate_and_recover_help_say_the_acting_principal_decides() -> None:
    """Both propose the same key change; the law records it by who is acting."""

    from cruxible_core.cli.main import CLI_COMMANDS

    registry = CLI_COMMANDS["principal"].commands
    assert registry is not None
    assert "rotation when that principal acts" in registry["rotate"].help
    assert "recovery when another principal acts" in registry["recover"].help
    runner = CliRunner()
    for command in ("rotate", "recover"):
        result = runner.invoke(cli, ["principal", command, "--help"], terminal_width=200)
        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        assert "when you are PRINCIPAL_ID" in text and "a rotation" in text
        assert "a recovery" in text
        assert "recovery identity" not in text


def test_the_global_principal_id_reaches_the_client_and_whoami_says_it_is_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.delenv("CRUXIBLE_PRINCIPAL_ID", raising=False)
    constructed: list[dict[str, Any]] = []

    class StubClient:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def whoami(self, instance_id: str) -> contracts.WhoAmI:
            return contracts.WhoAmI(
                actor_id="alice",
                credential_label=None,
                actor_id_source="principal_claim",
                authenticated=False,
                credential_permission_mode="admin",
                principal_registration_status="active",
                active_principal_ids=["alice"],
                coordinate=COORDINATE,
                can_author=True,
                authoring_refusal=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common.CruxibleClient", StubClient)
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common.client_compatibility.check_daemon_compatibility",
        lambda _client: None,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(tmp_path / "d.sock"),
            "--instance-id",
            "inst_principals",
            "--principal-id",
            "alice",
            "whoami",
        ],
    )

    assert result.exit_code == 0, result.output
    assert constructed[0]["principal_id"] == "alice"
    assert "configured principal ID (CRUXIBLE_PRINCIPAL_ID)" in result.output
    assert "Identity is a claim, not authentication" in result.output
    assert "Can author: yes" in result.output


def test_principal_rotate_writes_the_settings_file_beside_the_new_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """S5: rotate leaves a loadable settings file for the new key, token carried over."""

    from cruxible_client.authoring.principal_settings import read_principal_settings
    from cruxible_core.cli.context import (
        CliContextState,
        principal_binding,
        save_cli_context,
    )
    from cruxible_core.cli.principal_settings import write_principal_settings

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    url, instance = "https://principals.example.test", "inst_principals"
    old_dir, new_dir = tmp_path / "old-custody", tmp_path / "new-custody"
    old_dir.mkdir()
    old = write_principal_settings(
        old_dir,
        ctx_obj={"server_url": url},
        instance_id=instance,
        principal_id="reviewer",
        private_key_path=old_dir / "reviewer.ed25519",
        token="tok-carried",
        written_by="cruxible principal add",
    )
    save_cli_context(
        CliContextState(server_url=url).remember_principal(
            principal_binding(url, instance), "reviewer", old, activate=False
        )
    )
    record = PrincipalRecord(principal_id="reviewer", public_key="a" * 64, kind="ordinary")

    class StubClient:
        def orient(self, instance_id: str, **values: Any) -> Any:
            return SimpleNamespace(principals=(record,), truncated=False, next_cursor=None)

        def propose_principal_change(self, instance_id: str, **_: object) -> Any:
            return contracts.ProposalInspection(
                proposal={"proposal_id": "sha256:" + "5" * 64},
                accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            url,
            "--instance-id",
            instance,
            "principal",
            "rotate",
            "reviewer",
            "--key-dir",
            str(new_dir),
            "--name",
            "Rotate Reviewer",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    written = read_principal_settings(Path(payload["settings_path"]))
    assert written.path == new_dir.resolve() / "cruxible.env"
    assert written.private_key_path == str((new_dir / "reviewer.ed25519").resolve())
    assert (written.principal_id, written.instance_id, written.token) == (
        "reviewer",
        instance,
        "tok-carried",
    )
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600
    # The old file stays the one the CLI acts with until the change is active.
    assert read_principal_settings(old).private_key_path.endswith("old-custody/reviewer.ed25519")
    assert "tok-carried" not in result.output
