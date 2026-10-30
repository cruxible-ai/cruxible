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
    if not isinstance(http, TestClient):
        return http
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
    one wheel and lock writes byte-identical Provider operation declarations on
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
        http, instance_id, reviewer = next(opened)
        from tests.support.provider_installation import noop_contract_files
        from tests.test_server.test_provider_installation import _accept

        _accept(http, instance_id, reviewer, noop_contract_files(), name="noop-kit-contracts")
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
    provider_path = next(p for p in seen[0] if p.startswith("providers/cruxible-provider-noop/"))
    provider = parse_provider(seen[0][provider_path], path=provider_path)
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


def _worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, historical=False
) -> Iterator[tuple[Any, Any]]:
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
    if historical:
        from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
        from tests.support.core_provider_world import CoreProviderWorld

        operator = ProviderRuntimeOperator(tmp_path / "server-state")
        worlds = (
            CoreProviderWorld(tmp_path / "publisher", "inst_core_publisher", operator),
            CoreProviderWorld(tmp_path / "consumer", "inst_core_consumer", operator),
        )
        for world in worlds:
            _accept_local_call_contract(world)
        yield worlds
    else:
        for worlds in _fresh_open_worlds(tmp_path, monkeypatch, independent=False):
            for world in worlds:
                _accept_local_call_contract(world)
            yield worlds


