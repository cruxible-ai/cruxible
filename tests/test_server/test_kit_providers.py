"""Kits that carry provider packages: the wheel and lock reproduce the same definitions."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.artifacts import pack_artifact, unpack_artifact
from cruxible_client.contracts.authoring.inputs import BlueprintInstanceInput
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitBundle,
    KitChangeResult,
)
from cruxible_client.contracts.procedures.artifacts import parse_procedure
from cruxible_client.contracts.providers import (
    Provider,
    parse_provider,
    provider_digest,
    provider_implementation_digest,
)
from cruxible_client.kits import (
    KIT_ARTIFACT,
    read_kit_directory,
    stage_kit_providers,
    write_kit_directory,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.provider_wheels import install_provider_wheel
from cruxible_core.errors import RequestRefusedError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from tests.support.provider_checkout import ProviderCheckout


def _installer(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str, Path]]:
    """One fresh host under ``root`` whose operator resolves registry wheels from PyPI."""

    from tests.test_server.conftest import _playbill_http

    config = root / "server-state" / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {"provider_index_urls": ["https://pypi.org/simple", "https://files.pythonhosted.org/"]}
        )
    )
    yield from _playbill_http(root, monkeypatch)


def _client(http: TestClient) -> CruxibleClient:
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http  # type: ignore[assignment]
    return client


def _provider_definitions(instance_id: str) -> dict[str, bytes]:
    instance = get_playbill_manager().get(instance_id)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    return {
        path: tree[path]
        for path in sorted(tree)
        if path.startswith(("providers/", "provider-interfaces/"))
        and "workspace.file" not in path
        and "builtin" not in path
    }


def test_the_same_wheel_and_lock_reproduce_identical_definitions_on_fresh_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """The precondition for a kit pinning its bundled provider: the transfer install of
    one wheel and lock writes byte-identical Provider and ProviderInterface artifacts on
    any fresh instance of the same daemon environment."""

    wheels = provider_checkout.wheels
    noop = next(wheels.glob("cruxible_provider_noop-*.whl"))
    runtime = next(wheels.glob("cruxible_provider_runtime-*.whl"))
    lock = provider_checkout.repository / "packages/cruxible-provider-noop/uv.lock"
    seen = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        opened = _installer(root, monkeypatch)
        http, instance_id, _reviewer = next(opened)
        result = install_provider_wheel(
            _client(http), instance_id, wheel=noop, lock=lock, dependency_wheels=(runtime,)
        )
        assert result.registered, result
        seen.append(_provider_definitions(instance_id))
        for _ in opened:
            pass
    assert seen[0] and set(seen[0]) == set(seen[1])
    assert seen[0] == seen[1]
    # The Provider digest is per environment: its local_env pins one
    # materialization keyed by this platform, machine and Python, over the full
    # PEP 508 marker set. The implementation digest is the portable part.
    provider = parse_provider(
        seen[0]["providers/cruxible-provider-noop.json"],
        path="providers/cruxible-provider-noop.json",
    )
    assert isinstance(provider, Provider)
    local_env = provider.runtime_artifact.local_env
    assert local_env is not None
    environment = (
        f"{sys.platform}-{platform.machine().lower()}-"
        f"cp{sys.version_info.major}{sys.version_info.minor}"
    )
    assert set(local_env.materialization_digests) == {environment}
    (implementation,) = provider.implementations
    assert implementation.implementation_digest == provider_implementation_digest(
        interface_id=implementation.interface_id,
        interface_digest=implementation.interface_digest,
        entrypoint=implementation.entrypoint,
        distribution_sha256=provider.runtime_artifact.distribution.sha256,
    )


# --- a kit bundling its provider, end to end ---------------------------------

INTERFACE = "local.increment"
POLICY = "acme.intake"
PROCEDURE = "acme.increment"
BLUEPRINT = "acme.skeleton"
_ZERO = "sha256:" + "0" * 64
_RUN_WALL_CLOCK = {"microseconds": 30_000_000}
_MAX_WALL_CLOCK = {"microseconds": 60_000_000}


def _worlds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any]]:
    """A publisher and a fresh consumer on one daemon whose operator resolves registry
    wheels from PyPI (a plain transferred wheel needs it; a kit's bundled one does not)."""

    from tests.test_server.test_playbill_kits import _fresh_open_worlds

    config = tmp_path / "server-state" / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {"provider_index_urls": ["https://pypi.org/simple", "https://files.pythonhosted.org/"]}
        )
    )
    yield from _fresh_open_worlds(tmp_path, monkeypatch, independent=False)


