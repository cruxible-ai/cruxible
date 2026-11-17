"""CLI kit commands: no public push; add carries the upgrade decisions; status checks updates."""

from __future__ import annotations

import hashlib
import json
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
    KitOwnershipConflictRefusal,
    KitPathPlan,
    KitProvider,
    KitProviderFile,
    KitProviderFileBytes,
    KitProviderStatus,
    KitProviderStep,
    KitRequirementStatus,
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


def test_kit_lines_say_what_a_kept_divergence_keeps_and_use_public_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.kits import KitKeptDivergence

    kept = KitKeptDivergence(
        path="claim-types/acme.account/title.json",
        identity="ClaimType:acme.account.title",
        consequence="overwrites_your_edit",
        release_digest="sha256:" + "3" * 64,
    )

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme",
                version="1.1.0",
                status="would_propose",
                plan=(
                    KitPathPlan(
                        path=kept.path,
                        action="keep",
                        identity=kept.identity,
                        consequence="overwrites_your_edit",
                    ),
                    KitPathPlan(
                        path="capture-contracts/playbill.coordinator-self-source-v1.json",
                        action="add",
                        identity="CaptureContract:playbill.coordinator-self-source-v1",
                    ),
                ),
            )

        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                kits=(
                    InstalledKit(
                        kit_id="acme",
                        version="1.1.0",
                        content_digest="sha256:" + "1" * 64,
                        kept=(kept,),
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "acme-1.1.0")
    )

    added = CliRunner().invoke(cli, [*BASE, "add", "acme-1.1.0"])
    assert added.exit_code == 0, added.output
    assert "  keep: ClaimType:acme.account.title (keeps your edit)" in added.output
    assert "  add: CaptureContract:cruxible.coordinator-self-source-v1" in added.output
    assert "overwrites your edit" not in added.output and "playbill." not in added.output
    status = CliRunner().invoke(cli, [*BASE, "status", "--offline"])
    assert status.exit_code == 0, status.output
    assert "  kept: ClaimType:acme.account.title (your edit)" in status.output


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