def _package(
    root: Path,
    checkout: ProviderCheckout,
    *,
    name: str,
    increment: int = 1,
) -> Path:
    """A provider package directory as a kit's source tree holds it: pyproject, uv.lock,
    and in dist/ its wheel beside the runtime wheel its lock names by path."""

    from tests.support.provider_installation import build_local_call

    wheel, _lock = build_local_call(root, checkout.repository, name=name, increment=increment)
    project = wheel.parent.parent
    runtime = next(checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    shutil.copy(runtime, project / "dist" / runtime.name)
    return project


def _accept_local_call_contract(world: Any) -> None:
    """Accept the Contract a ``build_local_call`` package pins, as this world accepts."""

    from tests.support.provider_installation import (
        accept_local_call_contract,
        local_call_contract_files,
    )

    members = local_call_contract_files()
    if hasattr(world, "accept_members"):
        if not set(members) <= set(world.tree()):
            world.accept_members(members, timestamp="2026-10-24T11:00:00.000000Z")
    else:
        accept_local_call_contract(world.instance_id, world.approve)


def _install(world: Any, project: Path, *, control_domain: str = "operator") -> None:
    from packaging.utils import canonicalize_name, parse_wheel_filename

    _accept_local_call_contract(world)

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


def _graph(world: Any, *, provider: str | None) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """One open operation slot; provider selection belongs to a consumer Line."""
    from cruxible_client.contracts.procedures.authored import ProcedureSpec, lower_procedure_spec
    from tests.support.provider_installation import LOCAL_CALL_IN, LOCAL_CALL_OUT

    authored = lower_procedure_spec(
        ProcedureSpec.model_validate(
            {
                "procedure": PROCEDURE if provider is not None else BLUEPRINT,
                "input": {"n": "integer"},
                "output": {"n": "integer"},
                "slots": {
                    "increment": {"in": LOCAL_CALL_IN, "out": LOCAL_CALL_OUT, "effect": "pure"}
                },
                "steps": [{"call": "result", "slot": "increment", "input": {"n": "$input.n"}}],
                "returns": "result",
                "budget": {"wall_clock": "PT30S", "provider_calls": 1},
            }
        )
    )
    return authored.definition, authored.contracts


def _author(world: Any, *, procedure: bool = True) -> None:
    """Current owned shapes and open Procedures, never kit-carried authority."""
    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
    from tests.core_support._candidate_support import submit_member_candidate

    # Ownership/downgrade tests concern governed definition ownership, not the
    # removed acquisition-policy artifact. Keep a real typed shape in that role.
    shape = Contract.model_validate({"name": POLICY, "version": 1, "fields": {"n": "int"}})
    members = {contract_path(shape.name, 1): render_contract(shape)}
    if hasattr(world, "accept_members"):
        world.accept_members(members, timestamp="2026-10-24T11:00:00.000000Z")
    else:
        instance = get_playbill_manager().get(world.instance_id)
        proposed = submit_member_candidate(
            instance,
            members=members,
            actor_id="operator",
            proposal_name="kit-owned-shape",
            proposal_family="procedure",
            timestamp="2026-10-24T11:00:00.000000Z",
        ).proposal
        assert proposed.candidate is not None, proposed.evaluation.diagnostics
        world.approve(proposed.admission.proposal_id)
    draft = world.pb.changes(rationale="Ship open increment Procedures.")
    for selected in ("kit-call", None) if procedure else (None,):
        definition, contracts = _graph(world, provider=selected)
        draft.procedure(
            definition=ProcedureInput(kind="procedure", definition=definition, contracts=contracts)
        )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()
    if procedure:
        stored = parse_procedure(
            _tree(world)[f"procedures/{PROCEDURE}.json"], path=f"procedures/{PROCEDURE}.json"
        )
        (call,) = stored.definition.nodes
        assert call.slot == "increment"
        assert set(stored.definition.slots) == {"increment"}
        assert len(stored.owned_contracts) == 2
        for owned in stored.owned_contracts:
            assert set(owned.contract_schema.fields) == {"n"}
            assert owned.contract_schema.fields["n"].type in {"int", "integer"}


def _build(world: Any, *projects: Path, version: str = "1.0.0") -> KitBundle:
    from cruxible_core.cli.provider_wheels import stage_kit_provider_directory

    client = _client(world.http)
    request = KitBuildRequest(
        kit_id="acme",
        version=version,
        owns=("acme.",),
        providers=tuple(
            stage_kit_provider_directory(client, world.instance_id, project) for project in projects
        ),
    )
    if hasattr(world, "instance"):
        from cruxible_core.service.kits import service_build_kit

        return service_build_kit(world.instance, request).bundle
    return playbill_api.playbill_kit_build(world.instance_id, request).bundle


def _add(world: Any, bundle: KitBundle, **options: Any) -> KitChangeResult:
    staged = stage_kit_providers(_client(world.http), world.instance_id, bundle)
    request = KitAddRequest(bundle=staged, source="test", **options)
    if hasattr(world, "instance"):
        from cruxible_core.service.kits import service_add_kit
        from cruxible_core.service.procedures.provider_installation import service_install_provider

        return service_add_kit(
            world.instance,
            request,
            actor_id="owner",
            timestamp="2026-10-24T12:00:00.000000Z",
            install_provider=lambda request, **kwargs: service_install_provider(
                world.instance,
                operator=world.operator,
                request=request,
                actor_id="owner",
                timestamp="2026-10-24T12:00:00.000000Z",
                **kwargs,
            ),
        )
    return playbill_api.playbill_kit_add(world.instance_id, request)


def _tree(world: Any) -> dict[str, bytes]:
    if hasattr(world, "instance"):
        return world.tree()
    instance = get_playbill_manager().get(world.instance_id)
    return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))


def _run(world: Any, name: str, n: int) -> Any:
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec
    from tests.test_server.test_provider_installation import _run_line

    tree = _tree(world)
    path = next(p for p in tree if p.startswith("providers/kit-call/"))
    build = parse_provider(tree[path], path=path)
    line_name = name + "-run"
    intent = (
        world.pb.changes(rationale="Bind the installed kit operation.")
        .line(
            name=line_name,
            procedure=name,
            bindings={"increment": build.identity.name},
            parameters={"n": n},
        )
        .prepare()
    )
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()
    line_path = "lines/" + line_name + ".json"
    line = parse_line_spec(_tree(world)[line_path], path=line_path)
    assert line.bindings["increment"].build.artifact_digest == provider_digest(build).tagged
    return _run_line(world.instance_id, line_name)