def _package(root: Path, checkout: ProviderCheckout, *, name: str, increment: int = 1) -> Path:
    """A provider package directory as a kit's source tree holds it: pyproject, uv.lock,
    and in dist/ its wheel beside the runtime wheel its lock names by path."""

    from tests.support.provider_installation import build_local_call

    wheel, _lock = build_local_call(root, checkout.repository, name=name, increment=increment)
    project = wheel.parent.parent
    runtime = next(checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    shutil.copy(runtime, project / "dist" / runtime.name)
    return project


def _install(world: Any, project: Path, *, control_domain: str = "operator") -> None:
    from packaging.utils import canonicalize_name, parse_wheel_filename

    name = tomllib.loads((project / "pyproject.toml").read_text())["project"]["name"]
    wheels = sorted((project / "dist").glob("*.whl"))
    (root,) = (
        path for path in wheels if parse_wheel_filename(path.name)[0] == canonicalize_name(name)
    )
    result = install_provider_wheel(
        _client(world.http),
        world.instance_id,
        wheel=root,
        lock=project / "uv.lock",
        dependency_wheels=tuple(path for path in wheels if path != root),
        control_domain=control_domain,
    )
    assert result.registered, result


def _interface_entry(world: Any) -> dict[str, Any]:
    from cruxible_client.contracts.get_reads import GetRequest

    proof = (
        _client(world.http)
        .get(
            world.instance_id,
            request=GetRequest(ref=f"ProviderInterface:{INTERFACE}", detail="proof"),
        )
        .proof
    )
    assert proof is not None
    return dict(proof["entry"])


def _graph(world: Any, *, provider: str | None) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """The increment graph: bound to ``provider``, or (None) a Blueprint slot."""

    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema

    interface = _interface_entry(world)

    def carried(name: str, role: str) -> dict[str, str]:
        return {"kind": "carried_contract", "name": name, "role": role}

    node: dict[str, Any] = {
        "kind": "call",
        "node_id": "invoke",
        "as": "result",
        "input": {"n": "$input.n"},
        "interface": {
            "kind": "accepted",
            "role": "provider-interface",
            "target": interface["identity"],
        },
        "interface_digest": interface["interface_digest"],
        "contract_in": carried("request", "contract-in"),
        "contract_out": carried("result", "contract-out"),
    }
    if provider is None:
        node["provider"] = {"kind": "slot", "slot_name": "increment"}
    else:
        (row,) = (
            item
            for item in interface["providers"]
            if item["provider_identity"] == f"Provider:{provider}"
        )
        node["provider"] = {
            "kind": "accepted",
            "role": "provider",
            "target": f"Provider:{provider}",
        }
        node["implementation_digest"] = row["implementation_digest"]
    example = procedure_example()
    definition = {
        **example.definition,
        "name": PROCEDURE if provider is not None else BLUEPRINT,
        "graph_format": 6,
        "returns": "result",
        "contract_in": carried("request", "contract-in"),
        "contract_out": carried("result", "contract-out"),
        "nodes": [node],
        "budget": {
            **example.definition["budget"],  # type: ignore[dict-item]
            "wall_clock": _RUN_WALL_CLOCK,
            "max_items": None,
            "max_provider_calls": 1,
            "max_capture_bytes": 1048576,
        },
        "hard_caps": {
            **example.definition["hard_caps"],  # type: ignore[dict-item]
            "max_wall_clock": _MAX_WALL_CLOCK,
            "max_provider_calls": 2,
            "max_capture_bytes": 2097152,
        },
    }
    if provider is None:
        definition["pin_slots"] = [
            {
                "slot_name": "increment",
                "pin_role": "provider",
                "artifact_kind": "Provider",
                "interface_digest": interface["interface_digest"],
            }
        ]
    schema = {"n": PropertySchema(type="int")}
    contracts = (
        CarriedContractInput(name="request", fields=schema),
        CarriedContractInput(name="result", fields=schema),
    )
    return definition, contracts


def _author(world: Any, *, procedure: bool = True) -> None:
    """The kit's definitions: a policy, a Procedure pinning the bundled provider (and
    the policy), and a Blueprint whose one slot takes any provider of its interface."""

    from cruxible_client.authoring.examples import acquisition_policy_example
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.authoring.inputs import BlueprintInput, ProcedureInput

    example = acquisition_policy_example().acquisition_policy
    draft = world.pb.changes(rationale="Ship an increment kit.")
    draft.acquisition_policy(
        example.model_copy(
            update={"identity": ArtifactIdentity(kind="SourceAcquisitionPolicy", name=POLICY)}
        )
    )
    if procedure:
        definition, contracts = _graph(world, provider="kit-call")
        draft.procedure(
            definition=ProcedureInput(
                kind="procedure",
                definition=definition,
                activation_policy="drain",
                contracts=contracts,
                acquisition_policy=POLICY,
            )
        )
    definition, contracts = _graph(world, provider=None)
    draft.procedure(
        definition=BlueprintInput(kind="blueprint", definition=definition, contracts=contracts)
    )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()


def _build(world: Any, *projects: Path, version: str = "1.0.0") -> KitBundle:
    from cruxible_core.cli.provider_wheels import stage_kit_provider_directory

    client = _client(world.http)
    return playbill_api.playbill_kit_build(
        world.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version=version,
            owns=("acme.",),
            providers=tuple(
                stage_kit_provider_directory(client, world.instance_id, project)
                for project in projects
            ),
        ),
    ).bundle


