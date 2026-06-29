"""Focused tests for state CLI behavior."""

from __future__ import annotations

import json
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


def test_server_mode_create_state_overlay_defaults_root_dir_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubClient:
        def create_state_overlay(
            self,
            *,
            root_dir,
            transport_ref=None,
            state_ref=None,
            kit=None,
            no_kit=False,
        ):
            captured["root_dir"] = root_dir
            captured["transport_ref"] = transport_ref
            captured["state_ref"] = state_ref
            captured["kit"] = kit
            captured["no_kit"] = no_kit
            return contracts.StateOverlayResult(
                instance_id="inst_cloned",
                manifest=contracts.PublishedStateManifest(
                    format_version=1,
                    state_id="kev-reference",
                    release_id="2026-04-21",
                    snapshot_id="snap_1",
                    compatibility="data_only",
                    owned_entity_types=["Vendor", "Product", "Vulnerability"],
                    owned_relationship_types=["vulnerability_affects_product"],
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "state",
            "create-overlay",
            "--state-ref",
            "kev-reference",
            "--kit",
            "kev-triage",
        ],
    )

    assert result.exit_code == 0
    assert captured["root_dir"] == str(tmp_path)
    assert captured["state_ref"] == "kev-reference"
    assert captured["kit"] == "kev-triage"
    assert "Instance ID: inst_cloned" in result.output
    assert "Active instance: inst_cloned" in result.output

    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_cloned"


def test_server_mode_create_state_overlay_no_activate_leaves_context(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )

    class StubClient:
        def create_state_overlay(
            self,
            *,
            root_dir,
            transport_ref=None,
            state_ref=None,
            kit=None,
            no_kit=False,
        ):
            return contracts.StateOverlayResult(
                instance_id="inst_new",
                manifest=contracts.PublishedStateManifest(
                    format_version=1,
                    state_id="kev-reference",
                    release_id="2026-04-21",
                    snapshot_id="snap_1",
                    compatibility="data_only",
                    owned_entity_types=["Vendor", "Product", "Vulnerability"],
                    owned_relationship_types=["vulnerability_affects_product"],
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = runner.invoke(
        cli,
        [
            "state",
            "create-overlay",
            "--state-ref",
            "kev-reference",
            "--no-activate",
        ],
    )

    assert result.exit_code == 0
    assert "Instance ID: inst_new" in result.output
    assert "Active instance unchanged: inst_old" in result.output
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_old"


def test_server_mode_instance_backup_and_restore(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )
    captured: dict[str, object] = {}
    manifest = contracts.InstanceBackupManifest(
        instance_id="inst_restored",
        created_at="2026-03-21T00:00:00Z",
        cruxible_version="0.2.0",
        label="pre-release",
        original_config_path="/srv/project/config.yaml",
        restored_config_path="config.yaml",
        instance_mode="governed",
        artifacts={"state.db": "sha256:abc"},
    )

    class StubClient:
        def backup_instance(self, instance_id, *, artifact_path, label=None):
            captured["backup_instance_id"] = instance_id
            captured["backup_artifact_path"] = artifact_path
            captured["backup_label"] = label
            return contracts.InstanceBackupResult(
                instance_id=instance_id,
                artifact_path=artifact_path,
                manifest=manifest,
            )

        def restore_instance(self, *, artifact_path, root_dir=None):
            captured["restore_artifact_path"] = artifact_path
            captured["restore_root_dir"] = root_dir
            return contracts.InstanceRestoreResult(
                instance_id="inst_restored",
                root_dir=root_dir or "/server/default",
                manifest=manifest,
                registry_status="registered",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    snap = runner.invoke(
        cli,
        ["instance", "backup", "/tmp/backup.zip", "--label", "pre-release"],
    )
    assert snap.exit_code == 0
    assert "Wrote instance backup /tmp/backup.zip" in snap.output
    assert captured["backup_instance_id"] == "inst_old"
    assert captured["backup_label"] == "pre-release"

    restore = runner.invoke(
        cli,
        ["instance", "restore", "/tmp/backup.zip", "--at", "/srv/restored"],
    )
    assert restore.exit_code == 0
    assert "Restored instance inst_restored" in restore.output
    assert "Active instance: inst_restored" in restore.output
    assert "Previous active instance: inst_old" in restore.output
    assert captured["restore_root_dir"] == "/srv/restored"
    shown = runner.invoke(cli, ["context", "show", "--json"])
    assert json.loads(shown.output)["instance_id"] == "inst_restored"


def test_server_mode_instance_relocate(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    runner.invoke(
        cli,
        [
            "context",
            "connect",
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_old",
        ],
    )
    captured: dict[str, object] = {}
    manifest = contracts.InstanceBackupManifest(
        instance_id="inst_old",
        created_at="2026-03-21T00:00:00Z",
        cruxible_version="0.2.0",
        label="relocate",
        original_config_path="/srv/old/config.yaml",
        restored_config_path="config.yaml",
        instance_mode="governed",
        artifacts={"state.db": "sha256:abc"},
    )

    class StubClient:
        def relocate_instance(self, instance_id, *, to_dir, remove_source=False):
            captured["relocate_instance_id"] = instance_id
            captured["relocate_to_dir"] = to_dir
            captured["relocate_remove_source"] = remove_source
            return contracts.InstanceRelocateResult(
                instance_id=instance_id,
                from_dir="/srv/old",
                to_dir=to_dir,
                manifest=manifest,
                source_removed=remove_source,
                registry_status="registered",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = runner.invoke(
        cli,
        ["instance", "relocate", "--to", "/srv/new", "--remove-source"],
    )
    assert result.exit_code == 0, result.output
    assert "Relocated instance inst_old" in result.output
    assert "to=/srv/new" in result.output
    assert "source_removed=True" in result.output
    assert captured["relocate_instance_id"] == "inst_old"
    assert captured["relocate_to_dir"] == "/srv/new"
    assert captured["relocate_remove_source"] is True


def test_local_mode_instance_relocate_requires_server(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: None)
    result = runner.invoke(cli, ["instance", "relocate", "--to", "/srv/new"])
    assert result.exit_code != 0
    assert "server mode" in result.output
