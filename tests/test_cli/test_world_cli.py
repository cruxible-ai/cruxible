"""Focused tests for world CLI behavior."""

from __future__ import annotations

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


def test_server_mode_create_world_overlay_defaults_root_dir_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubClient:
        def create_world_overlay(
            self,
            *,
            root_dir,
            transport_ref=None,
            world_ref=None,
            kit=None,
            no_kit=False,
        ):
            captured["root_dir"] = root_dir
            captured["transport_ref"] = transport_ref
            captured["world_ref"] = world_ref
            captured["kit"] = kit
            captured["no_kit"] = no_kit
            return contracts.WorldOverlayResult(
                instance_id="inst_cloned",
                manifest=contracts.PublishedWorldManifest(
                    format_version=1,
                    world_id="kev-reference",
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
            "world",
            "create-overlay",
            "--world-ref",
            "kev-reference",
            "--kit",
            "kev-triage",
        ],
    )

    assert result.exit_code == 0
    assert captured["root_dir"] == str(tmp_path)
    assert captured["world_ref"] == "kev-reference"
    assert captured["kit"] == "kev-triage"
    assert "Instance ID: inst_cloned" in result.output