def _add(world: Any, bundle: KitBundle, **options: Any) -> KitChangeResult:
    staged = stage_kit_providers(_client(world.http), world.instance_id, bundle)
    return playbill_api.playbill_kit_add(
        world.instance_id, KitAddRequest(bundle=staged, source="test", **options)
    )


def _tree(world: Any) -> dict[str, bytes]:
    instance = get_playbill_manager().get(world.instance_id)
    return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))


def _run(world: Any, name: str, n: int) -> Any:
    procedure = world.pb.accepted_procedure(name)
    run = procedure.run(input=procedure.input(n=n))
    assert run.status == "succeeded", run
    return run.result.model_dump()


def test_a_kit_bundles_its_provider_installs_it_first_and_its_blueprint_takes_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        bundled = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        # Installed under another control domain, so the publisher's Provider
        # digest differs from the consumer's, as it would on another platform.
        _install(publisher, bundled, control_domain="publisher")
        _author(publisher)

        release = _build(publisher, bundled)

        manifest = release.manifest
        assert [item.path for item in manifest.artifacts] == [
            f"blueprints/{BLUEPRINT}.json",
            f"procedures/{PROCEDURE}.json",
            f"provider-interfaces/{INTERFACE}.json",
            f"source-acquisition-policies/{POLICY}.json",
        ]
        (provider,) = manifest.providers
        assert (provider.provider_id, provider.package, provider.interfaces) == (
            "kit-call",
            "kit-call",
            (INTERFACE,),
        )
        assert provider.wheel.filename.startswith("kit_call-")
        assert [item.filename for item in provider.dependencies] == [
            next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl")).name
        ]
        assert sorted(release.provider_contents()) == sorted(manifest.provider_files())
        # The bundle travels as a directory and as an OCI artifact unchanged.
        write_kit_directory(release, tmp_path / "kit")
        assert read_kit_directory(tmp_path / "kit") == release
        assert (tmp_path / "kit" / "providers" / provider.wheel.filename).is_file()
        assert unpack_artifact(KIT_ARTIFACT, pack_artifact(KIT_ARTIFACT, release)) == release

        preview = _add(consumer, release)
        assert preview.status == "would_propose", preview
        assert [step.action for step in preview.providers] == ["would_install"]
        assert "kit-call" not in str(_tree(consumer).keys())

        added = _add(consumer, release, dry_run=False)

        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        interface_path = f"provider-interfaces/{INTERFACE}.json"
        assert tree[interface_path] == release.contents()[interface_path]
        procedure = parse_procedure(
            tree[f"procedures/{PROCEDURE}.json"], path=f"procedures/{PROCEDURE}.json"
        )
        (provider_pin,) = (pin for pin in procedure.pins if pin.role == "provider")
        here = parse_provider(tree["providers/kit-call.json"], path="providers/kit-call.json")
        assert provider_pin.artifact_digest == provider_digest(here).tagged
        released = parse_procedure(
            release.contents()[f"procedures/{PROCEDURE}.json"], path=f"procedures/{PROCEDURE}.json"
        )
        assert provider_pin.artifact_digest != next(
            pin.artifact_digest for pin in released.pins if pin.role == "provider"
        )
        (status,) = playbill_api.playbill_kit_status(consumer.instance_id).kits
        assert [(item.provider_id, item.state) for item in status.providers] == [
            ("kit-call", "installed")
        ]
        again = _add(consumer, release, dry_run=False)
        assert again.status == "unchanged", again
        assert [step.action for step in again.providers] == ["unchanged"]

        # The kit's Procedure runs on the provider it bundled.
        assert _run(consumer, PROCEDURE, 2) == {"n": 3}

        # A second provider of the same interface fills the Blueprint's slot.
        other = _package(tmp_path / "local", provider_checkout, name="other-call")
        _install(consumer, other)
        draft = consumer.pb.changes(rationale="Increment with the other provider.")
        draft.procedure(
            definition=BlueprintInstanceInput(
                kind="blueprint_instance",
                name="acme.increment-other",
                blueprint=BLUEPRINT,
                bindings={"increment": "other-call"},
            )
        )
        intent = draft.prepare()
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit()
        assert submitted._candidate_status is not None
        consumer.approve(submitted._candidate_status.proposal_id)
        consumer.pb.refresh()
        assert _run(consumer, "acme.increment-other", 5) == {"n": 6}
    finally:
        for _ in opened:
            pass


