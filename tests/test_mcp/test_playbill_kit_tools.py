"""MCP kit tools: add by local kit or registry reference (this adapter reads or pulls),
status with update check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.artifacts import pack_artifact
from cruxible_client.artifacts.registry import RegistryRefusal
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitChangeResult,
    KitOwnershipConflictRefusal,
    KitStatus,
)
from cruxible_client.kits import KIT_ARTIFACT, KitSourceError, write_kit_directory
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

    monkeypatch.setattr(handlers, "resolve_kit_image", fetch)
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


def test_kit_collision_preserves_the_shared_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = KitOwnershipConflictRefusal(
        our_prefixes=("acme.account.",),
        owning_prefix="acme.",
        owning_kit_id="acme",
        detail="acme.account. overlaps acme., owned by kit acme",
        repair="Remove the owning kit first: cruxible kit remove acme; or install "
        "a release that does not own the overlapping prefix.",
    )
    answer = KitChangeResult(
        kit_id="extra", version="1.0.0", status="would_block", refusals=(refusal,)
    )

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            return answer

    bind_mcp_daemon(monkeypatch, StubClient())
    result = handlers.handle_playbill_kit_add("inst", bundle=_bundle())
    assert result.model_dump(mode="json")["refusals"] == [refusal.model_dump(mode="json")]


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
    monkeypatch.setattr(handlers, "resolve_kit_image", lambda ref: (image, "acme@" + image.digest))
    bind_mcp_daemon(monkeypatch, client)

    handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0")
    assert client.stored == []
    handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0", dry_run=False)

    assert sorted(client.stored) == sorted(_provider_files().values())
    assert [request.bundle.manifest for request in client.sent] == [bundle.manifest] * 2
    assert all(not request.bundle.provider_files for request in client.sent)
    assert [request.dry_run for request in client.sent] == [None, False]
    assert all(request.source == "acme@" + image.digest for request in client.sent)


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[KitAddRequest] = []

    def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
        self.sent.append(request)
        return KitChangeResult(kit_id="acme", version="1.0.0", status="would_propose")


def test_kit_add_reads_a_local_kit_directory_like_the_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _bundle()
    kit_dir = tmp_path / "acme"
    write_kit_directory(bundle, kit_dir)
    client = _RecordingClient()
    bind_mcp_daemon(monkeypatch, client)

    result = handlers.handle_playbill_kit_add("inst", reference=str(kit_dir))

    assert result.status == "would_propose"
    (request,) = client.sent
    assert request.bundle.manifest == bundle.manifest
    assert request.bundle.artifacts == bundle.artifacts
    assert request.source == "acme"


def test_kit_add_names_what_is_wrong_with_an_unreadable_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bind_mcp_daemon(monkeypatch, _RecordingClient())

    with pytest.raises(KitSourceError, match="nothing exists there"):
        handlers.handle_playbill_kit_add("inst", reference=str(tmp_path / "missing"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(KitSourceError, match="holds no cruxible-kit.json"):
        handlers.handle_playbill_kit_add("inst", reference=str(tmp_path / "empty"))
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "cruxible-kit.json").write_text("{}")
    with pytest.raises(KitSourceError, match="is not a kit manifest"):
        handlers.handle_playbill_kit_add("inst", reference=str(tmp_path / "bad"))


def test_a_registry_miss_on_a_path_like_reference_says_no_local_kit_exists_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cruxible_client.kits as kits

    def miss(source: str, *, registry: Any = None) -> Any:
        raise RegistryRefusal(
            "not_found", f"ghcr.io/x/{source}:latest is not published (HTTP 404)", status=404
        )

    monkeypatch.setattr(kits, "fetch_kit_image", miss)
    bind_mcp_daemon(monkeypatch, _RecordingClient())

    with pytest.raises(RegistryRefusal) as caught:
        handlers.handle_playbill_kit_add("inst", reference="kits/no-such")
    assert caught.value.error_code == "cruxible.kit.not_found"
    assert "no local kit directory or layout exists at kits/no-such either" in str(caught.value)
    with pytest.raises(RegistryRefusal) as plain:
        handlers.handle_playbill_kit_add("inst", reference="acme:9.9.9")
    assert "either" not in str(plain.value)


def test_a_kit_directory_whose_artifacts_lead_outside_it_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlinked parent (``artifacts -> ../outside``) is refused, not followed."""

    import shutil

    from cruxible_client.kits import resolve_kit

    kit = tmp_path / "named-kit"
    write_kit_directory(_bundle(), kit)
    outside = tmp_path / "outside-artifacts"
    shutil.move(str(kit / "artifacts"), outside)
    (kit / "artifacts").symlink_to(outside, target_is_directory=True)
    client = _RecordingClient()
    bind_mcp_daemon(monkeypatch, client)

    with pytest.raises(KitSourceError, match="symbolic link"):
        handlers.handle_playbill_kit_add("inst", reference=str(kit))
    # The CLI reads through the same shared reader.
    with pytest.raises(KitSourceError, match="symbolic link"):
        resolve_kit(str(kit))
    assert client.sent == []