def test_kit_collision_exposes_the_shared_refusal_and_next_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = KitOwnershipConflictRefusal(
        our_prefixes=("acme.account.",),
        owning_kit_id="acme",
        owning_prefix="acme.",
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

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "extra")
    )
    result = CliRunner().invoke(cli, [*BASE, "add", "extra"])
    assert result.exit_code == 0, result.output
    assert refusal.code in result.output
    assert f"Next: {refusal.repair}" in result.output

    result = CliRunner().invoke(cli, [*BASE, "add", "extra", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["refusals"] == [refusal.model_dump(mode="json")]


def test_kit_takeover_points_to_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme",
                version="1.0.0",
                status="would_propose",
                plan=(
                    KitPathPlan(
                        path="claim-types/acme.account/seats.json",
                        identity="ClaimType:acme.account.seats",
                        action="replace",
                        consequence="takes_over_outside_definition",
                    ),
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "acme")
    )
    result = CliRunner().invoke(cli, [*BASE, "add", "acme"])
    assert result.exit_code == 0, result.output
    assert "Next: rerun cruxible kit add with --keep ClaimType:acme.account.seats" in result.output


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
        operations=("local.increment",),
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

    def install_provider(self, *args: Any, **kwargs: Any) -> None:
        pytest.fail("Adapter staging must not install or execute provider packages")

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
    assert (request.kit_id, request.version, request.owns) == ("acme", "1.0.0", ("acme.",))
    assert provider.delivery == "bundled"
    assert (
        provider.lock_digest
        == "sha256:" + hashlib.sha256(files["kit-call-0.2.0.uv.lock"]).hexdigest()
    )
    assert (
        provider.wheel.digest
        == "sha256:" + hashlib.sha256(files["kit_call-0.2.0-py3-none-any.whl"]).hexdigest()
    )
    assert provider.wheel.filename == "kit_call-0.2.0-py3-none-any.whl"
    assert [item.filename for item in provider.dependencies] == [
        "cruxible_provider_runtime-0.2.0-py3-none-any.whl"
    ]
    assert sorted(client.stored) == sorted(files.values())
    assert "1 provider package(s), content sha256:" in result.output
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
    assert [request.dry_run for request in client.sent] == [None, False]
    assert [request.source for request in client.sent] == ["acme", "acme"]
    assert "acme 1.0.0: awaiting_providers" in committed.output
    assert "provider kit-call (kit-call 0.2.0): awaiting approval, proposal prop-1" in (
        committed.output
    )

    from pydantic import ValidationError

    invalid = _provider_bundle().model_dump(mode="json")
    invalid["manifest"]["providers"][0]["interfaces"] = ["local.increment"]

    def invalid_source(_reference):
        return KitBundle.model_validate(invalid), "invalid"

    client.stored.clear()
    client.sent.clear()
    monkeypatch.setattr("cruxible_core.cli.commands.playbill.resolve_kit", invalid_source)
    refused = CliRunner().invoke(cli, [*BASE, "add", "invalid", "--commit"])
    assert refused.exit_code != 0 and "interfaces" in refused.output
    assert client.stored == [] and client.sent == []
    with pytest.raises(ValidationError) as typed:
        KitBundle.model_validate(invalid)
    assert typed.value.errors()[0]["loc"] == ("manifest", "providers", 0, "interfaces")


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


def test_kit_status_shows_each_required_kit_and_whether_it_is_met(
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
                        requires=(
                            KitRequirementStatus(
                                kit_id="base",
                                min_version="1.0.0",
                                installed_version="1.2.0",
                                met=True,
                            ),
                            KitRequirementStatus(kit_id="other", min_version="2.0.0", met=False),
                        ),
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert "  requires base >= 1.0.0: met (installed 1.2.0)\n" in result.output
    assert "  requires other >= 2.0.0: not met (not installed)\n" in result.output
    # S4: the digest is labelled: the content digest kit add pins, not an image digest.
    assert f"acme 1.0.0 content sha256:{'1' * 64}; update check local source\n" in result.output


def test_kit_add_preview_shows_requires_met_and_one_heading_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1-S3: the preview says what a commit does now, shows requires and whether it is
    met, and spells every heading one way (``Document:`` beside ``ClaimType:``)."""

    from cruxible_core.service.kits import KIT_PREVIEW_AWAITING_INSTALL

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme",
                version="1.0.0",
                status="would_propose",
                requires=(
                    KitRequirementStatus(
                        kit_id="base", min_version="1.0.0", installed_version="1.0.0", met=True
                    ),
                ),
                plan=(
                    KitPathPlan(
                        path="claim-types/acme/plan.json",
                        action="add",
                        identity="ClaimType:acme.plan",
                    ),
                    KitPathPlan(
                        path="documents/acme.guide.json",
                        action="add",
                        identity="document:acme.guide",
                    ),
                ),
                detail=KIT_PREVIEW_AWAITING_INSTALL,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "acme-1.0.0")
    )

    result = CliRunner().invoke(cli, [*BASE, "add", "acme-1.0.0"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "  requires base >= 1.0.0: met (installed 1.0.0)" in lines
    assert "  requires: base >= 1.0.0" not in lines
    assert "ClaimType:" in lines and "Document:" in lines and "document:" not in lines
    assert "Contracts first" not in result.output
    assert KIT_PREVIEW_AWAITING_INSTALL in lines


def test_a_registry_refusal_prints_its_code_without_the_class_name() -> None:
    import click

    from cruxible_client.artifacts.registry import RegistryRefusal
    from cruxible_core.cli.main import handle_errors

    @click.command()
    @handle_errors
    def fetch() -> None:
        raise RegistryRefusal(
            "not_found", "ghcr.io/acme/kit:1 is not published (HTTP 404)", status=404
        )

    result = CliRunner().invoke(fetch)

    assert result.exit_code == 1
    assert result.stderr == (
        "Error: cruxible.kit.not_found: ghcr.io/acme/kit:1 is not published (HTTP 404)\n"
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_kit_pull_from_a_kit_directory_writes_what_a_registry_pull_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.artifacts import pack_artifact
    from cruxible_client.contracts.kits import KitFileBytes, kit_file_entry
    from cruxible_client.kits import KIT_ARTIFACT, write_kit_directory

    files = {
        "README.md": b"# acme\n",
        "walkthroughs.md": b"# walkthroughs\n",
        "examples/automation.json": b"{}\n",
        "reference.md": b"# reference\n",
        "skills/acme-triage/SKILL.md": b"---\nname: acme-triage\n---\n",
    }
    plain = _bundle()
    bundle = KitBundle(
        manifest=plain.manifest.model_copy(
            update={
                "files": tuple(
                    kit_file_entry(path, content) for path, content in sorted(files.items())
                )
            }
        ),
        artifacts=plain.artifacts,
        files=tuple(KitFileBytes.of(path, content) for path, content in sorted(files.items())),
    )
    image = pack_artifact(KIT_ARTIFACT, bundle)
    monkeypatch.setattr(
        "cruxible_client.kits.fetch_kit_image",
        lambda source, registry=None: (image, f"ghcr.io/cruxible-ai/kits/acme@{image.digest}"),
    )
    runner = CliRunner()
    pulled = runner.invoke(cli, [*BASE, "pull", "acme:1.0.0", "--out", str(tmp_path / "reg")])
    assert pulled.exit_code == 0, pulled.output

    source = tmp_path / "acme"
    write_kit_directory(bundle, source)
    (source / "source").mkdir()
    (source / "source" / "build.py").write_text("print()\n")  # not kit content
    local = runner.invoke(cli, [*BASE, "pull", str(source), "--out", str(tmp_path / "local")])
    assert local.exit_code == 0, local.output
    assert local.output == "acme: 1 artifacts\n"
    expected = _tree(tmp_path / "reg")
    assert "walkthroughs.md" in expected and "skills/acme-triage/SKILL.md" in expected
    assert _tree(tmp_path / "local") == expected

    # A layout from the directory carries the release's digest, and pulls back the same files.
    as_layout = runner.invoke(
        cli, [*BASE, "pull", str(source), "--out", str(tmp_path / "layout"), "--layout", "--json"]
    )
    assert as_layout.exit_code == 0, as_layout.output
    from cruxible_client.artifacts import read_layout

    assert read_layout(tmp_path / "layout").digest == image.digest
    again = runner.invoke(
        cli, [*BASE, "pull", str(tmp_path / "layout"), "--out", str(tmp_path / "again")]
    )
    assert again.exit_code == 0, again.output
    assert _tree(tmp_path / "again") == expected


def test_kit_pull_from_a_bad_path_refuses_with_the_kit_forms(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = runner.invoke(cli, [*BASE, "pull", "./no-such-kit", "--out", str(tmp_path / "out")])
    assert missing.exit_code != 0
    assert "cruxible.kit.source_invalid: no kit directory or layout at ./no-such-kit" in (
        missing.stderr
    )
    assert "a kit is a kit directory (holding cruxible-kit.json)" in missing.stderr

    (tmp_path / "empty").mkdir()
    empty = runner.invoke(
        cli, [*BASE, "pull", str(tmp_path / "empty"), "--out", str(tmp_path / "out")]
    )
    assert empty.exit_code != 0
    assert "holds no cruxible-kit.json" in empty.stderr
    assert not (tmp_path / "out").exists()


def test_kit_build_sends_the_automation_template_and_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.contracts.kits import KitFileBytes, kit_file_entry

    template = tmp_path / "automation.json"
    content = '{"kind": "change_set", "members": []}\n'
    template.write_text(content)
    plain = _bundle()
    built = KitBundle(
        manifest=plain.manifest.model_copy(
            update={
                "files": (kit_file_entry("automation.json", content.encode()),),
                "automation_template": "automation.json",
            }
        ),
        artifacts=plain.artifacts,
        files=(KitFileBytes.of("automation.json", content.encode()),),
    )

    class StubClient(_StagingClient):
        def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
            self.sent.append(request)
            return KitBuildResult(bundle=built)

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
            "--automation-template",
            str(template),
        ],
    )

    assert result.exit_code == 0, result.output
    (request,) = client.sent
    assert request.automation_template is not None
    assert (request.automation_template.path, request.automation_template.content) == (
        "automation.json",
        content,
    )
    assert f"automation template: {out / 'automation.json'} (never installed" in result.output
    assert (out / "automation.json").read_text() == content

    pulled = CliRunner().invoke(cli, [*BASE, "pull", str(out), "--out", str(tmp_path / "pull")])
    assert pulled.exit_code == 0, pulled.output
    assert f"automation template: {tmp_path / 'pull' / 'automation.json'}" in pulled.output
    assert (tmp_path / "pull" / "automation.json").read_text() == content


TEMPLATE = '{"kind": "change_set", "members": []}\n'


def _template_bundle() -> KitBundle:
    from cruxible_client.contracts.kits import KitFileBytes, kit_file_entry

    plain = _bundle()
    return KitBundle(
        manifest=plain.manifest.model_copy(
            update={
                "files": (kit_file_entry("automation.json", TEMPLATE.encode()),),
                "automation_template": "automation.json",
            }
        ),
        artifacts=plain.artifacts,
        files=(KitFileBytes.of("automation.json", TEMPLATE.encode()),),
    )


class _DaemonLikeClient(_StagingClient):
    """Answers kit add as the daemon does: it names the template's path, never its text."""

    def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
        self.sent.append(request)
        return KitChangeResult(
            kit_id="acme",
            version="1.0.0",
            status="accepted" if request.dry_run is False else "would_propose",
            automation_template=request.bundle.manifest.automation_template,
        )


def test_kit_add_returns_a_kit_directorys_automation_template_and_names_its_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.kits import write_kit_directory

    kit_dir = tmp_path / "acme"
    write_kit_directory(_template_bundle(), kit_dir)
    client = _DaemonLikeClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    runner = CliRunner()

    for extra in ([], ["--commit"]):
        added = runner.invoke(cli, [*BASE, "add", str(kit_dir), *extra, "--json"])
        assert added.exit_code == 0, added.output
        payload = json.loads(added.stdout)
        assert payload["automation_template"] == "automation.json"
        assert payload["automation_template_content"] == TEMPLATE
    text = runner.invoke(cli, [*BASE, "add", str(kit_dir)])
    assert text.exit_code == 0, text.output
    on_disk = kit_dir / "automation.json"
    assert f"automation template: {on_disk} (never installed" in text.output
    # Tailor a workspace copy, never the kit's own (coldcli-03).
    assert "copy it into your workspace, tailor the copy" in text.output
    assert f"authoring submit {on_disk}" not in text.output
    assert TEMPLATE.strip() not in text.output
    # The text came from the adapter's copy: kit add never sends inert files.
    assert [request.dry_run for request in client.sent] == [None, False, None]
    assert all(not request.bundle.files for request in client.sent)


def test_kit_add_by_registry_reference_returns_the_template_and_says_how_to_pull_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.artifacts import pack_artifact
    from cruxible_client.kits import KIT_ARTIFACT

    image = pack_artifact(KIT_ARTIFACT, _template_bundle())
    pinned = f"ghcr.io/cruxible-ai/kits/acme@{image.digest}"
    monkeypatch.setattr(
        "cruxible_client.kits.fetch_kit_image", lambda source, registry=None: (image, pinned)
    )
    client = _DaemonLikeClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    runner = CliRunner()

    added = runner.invoke(cli, [*BASE, "add", "acme:1.0.0", "--json"])
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout)["automation_template_content"] == TEMPLATE
    text = runner.invoke(cli, [*BASE, "add", "acme:1.0.0"])
    assert text.exit_code == 0, text.output
    assert (
        f"automation template: automation.json (never installed: cruxible kit pull {pinned} "
        "--out DIR writes it, and --json output carries its content" in text.output
    )


def test_kit_add_of_a_kit_without_a_template_carries_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.kits import write_kit_directory

    kit_dir = tmp_path / "acme"
    write_kit_directory(_bundle(), kit_dir)
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client", lambda: _DaemonLikeClient()
    )

    added = CliRunner().invoke(cli, [*BASE, "add", str(kit_dir), "--json"])
    assert added.exit_code == 0, added.output
    payload = json.loads(added.stdout)
    assert "automation_template" not in payload
    assert "automation_template_content" not in payload