def test_kit_build_refuses_providers_and_interfaces_it_cannot_reproduce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened = _worlds(tmp_path, monkeypatch)
    publisher, _consumer = next(opened)
    try:
        bundled = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, bundled)
        _author(publisher)
        # The Procedure pins kit-call, which the kit must bundle; another package
        # registering the same interface bytes does not stand in for it.
        alternative = _package(tmp_path / "alternative", provider_checkout, name="other-call")
        with pytest.raises(RequestRefusedError) as unbundled:
            _build(publisher, alternative)
        assert unbundled.value.error_code == "cruxible.kit.provider_not_bundled"
        # Another build of kit-call (same registration, other wheel bytes) is not
        # the one installed here.
        rebuilt = tmp_path / "rebuilt"
        shutil.copytree(bundled, rebuilt)
        (rebuilt / "src/local_call/NOTES.txt").write_text("a later build\n")
        for wheel in (rebuilt / "dist").glob("local_call-*.whl"):
            wheel.unlink()
        for wheel in (rebuilt / "dist").glob("kit_call-*.whl"):
            wheel.unlink()
        uv = shutil.which("uv")
        assert uv is not None
        subprocess.run(
            [uv, "build", "--wheel", "--offline", "--out-dir", str(rebuilt / "dist"), str(rebuilt)],
            check=True,
            capture_output=True,
        )
        with pytest.raises(RequestRefusedError) as differs:
            _build(publisher, rebuilt)
        assert differs.value.error_code == "cruxible.kit.provider_build_differs"
        # The same wheel under another lock is another build too (review F-002).
        relocked = tmp_path / "relocked"
        shutil.copytree(bundled, relocked)
        with (relocked / "uv.lock").open("a") as stream:
            stream.write("\n# another lock identity\n")
        with pytest.raises(RequestRefusedError) as lock_differs:
            _build(publisher, relocked)
        assert lock_differs.value.error_code == "cruxible.kit.provider_build_differs"
        assert "lock" in str(lock_differs.value)
    finally:
        for _ in opened:
            pass