def test_an_oci_layout_whose_blobs_lead_outside_it_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    from cruxible_client.artifacts import write_layout
    from cruxible_client.kits import resolve_kit_image

    outside = tmp_path / "outside-layout"
    write_layout(pack_artifact(KIT_ARTIFACT, _bundle()), outside)
    named = tmp_path / "named-layout"
    named.mkdir()
    for name in ("oci-layout", "index.json"):
        shutil.copyfile(outside / name, named / name)
    (named / "blobs").symlink_to(outside / "blobs", target_is_directory=True)
    client = _RecordingClient()
    bind_mcp_daemon(monkeypatch, client)

    with pytest.raises(KitSourceError, match="symbolic link"):
        handlers.handle_playbill_kit_add("inst", reference=str(named))
    with pytest.raises(KitSourceError, match="symbolic link"):
        resolve_kit_image(str(named))
    assert client.sent == []
    # A layout whose files all sit beneath it still reads.
    image, _origin = resolve_kit_image(str(outside))
    assert image.digest == pack_artifact(KIT_ARTIFACT, _bundle()).digest


def test_kit_add_returns_the_automation_template_from_a_directory_or_a_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_cli.test_playbill_kit_commands import (
        TEMPLATE,
        _DaemonLikeClient,
        _template_bundle,
    )

    bundle = _template_bundle()
    kit_dir = tmp_path / "acme"
    write_kit_directory(bundle, kit_dir)
    image = pack_artifact(KIT_ARTIFACT, bundle)
    monkeypatch.setattr(handlers, "resolve_kit_image", lambda ref: (image, "acme@" + image.digest))
    client = _DaemonLikeClient()
    bind_mcp_daemon(monkeypatch, client)

    previewed = handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0")
    committed = handlers.handle_playbill_kit_add("inst", reference="acme:1.0.0", dry_run=False)
    for result in (previewed, committed):
        assert result.automation_template == "automation.json"
        assert result.automation_template_content == TEMPLATE
    monkeypatch.undo()
    bind_mcp_daemon(monkeypatch, client)
    local = handlers.handle_playbill_kit_add("inst", reference=str(kit_dir))
    assert local.automation_template_content == TEMPLATE
    assert all(not request.bundle.files for request in client.sent)
    # An inline bundle that declares a template without its bytes refuses before
    # the daemon: no request is sent.
    before = len(client.sent)
    with pytest.raises(KitSourceError, match="carries no bytes") as refused:
        handlers.handle_playbill_kit_add("inst", bundle=bundle.model_copy(update={"files": ()}))
    assert refused.value.error_code == "cruxible.kit.source_invalid"
    assert len(client.sent) == before
    # A kit with no template carries neither.
    plain = handlers.handle_playbill_kit_add("inst", bundle=_bundle())
    assert plain.automation_template is None and plain.automation_template_content is None