def test_a_kit_directory_whose_template_differs_from_its_manifest_refuses_before_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.kits import write_kit_directory

    kit_dir = tmp_path / "acme"
    write_kit_directory(_template_bundle(), kit_dir)
    (kit_dir / "automation.json").write_text('{"kind": "change_set", "members": [1]}\n')
    client = _DaemonLikeClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    refused = CliRunner().invoke(cli, [*BASE, "add", str(kit_dir), "--commit"])
    assert refused.exit_code != 0
    assert "cruxible.kit.source_invalid" in refused.stderr
    assert "automation.json differs from its sha256" in refused.stderr
    assert client.sent == [] and client.stored == []


def test_kit_add_base_sends_the_bundled_base_kit_without_reading_a_directory_or_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.kits.base_kit import bundled_base_kit

    sent: list[KitAddRequest] = []

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            sent.append(request)
            return KitChangeResult(
                kit_id="base", version="1.0.0", status="would_propose", transition="upgrade"
            )

    def no_other_source(_kit: str) -> Any:
        raise AssertionError("kit add base reads the bundled copy, never a directory or registry")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr("cruxible_core.cli.commands.playbill.resolve_kit", no_other_source)

    result = CliRunner().invoke(cli, [*BASE, "add", "base"])

    assert result.exit_code == 0, result.output
    (request,) = sent
    assert request.source == "bundled"
    assert request.bundle.manifest == bundled_base_kit().manifest
    assert "base 1.0.0: would_propose (upgrade)" in result.output


