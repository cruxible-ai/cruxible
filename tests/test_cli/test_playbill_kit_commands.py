"""CLI kit commands: no public push; add carries the upgrade decisions; status checks updates."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts import CasObjectResult
from cruxible_client.contracts.kits import (
    InstalledKit,
    KitAddRequest,
    KitBuildRequest,
    KitBuildResult,
    KitBundle,
    KitChangeResult,
    KitPathPlan,
    KitProvider,
    KitProviderFile,
    KitProviderFileBytes,
    KitProviderStatus,
    KitProviderStep,
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


def _provider_files() -> dict[str, bytes]:
    return {
        "kit_call-0.2.0-py3-none-any.whl": b"wheel",
        "kit-call-0.2.0.uv.lock": b"lock",
        "cruxible_provider_runtime-0.2.0-py3-none-any.whl": b"runtime",
    }


def _provider_bundle() -> KitBundle:
    files = _provider_files()

    def file(name: str) -> KitProviderFile:
        return KitProviderFile(
            filename=name, sha256="sha256:" + hashlib.sha256(files[name]).hexdigest()
        )

    base = _bundle()
    provider = KitProvider(
        provider_id="kit-call",
        package="kit-call",
        version="0.2.0",
        wheel=file("kit_call-0.2.0-py3-none-any.whl"),
        lock=file("kit-call-0.2.0.uv.lock"),
        dependencies=(file("cruxible_provider_runtime-0.2.0-py3-none-any.whl"),),
        interfaces=("local.increment",),
    )
    return KitBundle(
        manifest=base.manifest.model_copy(update={"providers": (provider,)}),
        artifacts=base.artifacts,
        provider_files=tuple(KitProviderFileBytes.of(name, files[name]) for name in sorted(files)),
    )


class _StagingClient:
    def __init__(self) -> None:
        self.stored: list[bytes] = []
        self.sent: list[Any] = []

    def store_body(self, instance_id: str, content: bytes) -> CasObjectResult:
        self.stored.append(content)
        return CasObjectResult(
            digest="sha256:" + hashlib.sha256(content).hexdigest(),
            present=True,
            byte_length=len(content),
            redacted=False,
        )


def test_kit_build_bundles_a_provider_directory_as_staged_wheels_and_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "kit-call"
    (project / "dist").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "kit-call"\nversion = "0.2.0"\n')
    files = _provider_files()
    (project / "uv.lock").write_bytes(files["kit-call-0.2.0.uv.lock"])
    for name in (
        "kit_call-0.2.0-py3-none-any.whl",
        "cruxible_provider_runtime-0.2.0-py3-none-any.whl",
    ):
        (project / "dist" / name).write_bytes(files[name])

    class StubClient(_StagingClient):
        def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
            self.sent.append(request)
            return KitBuildResult(bundle=_provider_bundle())

    client = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    out = tmp_path / "kit"
    result = CliRunner().invoke(
        cli,
        [
            *BASE,
            "build",
            "--id",
            "acme",
            "--version",
            "1.0.0",
            "--owns",
            "acme.",
            "--out",
            str(out),
            "--provider",
            str(project),
        ],
    )

    assert result.exit_code == 0, result.output
    (request,) = client.sent
    (provider,) = request.providers
    assert provider.wheel.filename == "kit_call-0.2.0-py3-none-any.whl"
    assert [item.filename for item in provider.dependencies] == [
        "cruxible_provider_runtime-0.2.0-py3-none-any.whl"
    ]
    assert sorted(client.stored) == sorted(files.values())
    assert "1 provider package(s)" in result.output
    assert (out / "providers" / "kit_call-0.2.0-py3-none-any.whl").read_bytes() == b"wheel"


def test_kit_add_stages_bundled_providers_only_when_it_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient(_StagingClient):
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            self.sent.append(request)
            return KitChangeResult(
                kit_id="acme",
                version="1.0.0",
                status="awaiting_providers",
                providers=(
                    KitProviderStep(
                        provider_id="kit-call",
                        package="kit-call",
                        version="0.2.0",
                        action="awaiting_approval",
                        proposal_id="prop-1",
                    ),
                ),
            )

    client = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_provider_bundle(), "acme")
    )

    preview = CliRunner().invoke(cli, [*BASE, "add", "acme"])
    assert preview.exit_code == 0, preview.output
    assert client.stored == []
    committed = CliRunner().invoke(cli, [*BASE, "add", "acme", "--commit"])
    assert committed.exit_code == 0, committed.output
    assert sorted(client.stored) == sorted(_provider_files().values())
    assert all(not request.bundle.provider_files for request in client.sent)
    assert "acme 1.0.0: awaiting_providers" in committed.output
    assert "provider kit-call (kit-call 0.2.0): awaiting approval, proposal prop-1" in (
        committed.output
    )


def test_kit_status_lists_bundled_providers_and_their_install_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                kits=(
                    InstalledKit(
                        kit_id="acme",
                        version="1.0.0",
                        content_digest="sha256:" + "1" * 64,
                        source="acme",
                        providers=(
                            KitProviderStatus(
                                provider_id="kit-call",
                                package="kit-call",
                                version="0.2.0",
                                wheel_sha256="sha256:" + "3" * 64,
                                state="differs",
                                installed_version="0.1.0",
                            ),
                        ),
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert "provider kit-call (kit-call 0.2.0): differs (installed: 0.1.0)" in result.output