def test_a_carried_interface_must_be_what_a_bundled_wheel_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        installed = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, installed)
        # A Blueprint alone carries the interface, so its bytes decide.
        _author(publisher, procedure=False)
        with pytest.raises(RequestRefusedError) as unbundled:
            _build(publisher)
        assert unbundled.value.error_code == "cruxible.kit.interface_not_bundled"
        # This build's classifier differs, so it registers other interface bytes.
        other = _package(tmp_path / "other", provider_checkout, name="kit-call", increment=2)
        with pytest.raises(RequestRefusedError) as differs:
            _build(publisher, other)
        assert differs.value.error_code == "cruxible.kit.interface_differs"

        # A consumer holding another build of a bundled provider is refused,
        # never silently replaced.
        release = _build(publisher, installed)
        _install(consumer, other)
        refused = _add(consumer, release, dry_run=False)
        assert refused.status == "blocked", refused
        (step,) = refused.providers
        assert step.action == "blocked" and "another build" in (step.detail or "")
        (status,) = playbill_api.playbill_kit_status(consumer.instance_id).kits or (None,)
        assert status is None
    finally:
        for _ in opened:
            pass


def _with_other_runtime_build(project: Path, checkout: ProviderCheckout, root: Path) -> Path:
    """The same package with another build of its runtime dependency: same file name
    and version, other bytes. Its lock pins the runtime by name and version only."""

    source = root / "runtime-source"
    shutil.copytree(checkout.repository / "packages/cruxible-provider-runtime", source)
    (source / "src/cruxible_provider_runtime/another_build.py").write_text("BUILD = 'B'\n")
    other = root / "other-build"
    shutil.copytree(project, other)
    for wheel in (other / "dist").glob("cruxible_provider_runtime-*.whl"):
        wheel.unlink()
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(other / "dist"), str(source)],
        check=True,
        capture_output=True,
    )
    return other


def test_a_bundled_provider_matches_only_with_its_whole_dependency_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Review F-001: the lock pins path dependencies by name and version only, so the
    installed build is matched by its materialization, which names their wheels."""

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        other = _with_other_runtime_build(project, provider_checkout, tmp_path)
        (runtime_a,) = (project / "dist").glob("cruxible_provider_runtime-*.whl")
        (runtime_b,) = (other / "dist").glob("cruxible_provider_runtime-*.whl")
        assert runtime_a.name == runtime_b.name
        assert runtime_a.read_bytes() != runtime_b.read_bytes()
        _install(publisher, project)
        _author(publisher)
        # Build: the bundled runtime B is not the closure installed here (A).
        with pytest.raises(RequestRefusedError) as differs:
            _build(publisher, other)
        assert differs.value.error_code == "cruxible.kit.provider_build_differs"
        assert "dependency closure" in str(differs.value)

        # Add: the consumer runs kit-call over runtime B; the kit bundles A.
        release = _build(publisher, project)
        _install(consumer, other)
        before = _tree(consumer)
        refused = _add(consumer, release, dry_run=False)
        assert refused.status == "blocked", refused
        (step,) = refused.providers
        assert step.action == "blocked" and "dependency closure" in (step.detail or "")
        assert _tree(consumer) == before
    finally:
        for _ in opened:
            pass


def test_kit_add_refuses_a_manifest_its_staged_provider_files_do_not_reproduce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Review F-005: the staged wheel is inspected before anything is installed."""

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        _author(publisher)
        release = _build(publisher, project)
        wrong = release.manifest.providers[0].model_copy(
            update={"provider_id": "fictional", "package": "fictional", "version": "9.9.9"}
        )
        release = release.model_copy(
            update={"manifest": release.manifest.model_copy(update={"providers": (wrong,)})}
        )
        installs: list[object] = []
        monkeypatch.setattr(
            playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
        )
        before = _tree(consumer)
        with pytest.raises(RequestRefusedError) as refused:
            _add(consumer, release, dry_run=False)
        assert refused.value.error_code == "cruxible.kit.provider_manifest_mismatch"
        assert installs == []
        assert _tree(consumer) == before
    finally:
        for _ in opened:
            pass


