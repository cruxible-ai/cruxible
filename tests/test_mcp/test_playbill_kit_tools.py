"""MCP kit tools: add by registry reference (this adapter pulls), status with update check."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.artifacts import pack_artifact
from cruxible_client.contracts.kits import KitAddRequest, KitChangeResult, KitStatus
from cruxible_client.kits import KIT_ARTIFACT
from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from tests.support.mcp_daemon import bind_mcp_daemon
from tests.test_client.test_artifacts import _bundle


def test_kit_add_by_reference_pulls_in_the_adapter_and_records_the_pinned_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    image = pack_artifact(KIT_ARTIFACT, bundle)
    pulled: list[str] = []
    sent: list[KitAddRequest] = []

    def fetch(reference: str) -> tuple[Any, str]:
        pulled.append(reference)
        return image, f"ghcr.io/cruxible-ai/kits/acme@{image.digest}"

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            sent.append(request)
            return KitChangeResult(kit_id="acme", version="1.0.0", status="would_propose")

    monkeypatch.setattr(handlers, "fetch_kit_image", fetch)
    bind_mcp_daemon(monkeypatch, StubClient())

    result = handlers.handle_playbill_kit_add(
        "inst", reference="acme:1.0.0", keep=("ClaimType:acme.b", "ClaimType:acme.a")
    )

    assert result.status == "would_propose"
    assert pulled == ["acme:1.0.0"]
    (request,) = sent
    assert request.bundle == bundle
    assert request.source == f"ghcr.io/cruxible-ai/kits/acme@{image.digest}"
    assert request.keep == ("ClaimType:acme.a", "ClaimType:acme.b")
    with pytest.raises(DataValidationError, match="exactly one of reference or bundle"):
        handlers.handle_playbill_kit_add("inst")


def test_kit_status_runs_the_update_check_in_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bool] = []

    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus()

    def check(status: KitStatus, *, offline: bool = False) -> KitStatus:
        checked.append(offline)
        return status

    bind_mcp_daemon(monkeypatch, StubClient())
    monkeypatch.setattr(handlers, "check_kit_updates", check)

    handlers.handle_playbill_kit_status("inst", offline=True)

    assert checked == [True]


def test_kit_add_stages_a_pulled_kits_bundled_providers_when_it_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_cli.test_playbill_kit_commands import (
        _provider_bundle,
        _provider_files,
        _StagingClient,
    )

    bundle = _provider_bundle()
    image = pack_artifact(KIT_ARTIFACT, bundle)

    class StubClient(_StagingClient):
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            self.sent.append(request)
            return KitChangeResult(kit_id="acme", version="1.0.0", status="accepted")

    client = StubClient()
    monkeypatch.setattr(handlers, "fetch_kit_image", lambda ref: (image, "acme@" + image.digest))
    bind_mcp_daemon(monkeypatch, client)

    handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0")
    assert client.stored == []
    handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0", dry_run=False)

    assert sorted(client.stored) == sorted(_provider_files().values())
    assert [request.bundle.manifest for request in client.sent] == [bundle.manifest] * 2
    assert all(not request.bundle.provider_files for request in client.sent)