def test_kit_status_compares_the_base_kit_with_the_bundled_copy_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                kits=(
                    InstalledKit(
                        kit_id="base",
                        version="0.9.0",
                        content_digest="sha256:" + "1" * 64,
                        source="bundled",
                        latest_available="1.0.0",
                        update_check="checked",
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert "base 0.9.0" in result.output
    assert "latest 1.0.0 available" in result.output
    assert "source: bundled" in result.output


@pytest.mark.parametrize(
    ("available", "shown"),
    [
        ("sha256:" + "2" * 64, "; differs from the available 1.0.0, content sha256:" + "2" * 64),
        ("sha256:" + "1" * 64, "; up to date"),
    ],
)
def test_kit_status_says_a_same_version_receipt_differs_from_the_bundled_content(
    monkeypatch: pytest.MonkeyPatch, available: str, shown: str
) -> None:
    """Base 1.0.0 installed with other content than the bundled 1.0.0 is not up to date."""

    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                kits=(
                    InstalledKit(
                        kit_id="base",
                        version="1.0.0",
                        content_digest="sha256:" + "1" * 64,
                        source="bundled",
                        latest_available="1.0.0",
                        latest_content_digest=available,
                        update_check="checked",
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert f"base 1.0.0 content sha256:{'1' * 64}{shown}\n" in result.output


def test_kit_status_shows_a_pending_kit_and_the_command_that_approves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under independent approval, kit status said "No kits installed." while the base
    kit's install proposal waited; init had reported it."""

    from cruxible_client.contracts.kits import KitResumeStatus
    from cruxible_core.kits.base_kit_pin import BASE_KIT_PIN

    proposal = "sha256:" + "c" * 64

    class StubClient:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(
                resumes=(
                    KitResumeStatus(
                        kit_id="base",
                        version="1.0.0",
                        state="proposed",
                        actor_id="owner",
                        recorded_at="2026-11-15T00:00:00Z",
                        proposal_id=proposal,
                        proposal_open=True,
                        source="bundled",
                    ),
                    KitResumeStatus(
                        kit_id="acme",
                        version="1.0.0",
                        state="proposed",
                        actor_id="owner",
                        recorded_at="2026-11-15T00:00:00Z",
                        proposal_id="sha256:" + "d" * 64,
                    ),
                    KitResumeStatus(
                        kit_id="ops",
                        version="2.0.0",
                        state="proposed",
                        actor_id="owner",
                        recorded_at="2026-11-15T00:00:00Z",
                        proposal_id="sha256:" + "a" * 64,
                        proposal_awaiting_readmit=True,
                        # A local directory named base records "base" as its origin.
                        source="base",
                        content_digest="sha256:" + "b" * 64,
                    ),
                    KitResumeStatus(
                        kit_id="base",
                        version=BASE_KIT_PIN.version,
                        state="proposed",
                        actor_id="owner",
                        recorded_at="2026-11-15T00:00:00Z",
                        proposal_id="sha256:" + "9" * 64,
                        proposal_awaiting_readmit=True,
                        source="bundled",
                        content_digest=BASE_KIT_PIN.content_digest,
                    ),
                )
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "status", "--offline"])

    assert result.exit_code == 0, result.output
    assert "No kits installed." not in result.output
    assert f"base 1.0.0: kit add proposed (as owner); proposal {proposal}\n" in result.output
    assert (
        f"  next: cruxible proposal approve {proposal} --signer-id ID --key FILE "
        "(the approval that completes it activates it)\n" in result.output
    )
    # A proposal no longer open names no approve step.
    assert result.output.count("next: cruxible proposal approve") == 1
    # One awaiting readmit names the add to re-run: the concrete `base` operand only
    # for the bundled base kit (id and content digest), else the placeholder with
    # the recorded origin beside it (review F-009).
    assert (
        "  awaiting readmit: accepted state changed what it touches; "
        "next: cruxible kit add '<KIT>' (recorded origin: base)\n" in result.output
    )
    assert (
        "  awaiting readmit: accepted state changed what it touches; "
        "next: cruxible kit add base\n" in result.output
    )


def test_kit_help_says_what_a_default_provider_stands_for_now() -> None:
    """Providers no longer implement interfaces: operations serve capabilities, and a
    default provider is skipped when installed operations already serve every slot."""

    import inspect

    from cruxible_core.mcp import tools
    from cruxible_core.service.kits import service_add_kit

    for command in ("add", "build"):
        result = CliRunner().invoke(cli, ["kit", command, "--help"])
        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        assert "interfaces" not in text
        assert "serve every slot's capability" in text
    assert "with interfaces" not in inspect.getsource(tools)
    assert "operation Contracts first" not in (service_add_kit.__doc__ or "")


@pytest.mark.parametrize(("status", "exit_code"), [("would_block", 0), ("blocked", 1)])
def test_kit_remove_exits_1_only_when_a_commit_was_blocked(
    monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int
) -> None:
    from cruxible_client.contracts.kits import KitRemoveRequest

    class StubClient:
        def remove_kit(self, instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme",
                version="1.0.0",
                status=status,  # type: ignore[arg-type]
                # A refused commit's admission id: nothing to act on (sec N1).
                proposal_id="sha256:" + "e" * 64 if status == "blocked" else None,
                detail="Refused: cruxible.change_set.incomplete_closure: 1 live artifact(s)",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    commit = ["--commit", "--at", "abcdef123456"] if status == "blocked" else []
    result = CliRunner().invoke(cli, [*BASE, "remove", "acme", *commit])

    assert result.exit_code == exit_code, result.output
    assert f"acme 1.0.0: {status}" in result.output
    assert "Refused: cruxible.change_set.incomplete_closure" in result.output
    assert "Proposal:" not in result.output


def test_a_blocked_kit_change_names_a_proposal_it_left_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review F-006: accepted state moved after admission, so the proposal stays open;
    the blocked answer names it and how to withdraw it."""

    from cruxible_client.contracts.kits import KitRemoveRequest

    proposal = "sha256:" + "f" * 64

    class StubClient:
        def remove_kit(self, instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme",
                version="1.0.0",
                status="blocked",
                proposal_id=proposal,
                proposal_open=True,
                detail="Accepted state moved during kit add; preview the kit again.",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(cli, [*BASE, "remove", "acme", "--commit", "--at", "abcdef123456"])

    assert result.exit_code == 1, result.output
    assert (
        f"Proposal: {proposal} (open; withdraw it: cruxible proposal withdraw {proposal} "
        "--reason '<TEXT>')" in result.output
    )


def test_a_blocked_kit_remove_prints_its_refusal_first_and_the_plan_as_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retire/keep list of a kit that owns hundreds of paths buried the refusal
    under it; the refusal and its repair come first, then counts per kind, and
    --detail lists each path after them (ruling 8)."""

    from cruxible_client.contracts.kits import KitPathPlan, KitRemoveRequest

    refusal = (
        "Refused: cruxible.change_set.incomplete_closure: 3 live artifact(s) outside this "
        "change depend on definitions it retires or replaces. Repair: retire the Claims"
    )
    plan = (
        *(
            KitPathPlan(
                path=f"claim-types/acme/c{index}.json",
                action="retire",
                identity=f"ClaimType:acme.c{index}",
            )
            for index in range(40)
        ),
        KitPathPlan(
            path="subject-kinds/acme.account.json",
            action="retire",
            identity="SubjectKind:acme.account",
        ),
        KitPathPlan(
            path="capture-contracts/acme.json",
            action="keep",
            identity="CaptureContract:acme",
            detail="Contract versions are immutable and remain accepted",
        ),
    )

    class StubClient:
        def remove_kit(self, instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
            return KitChangeResult(
                kit_id="acme", version="1.0.0", status="blocked", plan=plan, detail=refusal
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    commit = ["--commit", "--at", "abcdef123456"]

    brief = CliRunner().invoke(cli, [*BASE, "remove", "acme", *commit])
    listed = CliRunner().invoke(cli, [*BASE, "remove", "acme", *commit, "--detail"])

    assert brief.exit_code == listed.exit_code == 1, brief.output
    lines = brief.output.splitlines()
    start = lines.index("acme 1.0.0: blocked")
    assert lines[start : start + 5] == [
        "acme 1.0.0: blocked",
        refusal,
        "Plan:",
        "  keep: 1 (CaptureContract 1)",
        "  retire: 41 (ClaimType 40, SubjectKind 1)",
    ]
    assert "(--detail lists each path)" in brief.output
    assert "acme.c7" not in brief.output
    assert listed.output.index(refusal) < listed.output.index("retire: ClaimType:acme.c7")
    assert "(--detail lists each path)" not in listed.output


def test_kit_build_carries_named_files_listed_with_their_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kit-build-files: `--file PATH` copies plain files into the kit and lists each in
    the manifest's files with its sha256, as a released kit's files are listed."""

    from cruxible_client.contracts.kits import KitBuildResult
    from cruxible_client.kits import read_kit_directory

    workspace = tmp_path / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "README.md").write_text("# Acme kit\n")
    (workspace / "docs" / "guide.md").write_text("How to use it.\n")
    monkeypatch.chdir(workspace)

    class StubClient:
        def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
            return KitBuildResult(bundle=_bundle())

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    out = tmp_path / "kit"
    argv = [*BASE, "build", "--id", "acme", "--version", "1.0.0", "--owns", "acme."]
    result = CliRunner().invoke(
        cli, [*argv, "--out", str(out), "--file", "README.md", "--file", "docs/guide.md"]
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads((out / "cruxible-kit.json").read_text())
    assert [(item["path"], item["role"], item["sha256"]) for item in manifest["files"]] == [
        ("README.md", "readme", "sha256:" + hashlib.sha256(b"# Acme kit\n").hexdigest()),
        (
            "docs/guide.md",
            "reference",
            "sha256:" + hashlib.sha256(b"How to use it.\n").hexdigest(),
        ),
    ]
    assert (out / "docs" / "guide.md").read_text() == "How to use it.\n"
    assert read_kit_directory(out).manifest.content_digest == _bundle().manifest.content_digest

    (tmp_path / "outside.md").write_text("elsewhere\n")
    for path, why in (("missing.md", "is not a file"), ("../outside.md", "outside the workspace")):
        refused = CliRunner().invoke(
            cli, [*argv, "--out", str(tmp_path / f"refused-{why[:4]}"), "--file", path]
        )
        assert refused.exit_code == 1, refused.output
        assert "cruxible.kit.file_refused" in refused.output and why in refused.output
        assert "Repair:" in refused.output
        assert not (tmp_path / f"refused-{why[:4]}").exists()


def test_kit_add_counts_unchanged_paths_and_lists_them_only_with_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1: a large release's preview lists what changes and counts the rest."""

    from cruxible_client.kits import compact_kit_change

    answer = KitChangeResult(
        kit_id="acme",
        version="1.1.0",
        status="would_propose",
        plan=(
            KitPathPlan(
                path="claim-types/acme.account/plan.json",
                action="replace",
                identity="ClaimType:acme.account.plan",
            ),
            *(
                KitPathPlan(
                    path=f"claim-types/acme.account/f{index}.json",
                    action="unchanged",
                    identity=f"ClaimType:acme.account.f{index}",
                )
                for index in range(175)
            ),
        ),
    )

    class StubClient:
        def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
            return answer

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.resolve_kit", lambda _kit: (_bundle(), "acme-1.1.0")
    )

    brief = CliRunner().invoke(cli, [*BASE, "add", "./acme"])
    full = CliRunner().invoke(cli, [*BASE, "add", "./acme", "--detail"])
    as_json = CliRunner().invoke(cli, [*BASE, "add", "./acme", "--json"])

    assert brief.exit_code == full.exit_code == as_json.exit_code == 0, brief.output
    assert "replace: ClaimType:acme.account.plan" in brief.output
    assert "acme.account.f7" not in brief.output
    assert "unchanged: 175 (--detail lists each)" in brief.output
    assert "unchanged: ClaimType:acme.account.f7" in full.output
    assert "unchanged: 175" not in full.output
    payload = json.loads(as_json.output[as_json.output.index("{") :])
    assert payload["unchanged"] == 175 and len(payload["plan"]) == 1
    compact = compact_kit_change(answer)
    assert compact.unchanged == 175 and compact_kit_change(compact) == compact


def test_proposal_list_names_changed_paths_by_their_public_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review F-007: the awaiting-readmit row printed a playbill. path."""

    from cruxible_client import contracts
    from cruxible_client.contracts.get_text import definition_path_name

    paths = (
        "capture-contracts/playbill.coordinator-self-source-v1.json",
        "claim-types/dev.roadmap_item/owner.json",
        "contracts/acme.row/v2.json",
        "subjects/dev.item/x.json",
        "governance/approval-policy.json",
    )

    class StubClient:
        def list_proposals(self, instance_id: str, **_: Any) -> contracts.ProposalList:
            return contracts.ProposalList(
                coordinate=contracts.AcceptedCoordinate(
                    git_oid="a" * 40,
                    semantic_root="sha256:" + "1" * 64,
                    generation_root="sha256:" + "2" * 64,
                    compiler_digest="sha256:" + "3" * 64,
                ),
                entries=[
                    contracts.ProposalListEntry(
                        proposal_id="sha256:" + "e" * 64,
                        actor_id="owner",
                        target_ref="refs/proposals/owner/x",
                        admitted_at=None,
                        verdict="candidate",
                        status="awaiting_readmit",
                        stale_paths=paths,
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    server = BASE[:-1]
    text = CliRunner().invoke(cli, [*server, "proposal", "list"])
    shown = CliRunner().invoke(cli, [*server, "proposal", "list", "--json"])

    assert text.exit_code == 0, text.output
    assert "playbill." not in text.output
    assert (
        "changed since its base: CaptureContract:cruxible.coordinator-self-source-v1, "
        "ClaimType:dev.roadmap_item.owner, Contract:acme.row@2, dev.item/x, "
        "governance/approval-policy.json" in text.output
    )
    assert json.loads(shown.output)["entries"][0]["stale_paths"] == list(paths)
    assert definition_path_name("notes/NOTE-" + "1" * 32 + ".json") == "Note:NOTE-" + "1" * 32
