"""CLI kit commands: no public push; add carries the upgrade decisions; status checks updates."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.kits import (
    InstalledKit,
    KitAddRequest,
    KitChangeResult,
    KitPathPlan,
    KitStatus,
)
from cruxible_core.cli.main import cli
from tests.test_client.test_artifacts import _bundle

BASE = ["--server-url", "https://kits.example.test", "--instance-id", "inst", "kit"]


def test_kit_push_is_not_a_public_command() -> None:
    result = CliRunner().invoke(cli, [*BASE, "push", "acme", "acme:1.0.0"])
    assert result.exit_code == 2


def test_kit_add_sends_the_upgrade_decisions_and_groups_the_plan_by_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sent: list[KitAddRequest] = []

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            sent.append(request)
            return KitChangeResult(
                kit_id="acme",
                version="1.1.0",
                status="would_propose",
                transition="upgrade",
                installed_version="1.0.0",
                plan=(
                    KitPathPlan(
                        path="claim-types/acme.account/plan.json",
                        action="replace",
                        identity="ClaimType:acme.account.plan",
                        consequence="overwrites_your_edit",
                        dependent_count=3,
                    ),
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "acme-1.1.0")
    )

    result = CliRunner().invoke(
        cli,
        [
            *BASE,
            "add",
            "acme-1.1.0",
            "--keep",
            "ClaimType:acme.account.seats",
            "--retire-dependents",
            "ClaimType:acme.account.old",
            "--allow-downgrade",
        ],
    )

    assert result.exit_code == 0, result.output
    (request,) = sent
    assert request.keep == ("ClaimType:acme.account.seats",)
    assert request.retire_dependents == ("ClaimType:acme.account.old",)
    assert request.allow_downgrade is True
    assert "acme 1.1.0: would_propose (upgrade from 1.0.0)" in result.output
    assert "ClaimType:" in result.output
    assert (
        "  replace: ClaimType:acme.account.plan (overwrites your edit; 3 dependent(s))"
        in result.output
    )


def test_kit_status_offline_skips_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                kits=(
                    InstalledKit(
                        kit_id="acme",
                        version="1.0.0",
                        content_digest="sha256:" + "1" * 64,
                        source="ghcr.io/cruxible-ai/kits/acme@sha256:" + "2" * 64,
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert "acme 1.0.0" in result.output
    assert "update check offline" in result.output