def test_fresh_consumer_provisions_the_operation_contract_before_bundled_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Isolate install prerequisites from the separately blocked slot-Contract export.

    A kit may bundle an operation alongside a pure definition. The installer
    still validates its real input/output Contract before kit definitions land.
    The original slotted export scenarios below keep their stronger assertions.
    """
    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from tests.support.provider_installation import local_call_contract_files
    from tests.test_server.test_provider_installation import _run_call

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        pure = ProcedureInput(
            kind="procedure",
            spec=ProcedureSpec.model_validate(
                {
                    "procedure": "acme.installation-marker",
                    "input": {},
                    "output": {"n": "integer"},
                    "steps": [{"project": "result", "fields": {"n": 2}}],
                    "returns": "result",
                    "budget": {"provider_calls": 0},
                }
            ),
        )
        intent = (
            publisher.pb.changes(rationale="A pure kit definition.")
            .procedure(definition=pure)
            .prepare()
        )
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit(accept="never")
        publisher.approve(submitted._candidate_status.proposal_id)
        release = _build(publisher, project)
        assert set(release.contents()) == {"procedures/acme.installation-marker.json"}
        assert len(release.manifest.providers) == 1
        before = _tree(consumer)
        assert all(before[path] == content for path, content in local_call_contract_files().items())
        preview = _add(consumer, release)
        assert preview.status == "would_propose", preview
        assert [step.action for step in preview.providers] == ["would_install"]
        assert _tree(consumer) == before
        assert not any(path.startswith("providers/kit-call/") for path in before)
        added = _add(consumer, release, dry_run=False)
        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        path = next(path for path in tree if path.startswith("providers/kit-call/"))
        provider = parse_provider(tree[path], path=path)
        assert _run_call(
            consumer.http,
            consumer.instance_id,
            consumer.reviewer_key,
            build=provider.identity.name,
            value={"n": 2},
            name="bundled-prerequisite",
        ) == {"n": 3}
    finally:
        for _ in opened:
            pass


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
            f"contracts/{POLICY}/v1.json",
            "contracts/local.increment.value/v1.json",
            f"procedures/{PROCEDURE}.json",
            f"procedures/{BLUEPRINT}.json",
        ]
        assert (
            release.contents()["contracts/local.increment.value/v1.json"]
            == (_tree(publisher)["contracts/local.increment.value/v1.json"])
        )
        (provider,) = manifest.providers
        assert (provider.provider_id, provider.package, provider.operations) == (
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
        procedure = parse_procedure(
            tree[f"procedures/{PROCEDURE}.json"], path=f"procedures/{PROCEDURE}.json"
        )
        released = parse_procedure(
            release.contents()[f"procedures/{PROCEDURE}.json"], path=f"procedures/{PROCEDURE}.json"
        )
        from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest

        assert procedure_artifact_digest(procedure) == procedure_artifact_digest(released)
        assert procedure.definition.slots == released.definition.slots
        held_path = next(p for p in tree if p.startswith("providers/kit-call/"))
        here = parse_provider(tree[held_path], path=held_path)
        publisher_provider = parse_provider(_tree(publisher)[held_path], path=held_path)
        assert here.identity == publisher_provider.identity
        assert provider_digest(here) != provider_digest(publisher_provider)
        (status,) = playbill_api.playbill_kit_status(consumer.instance_id).kits
        assert [(item.provider_id, item.state) for item in status.providers] == [
            ("kit-call", "installed")
        ]
        again = _add(consumer, release, dry_run=False)
        assert again.status == "unchanged", again.model_dump_json(indent=2)
        assert [step.action for step in again.providers] == ["unchanged"]

        # The kit's Procedure runs on the provider it bundled.
        assert _run(consumer, PROCEDURE, 2) == {"n": 3}

        # A second provider of the same interface fills the Blueprint's slot.
        other = _package(tmp_path / "local", provider_checkout, name="other-call")
        _install(consumer, other)
        draft = consumer.pb.changes(rationale="Increment with the other provider.")
        draft.line(
            name="acme.increment-other",
            procedure=BLUEPRINT,
            bindings={"increment": "other-call"},
            parameters={"n": 5},
        )
        intent = draft.prepare()
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit(accept="never")
        assert submitted._candidate_status is not None
        consumer.approve(submitted._candidate_status.proposal_id)
        consumer.pb.refresh()
        other_run = consumer.pb.line("acme.increment-other").run()
        assert other_run.status == "succeeded", other_run
        assert other_run.result.model_dump() == {"n": 6}
    finally:
        for _ in opened:
            pass


def _run_exact_build(world: Any, procedure: str, build: Provider, *, name: str, n: int) -> Any:
    """Accept an exact Line over the released open Procedure and invoke the real child."""
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec
    from tests.test_server.test_provider_installation import _run_line

    intent = (
        world.pb.changes(rationale="Select this exact coexisting build.")
        .line(
            name=name,
            procedure=procedure,
            bindings={"increment": build.identity.name},
            parameters={"n": n},
        )
        .prepare()
    )
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()
    path = f"lines/{name}.json"
    line = parse_line_spec(_tree(world)[path], path=path)
    pin = line.bindings["increment"].build
    assert pin.target == build.identity
    assert pin.artifact_digest == provider_digest(build).tagged
    return _run_line(world.instance_id, name)


def test_open_procedure_exports_with_independent_wheel_and_lock_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """F001: open signatures impose no publisher-build or bundle exclusivity.

    Wheel and lock commitments are checked at staging, installation and exact Lines.
    """
    from cruxible_client.contracts.providers import provider_path
    from cruxible_client.errors import ConfigError

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        bundled = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, bundled)
        _author(publisher)
        unbundled = _build(publisher)
        assert unbundled.manifest.providers == ()
        procedure_path = f"procedures/{PROCEDURE}.json"
        procedure = parse_procedure(unbundled.contents()[procedure_path], path=procedure_path)
        assert set(procedure.definition.slots) == {"increment"}
        assert not any(pin.target.kind == "Provider" for pin in procedure.pins)
        assert not any(path.startswith("providers/") for path in unbundled.contents())
        alternative = _package(tmp_path / "alternative", provider_checkout, name="other-call")
        independent = _build(publisher, alternative)
        assert independent.contents() == unbundled.contents()
        assert independent.manifest.providers[0].provider_id == "other-call"

        rebuilt = tmp_path / "rebuilt"
        shutil.copytree(bundled, rebuilt)
        (rebuilt / "src/local_call/NOTES.txt").write_text("a later build\n")
        for wheel in (rebuilt / "dist").glob("kit_call-*.whl"):
            wheel.unlink()
        uv = shutil.which("uv")
        assert uv is not None
        subprocess.run(
            [uv, "build", "--wheel", "--offline", "--out-dir", str(rebuilt / "dist"), str(rebuilt)],
            check=True,
            capture_output=True,
        )
        relocked = tmp_path / "relocked"
        shutil.copytree(bundled, relocked)
        with (relocked / "uv.lock").open("a") as stream:
            stream.write("\n# another lock identity\n")
        releases = tuple(
            _build(publisher, project, version=f"{i}.0.0")
            for i, project in enumerate((bundled, rebuilt, relocked), start=1)
        )
        packages = [release.manifest.providers[0] for release in releases]
        original, different_wheel, different_lock = packages
        assert original.wheel != different_wheel.wheel
        assert original.lock == different_wheel.lock
        assert original.wheel == different_lock.wheel
        assert original.lock != different_lock.lock
        assert len({package.build for package in packages}) == 3
        assert releases[2].contents() == unbundled.contents()
        for release in releases[:2]:
            assert release.contents() == unbundled.contents()
            added = _add(consumer, release, dry_run=False)
            assert added.status == "accepted", added
            assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        assert len([p for p in tree if p.startswith("providers/kit-call/")]) == 2
        for i, package in enumerate(packages[:2]):
            path = provider_path(package.build)
            held = parse_provider(tree[path], path=path)
            assert _run_exact_build(consumer, PROCEDURE, held, name=f"acme.build-{i}", n=2) == {
                "n": 3
            }

        # Forged file commitments fail in ordinary SDK staging before installation.
        with monkeypatch.context() as guard:
            installs: list[object] = []
            guard.setattr(
                playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
            )
            before = _tree(consumer)
            coordinate = get_playbill_manager().get(consumer.instance_id).accepted_coordinate()
            for field in ("wheel", "lock"):
                held_file = getattr(original, field)
                forged = original.model_copy(
                    update={field: held_file.model_copy(update={"sha256": _ZERO})}
                )
                bad = releases[0].model_copy(
                    update={
                        "manifest": releases[0].manifest.model_copy(update={"providers": (forged,)})
                    }
                )
                with pytest.raises(ConfigError, match="under another digest"):
                    _add(consumer, bad, dry_run=False)
                assert installs == []
                assert _tree(consumer) == before
                assert (
                    get_playbill_manager().get(consumer.instance_id).accepted_coordinate()
                    == coordinate
                )
        # A different lock names a distinct actual build even when its resolved
        # set and primary wheel are unchanged. Preserve the real coexistence
        # expectation: a runtime cache/lock verification failure is not kit policy.
        added = _add(consumer, releases[2], dry_run=False)
        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        assert len([p for p in tree if p.startswith("providers/kit-call/")]) == 3
        path = provider_path(different_lock.build)
        held = parse_provider(tree[path], path=path)
        assert _run_exact_build(consumer, PROCEDURE, held, name="acme.build-lock", n=2) == {"n": 3}
    finally:
        for _ in opened:
            pass


def test_open_signature_allows_coexisting_registered_builds_and_exact_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """F001: a slot declares a signature; consumer Lines choose each actual build."""
    from cruxible_client.contracts.providers import provider_path

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        installed = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, installed)
        _author(publisher, procedure=False)
        unbundled = _build(publisher)
        assert unbundled.manifest.providers == ()
        path = f"procedures/{BLUEPRINT}.json"
        assert path in unbundled.contents()
        procedure = parse_procedure(unbundled.contents()[path], path=path)
        assert not any(pin.target.kind == "Provider" for pin in procedure.pins)
        other = _package(tmp_path / "other", provider_checkout, name="kit-call", increment=2)
        compatible = _build(publisher, other)
        original = _build(publisher, installed)
        assert compatible.contents() == original.contents() == unbundled.contents()
        original_package = original.manifest.providers[0]
        other_package = compatible.manifest.providers[0]
        assert original_package.operations == other_package.operations == (INTERFACE,)
        assert original_package.build != other_package.build
        _install(consumer, other)
        added = _add(consumer, original, dry_run=False)
        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        assert len([p for p in tree if p.startswith("providers/kit-call/")]) == 2
        for package, delta in ((original_package, 1), (other_package, 2)):
            build_path = provider_path(package.build)
            held = parse_provider(tree[build_path], path=build_path)
            assert tuple(op.name for op in held.operations) == (INTERFACE,)
            assert _run_exact_build(consumer, BLUEPRINT, held, name=f"acme.delta-{delta}", n=2) == {
                "n": 2 + delta
            }
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
        # An open Procedure commits no publisher build. Both packages export;
        # the primary wheel+lock agree while their materializations differ.
        alternative = _build(publisher, other)
        release = _build(publisher, project)
        assert alternative.contents() == release.contents()
        a, b = release.manifest.providers[0], alternative.manifest.providers[0]
        assert a.wheel == b.wheel and a.lock == b.lock and a.build == b.build
        assert a.dependencies != b.dependencies

        # Add commits a particular materialization of that exact build.
        _install(consumer, other)
        installs: list[object] = []
        monkeypatch.setattr(
            playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
        )
        before = _tree(consumer)
        coordinate = get_playbill_manager().get(consumer.instance_id).accepted_coordinate()
        refused = _add(consumer, release, dry_run=False)
        assert refused.status == "blocked", refused
        (step,) = refused.providers
        assert step.action == "blocked" and "dependency closure" in (step.detail or "")
        assert "cruxible.provider.build_rematerialized" in (step.detail or "")
        assert installs == []
        assert _tree(consumer) == before
        assert get_playbill_manager().get(consumer.instance_id).accepted_coordinate() == coordinate
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


def test_kit_add_on_a_daemon_without_a_runtime_directory_names_the_cause_and_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """Full loop 2, new finding 1: the lane precondition, not a bare ConfigError.

    The consumer daemon starts with TMPDIR and XDG_RUNTIME_DIR unset and a state
    root too long for its control sockets; its kit add refuses as an operator
    environment refusal naming the cause and the repair, and never a path.
    """

    from cruxible_client.contracts.repairs import HandEditRepair
    from cruxible_core.errors import ProviderLaneRefusedError
    from cruxible_core.providers.provider_process_leases import (
        PROVIDER_RUNTIME_DIRECTORY_REPAIR,
    )

    opened = _worlds(tmp_path / "publish", monkeypatch)
    publisher, _ = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        _author(publisher)
        release = _build(publisher, project)
    finally:
        for _ in opened:
            pass

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    opened = _worlds(tmp_path / ("long-state-root-" + "x" * 110), monkeypatch)
    _, consumer = next(opened)
    try:
        before = _tree(consumer)
        with pytest.raises(ProviderLaneRefusedError) as refused:
            _add(consumer, release, dry_run=False)
        assert _tree(consumer) == before
    finally:
        for _ in opened:
            pass

    message = str(refused.value)
    assert refused.value.error_code == "provider_runtime_directory_unavailable"
    assert "is not set, so no private per-user runtime directory is configured" in message
    assert message.endswith(PROVIDER_RUNTIME_DIRECTORY_REPAIR)
    assert isinstance(refused.value.repair, HandEditRepair)
    assert refused.value.repair.hand_edit.required_change == PROVIDER_RUNTIME_DIRECTORY_REPAIR
    # The cause names its byte budget and a templated example directory
    # (`/run/user/$UID` on Linux), never a resolved path.
    assert "must name a private directory of at most 57 bytes" in message
    assert "/" not in message.replace("/run/user/$UID", "")
    assert "long-state-root" not in message


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
        policy_path = f"contracts/{POLICY}/v1.json"
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

    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_core.service.kits import service_add_kit, service_build_kit
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.core_support._knowledge_loop_support import accept_proposal
    from tests.core_support._support import initialize_local
    from tests.test_procedures import test_procedure_source_runs as sources

    monkeypatch.setattr(sources, "PROCEDURE_NAME", "acme.files")
    (tmp_path / "publisher").mkdir()
    instance, owner, procedure, _root, _policy = sources._world(tmp_path / "publisher")
    # B2: a Procedure's Source/Call nodes already name an open slot (`slot:
    # str`), and the Procedure declares that slot's signature in `slots`
    # (`ProviderInterface`/pin_slots/the separate "Blueprint" shape this test
    # used to build by hand are gone; the open-slot/bound-build distinction is
    # a Line concern now, not a Procedure one). "acme.files-skeleton" is
    # therefore just this same open-slot definition under another name.
    raw = procedure.definition.model_dump(mode="json", by_alias=True)
    raw["name"] = "acme.files-skeleton"
    from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
    from cruxible_client.contracts.procedures.models import ProcedureDefinition

    definition = ProcedureDefinition.model_validate(raw)
    blueprint = ProcedureArtifact(
        identity={"kind": "Procedure", "name": "acme.files-skeleton"},  # type: ignore[arg-type]
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=procedure.owned_contracts,
        activation_policy="drain",
    )
    inspection = submit_member_candidate(
        instance,
        members={procedure_path(blueprint.identity.name): render_procedure(blueprint)},
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
    assert "procedures/acme.files-skeleton.json" in paths
    assert not any(path.startswith(("providers/", "provider-interfaces/")) for path in paths)
    assert release.manifest.providers == ()
    # A Procedure no longer pins a Provider at all (B2): its Source/Call nodes
    # only name an open slot; the exact build is recorded on a Line's own
    # bindings at acceptance, never on the Procedure. There is therefore
    # nothing resembling a Provider/ProviderInterface pin left on the released
    # Procedure artifact to check -- the asserts above (no provider path in
    # the release, no provider in the manifest) are the whole of this guarantee.

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
    """F002: feature.reader really carries a mutable Query owned by the prior kit."""
    from cruxible_client.contracts.kits import KitArtifact, KitArtifactBytes
    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_client.contracts.query.definitions import (
        parse_query_definition,
        render_query_definition,
    )
    from cruxible_core.cli.provider_wheels import stage_kit_provider_directory
    from cruxible_core.service.kits import _artifact_state
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.test_procedures.test_named_reference_closure import (
        authored_procedure,
        authored_query,
    )

    opened = _worlds(tmp_path, monkeypatch)
    publisher, consumer = next(opened)
    try:
        project = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        _install(publisher, project)
        query_name = "acme.shared"
        query_path = f"query-definitions/{query_name}.json"
        reader_path = "procedures/feature.reader.json"
        query = authored_query(query_name)
        reader = authored_procedure("feature.reader", query=query_name)
        publisher_instance = get_playbill_manager().get(publisher.instance_id)
        proposed = submit_member_candidate(
            publisher_instance,
            members={
                query_path: render_query_definition(query),
                reader_path: render_procedure(reader),
            },
            actor_id="operator",
            proposal_name="foreign-query-dependency",
            proposal_family="procedure",
            timestamp="2026-10-24T11:00:00.000000Z",
        ).proposal
        assert proposed.candidate is not None, proposed.evaluation.diagnostics
        publisher.approve(proposed.admission.proposal_id)
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
        assert {reader_path, query_path} <= release.contents().keys()
        released_reader = parse_procedure(release.contents()[reader_path], path=reader_path)
        (tap,) = released_reader.definition.nodes
        assert tap.kind == "state_tap"
        assert tap.query.target.kind == "QueryDefinition"
        assert tap.query.target.name == query_name
        assert not query_name.startswith(release.manifest.owns)
        query = parse_query_definition(release.contents()[query_path], path=query_path)
        changed = render_query_definition(
            query.model_copy(
                update={
                    "default_budgets": query.default_budgets.model_copy(update={"max_results": 9}),
                }
            )
        )
        assert changed != release.contents()[query_path]
        prior = KitBundle(
            manifest=release.manifest.model_copy(
                update={
                    "kit_id": "prior",
                    "owns": ("acme.",),
                    "providers": (),
                    "artifacts": (
                        KitArtifact(
                            path=query_path,
                            artifact_digest=_artifact_state(query_path, changed).artifact_digest,
                        ),
                    ),
                }
            ),
            artifacts=(KitArtifactBytes.of(query_path, changed),),
        )
        accepted = playbill_api.playbill_kit_add(
            consumer.instance_id, KitAddRequest(bundle=prior, dry_run=False)
        )
        assert accepted.status == "accepted", accepted
        assert _tree(consumer)[query_path] == changed
        installs: list[object] = []
        monkeypatch.setattr(
            playbill_api, "service_install_provider", lambda *a, **k: installs.append(k)
        )
        instance = get_playbill_manager().get(consumer.instance_id)
        coordinate = instance.accepted_coordinate()
        before = _tree(consumer)
        for dry_run in (True, False):
            refused = _add(consumer, release, dry_run=dry_run)
            assert refused.status == ("would_block" if dry_run else "blocked"), refused
            assert "owned by kit prior" in (refused.detail or "")
            assert installs == []
            assert _tree(consumer) == before
            assert instance.accepted_coordinate() == coordinate
            assert not any(path.startswith("providers/kit-call/") for path in _tree(consumer))
    finally:
        for _ in opened:
            pass