def test_kit_add_rechecks_each_provider_is_live_before_proposing_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Review F-005: an install reporting success without the bundled Provider live
    proposes no definitions."""

    from cruxible_client.contracts.provider_installation import ProviderInstallResult

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        _author(publisher)
        release = _build(publisher, project)
        monkeypatch.setattr(
            playbill_api,
            "service_install_provider",
            lambda *a, **k: ProviderInstallResult(
                installation_id=_ZERO,
                provider_id="kit-call",
                status="ready",
                installed=True,
                registered=True,
            ),
        )
        before = _tree(consumer)
        with pytest.raises(RequestRefusedError) as refused:
            _add(consumer, release, dry_run=False)
        assert refused.value.error_code == "cruxible.kit.provider_not_installed"
        assert _tree(consumer) == before
    finally:
        for _ in opened:
            pass


@pytest.mark.parametrize("prior", ["overlapping kit", "newer release"])
def test_refusals_the_current_state_decides_come_before_any_provider_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_checkout: ProviderCheckout,
    prior: str,
) -> None:
    """Review F-006: ownership overlap and a disallowed downgrade install nothing."""

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        _author(publisher)
        release = _build(publisher, project)
        policy_path = f"source-acquisition-policies/{POLICY}.json"
        installed = KitBundle(
            manifest=release.manifest.model_copy(
                update={
                    "kit_id": "prior" if prior == "overlapping kit" else "acme",
                    "version": "1.0.0" if prior == "overlapping kit" else "2.0.0",
                    "providers": (),
                    "artifacts": tuple(
                        item for item in release.manifest.artifacts if item.path == policy_path
                    ),
                }
            ),
            artifacts=tuple(item for item in release.artifacts if item.path == policy_path),
        )
        first = playbill_api.playbill_kit_add(
            consumer.instance_id, KitAddRequest(bundle=installed, dry_run=False)
        )
        assert first.status == "accepted", first
        installs: list[object] = []
        monkeypatch.setattr(
            playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
        )
        before = _tree(consumer)

        refused = _add(consumer, release, dry_run=False)

        assert refused.status == "blocked", refused
        assert ("overlaps" if prior == "overlapping kit" else "older than") in (
            refused.detail or ""
        )
        assert installs == []
        assert _tree(consumer) == before
    finally:
        for _ in opened:
            pass


def test_a_kit_pins_the_compiler_seeded_workspace_file_built_ins_as_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F-003: a Source Procedure over Provider:cruxible-builtin, and a Blueprint
    whose slot takes workspace.file, travel without carrying, installing or remapping
    the built-ins; the consumer must hold the same frozen ones."""

    from cruxible_client.contracts.procedures.artifacts import BlueprintArtifact
    from cruxible_client.contracts.procedures.blueprints import blueprint_path, render_blueprint
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProcedureDefinition
    from cruxible_core.service.kits import _builtin, service_add_kit, service_build_kit
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.core_support._knowledge_loop_support import accept_proposal
    from tests.core_support._support import initialize_local
    from tests.test_procedures import test_procedure_source_runs as sources

    monkeypatch.setattr(sources, "PROCEDURE_NAME", "acme.files")
    (tmp_path / "publisher").mkdir()
    instance, owner, procedure, _root, _policy = sources._world(tmp_path / "publisher")
    raw = procedure.definition.model_dump(mode="json", by_alias=True)
    raw["name"] = "acme.files-skeleton"
    raw["nodes"][0]["provider"] = {
        "tag": "playbill-procedure-pin-slot-ref-v1",
        "slot_name": "files",
    }
    raw["nodes"][0]["implementation_digest"] = None
    raw["pin_slots"] = [
        {
            "slot_name": "files",
            "pin_role": "provider",
            "artifact_kind": "Provider",
            "interface_digest": raw["nodes"][0]["interface_digest"],
        }
    ]
    definition = ProcedureDefinition.model_validate(raw)
    blueprint = BlueprintArtifact(
        identity={"kind": "Blueprint", "name": "acme.files-skeleton"},  # type: ignore[arg-type]
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(pin for pin in procedure.pins if pin.role != "provider"),
        owned_contracts=procedure.owned_contracts,
        activation_policy="drain",
    )
    inspection = submit_member_candidate(
        instance,
        members={blueprint_path(blueprint.identity.name): render_blueprint(blueprint)},
        actor_id="owner",
        proposal_name="files-skeleton",
        proposal_family="procedure",
        timestamp=sources.ACCEPT_STAMP,
    )
    accept_proposal(instance, owner, inspection)

    release = service_build_kit(
        instance, KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",))
    ).bundle

    paths = [item.path for item in release.manifest.artifacts]
    assert "procedures/acme.files.json" in paths
    assert "blueprints/acme.files-skeleton.json" in paths
    assert not any(path.startswith(("providers/", "provider-interfaces/")) for path in paths)
    assert release.manifest.providers == ()
    released = parse_procedure(
        release.contents()["procedures/acme.files.json"], path="procedures/acme.files.json"
    )
    builtins = [
        pin for pin in released.pins if pin.target.kind in {"Provider", "ProviderInterface"}
    ]
    assert len(builtins) == 2
    assert all(_builtin(pin.target.kind, pin.target.name, pin.artifact_digest) for pin in builtins)

    (tmp_path / "consumer").mkdir()
    consumer, _consumer_owner = initialize_local(tmp_path / "consumer")
    added = service_add_kit(
        consumer,
        KitAddRequest(bundle=release, source="test", dry_run=False),
        actor_id="owner",
        timestamp=sources.ACCEPT_STAMP,
    )
    assert added.status in {"accepted", "proposed"}, added
    assert added.providers == ()


def test_a_takeover_of_another_kits_definition_refuses_before_any_provider_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Review F-006 (delta): another kit owns a different version of a definition the
    release carries; the commit refuses as its preview does, installing nothing."""

    from cruxible_client.contracts.acquisition_policies import (
        SourceAcquisitionPolicy,
        render_acquisition_policy,
    )
    from cruxible_client.contracts.kits import KitArtifact, KitArtifactBytes
    from cruxible_core.cli.provider_wheels import stage_kit_provider_directory
    from cruxible_core.service.kits import _artifact_state
    from tests.test_server import test_kit_providers as helpers

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        monkeypatch.setattr(helpers, "PROCEDURE", "feature.increment")
        monkeypatch.setattr(helpers, "BLUEPRINT", "feature.skeleton")
        _author(publisher)
        release = playbill_api.playbill_kit_build(
            publisher.instance_id,
            KitBuildRequest(
                kit_id="feature",
                version="1.0.0",
                owns=("feature.",),
                providers=(
                    stage_kit_provider_directory(
                        _client(publisher.http), publisher.instance_id, project
                    ),
                ),
            ),
        ).bundle
        policy_path = f"source-acquisition-policies/{POLICY}.json"
        raw = json.loads(release.contents()[policy_path])
        raw["inputs"][0]["max_age"] = {"microseconds": 1}
        changed = render_acquisition_policy(SourceAcquisitionPolicy.model_validate(raw))
        prior = KitBundle(
            manifest=release.manifest.model_copy(
                update={
                    "kit_id": "prior",
                    "owns": ("acme.",),
                    "providers": (),
                    "artifacts": (
                        KitArtifact(
                            path=policy_path,
                            artifact_digest=_artifact_state(policy_path, changed).artifact_digest,
                        ),
                    ),
                }
            ),
            artifacts=(KitArtifactBytes.of(policy_path, changed),),
        )
        accepted = playbill_api.playbill_kit_add(
            consumer.instance_id, KitAddRequest(bundle=prior, dry_run=False)
        )
        assert accepted.status == "accepted", accepted
        installs: list[object] = []
        monkeypatch.setattr(
            playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
        )
        instance = get_playbill_manager().get(consumer.instance_id)
        coordinate = instance.accepted_coordinate().git_oid
        preview = _add(consumer, release, dry_run=True)
        assert preview.status == "would_block", preview
        assert "owned by kit prior" in (preview.detail or "")

        refused = _add(consumer, release, dry_run=False)

        assert refused.status == "blocked", refused
        assert "owned by kit prior" in (refused.detail or "")
        assert installs == []
        assert instance.accepted_coordinate().git_oid == coordinate
    finally:
        for _ in opened:
            pass
