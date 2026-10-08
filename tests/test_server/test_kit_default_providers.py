"""A kit names a default provider for a contract: installed by name, or satisfied.

The kit carries a Blueprint with a ``web.fetch`` slot and names a default
implementation by package and exact version. A fresh consumer installs it by
name from its provider index; one where another implementation of the contract
is already installed keeps that one, and the Blueprint's slot takes it. Either
way an occurrence admits only the buckets the bound implementation claims.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    BlueprintInput,
    BlueprintInstanceInput,
    CarriedContractInput,
)
from cruxible_client.contracts.kits import (
    KitBuildRequest,
    KitBundle,
    KitManifest,
    KitProvider,
    KitProviderFile,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import parse_provider
from cruxible_client.kits import read_kit_directory, write_kit_directory
from cruxible_core.cli.provider_wheels import stage_kit_provider_directory
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.web_fetch import web_fetch_interface_registration
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from tests.support.provider_checkout import ProviderCheckout
from tests.test_server.test_kit_providers import _add, _client, _install, _tree

CONTRACT = "acme.web"
POLICY = "acme.intake"
BLUEPRINT = "acme.fetch"
INTERFACE_PATH = "provider-interfaces/web.fetch.json"
API_JSON = ("source_kind=api_json;access=*;page_weight=*", "web-fetch-api-json")
STATIC_LIGHT = ("source_kind=static_html;access=*;page_weight=light", "web-fetch-static-light")
STATIC_MEDIUM = ("source_kind=static_html;access=*;page_weight=medium", "web-fetch-static-medium")


def _project(root: Path, checkout: ProviderCheckout, *, name: str, claims: Any) -> Path:
    """A web.fetch package directory: pyproject, uv.lock, and in dist/ its wheel
    beside the runtime wheel its lock names by path."""

    from tests.support.provider_installation import build_web_fetch_alternative

    wheel, _lock = build_web_fetch_alternative(root, checkout.repository, name=name, claims=claims)
    runtime = next(checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    shutil.copy(runtime, wheel.parent / runtime.name)
    return wheel.parent.parent


def _worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout: ProviderCheckout
) -> tuple[Iterator[tuple[Any, Any]], Path, Path]:
    """A publisher and a fresh consumer on one daemon whose provider index is a file
    index serving the default package and its runtime (registry wheels: PyPI)."""

    from tests.support.provider_installation import write_file_index
    from tests.test_server.test_playbill_kits import _fresh_open_worlds

    default = _project(
        tmp_path / "default",
        checkout,
        name="fetch-default",
        claims=(API_JSON, STATIC_LIGHT, STATIC_MEDIUM),
    )
    alternative = _project(
        tmp_path / "alternative", checkout, name="fetch-alt", claims=(STATIC_LIGHT,)
    )
    index = write_file_index(tmp_path / "index", tuple(sorted((default / "dist").glob("*.whl"))))
    config = tmp_path / "server-state" / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"provider_index_urls": list(index)}))
    return _fresh_open_worlds(tmp_path, monkeypatch, independent=False), default, alternative


def _accept(world: Any, draft: Any) -> None:
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()


def _web_fetch_entry(world: Any) -> dict[str, Any]:
    from cruxible_client.contracts.get_reads import GetRequest

    proof = (
        _client(world.http)
        .get(
            world.instance_id, request=GetRequest(ref="ProviderInterface:web.fetch", detail="proof")
        )
        .proof
    )
    assert proof is not None
    return dict(proof["entry"])


def _author(world: Any) -> None:
    """A capture contract, an acquisition policy and a Blueprint whose one Source
    slot takes any implementation of web.fetch; its request is an API endpoint."""

    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.contracts.captures import capture_component_pin
    from tests.core_support._pc_c_support import capture_contract
    from tests.test_procedures.test_procedure_source_runs import _policy

    base = capture_contract(name=CONTRACT)
    contract = base.model_copy(
        update={
            "logical_source_identities": ("web.response",),
            "coordinate_schema_pins": (
                capture_component_pin("coordinate-schema", "http-response-v1"),
            ),
            "selector_schema_pins": (
                capture_component_pin("selector-schema", "whole-response-v1"),
            ),
            "selection_budget": base.selection_budget.model_copy(update={"max_bytes": 1048576}),
        }
    )
    _accept(
        world,
        world.pb.changes(rationale="Retain fetched observations.")
        .capture_contract(contract)
        .acquisition_policy(_policy(name=POLICY, input_name="observation")),
    )
    interface = _web_fetch_entry(world)
    example = procedure_example()
    carried = {"kind": "carried_contract", "role": "contract-out", "name": "fetch-result"}
    definition = {
        **example.definition,
        "name": BLUEPRINT,
        "graph_format": 6,
        "returns": "result",
        "contract_out": carried,
        "nodes": [
            {
                "kind": "source",
                "node_id": "fetch",
                "as": "observation",
                "next": "shape",
                "capture_contract": {
                    "kind": "accepted",
                    "role": "capture-contract",
                    "target": contract.identity.qualified,
                },
                "provider": {"kind": "slot", "slot_name": "fetch"},
                "interface": {
                    "kind": "accepted",
                    "role": "provider-interface",
                    "target": interface["identity"],
                },
                "interface_digest": interface["interface_digest"],
                "request": {
                    "url": "https://fixture.invalid/api/v1/measurements.json",
                    "expected_format": "json",
                },
            },
            {
                "kind": "project",
                "node_id": "shape",
                "as": "result",
                "fields": {"text": "$steps.observation.derived.text"},
                "contract_out": carried,
            },
        ],
        "pin_slots": [
            {
                "slot_name": "fetch",
                "pin_role": "provider",
                "artifact_kind": "Provider",
                "interface_digest": interface["interface_digest"],
            }
        ],
        "budget": {
            **example.definition["budget"],  # type: ignore[dict-item]
            "wall_clock": {"microseconds": 30_000_000},
            "max_items": None,
            "max_provider_calls": 1,
            "max_capture_bytes": 1048576,
        },
        "hard_caps": {
            **example.definition["hard_caps"],  # type: ignore[dict-item]
            "max_wall_clock": {"microseconds": 60_000_000},
            "max_provider_calls": 2,
            "max_capture_bytes": 2097152,
        },
    }
    contracts = (
        next(row for row in example.contracts if row.name == "empty-input"),
        CarriedContractInput(name="fetch-result", fields={"text": PropertySchema(type="string")}),
    )
    _accept(
        world,
        world.pb.changes(rationale="Fetch with whichever web.fetch is installed.").procedure(
            definition=BlueprintInput(kind="blueprint", definition=definition, contracts=contracts)
        ),
    )


def _build_with_default(world: Any, default: Path) -> KitBundle:
    client = _client(world.http)
    return playbill_api.playbill_kit_build(
        world.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            providers=(
                stage_kit_provider_directory(client, world.instance_id, default, delivery="index"),
            ),
        ),
    ).bundle


def _instantiate_and_refuse(world: Any) -> None:
    """The slot takes the alternative; it claims no API bucket, so the run refuses
    the endpoint before any provider code starts."""

    _accept(
        world,
        world.pb.changes(rationale="Fetch with the alternative.").procedure(
            definition=BlueprintInstanceInput(
                kind="blueprint_instance",
                name="acme.fetch-alt",
                blueprint=BLUEPRINT,
                bindings={"fetch": "fetch-alt"},
                acquisition_policy=POLICY,
            )
        ),
    )
    run = world.pb.accepted_procedure("acme.fetch-alt").run()
    state = _client(world.http).get_procedure_run(world.instance_id, run.run_id)
    assert run.status != "succeeded"
    assert "unclaimed_bucket" in state.model_dump_json(), state.model_dump_json(indent=2)


def test_a_fresh_consumer_installs_the_default_provider_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened, default, alternative = _worlds(tmp_path, monkeypatch, provider_checkout)
    publisher, consumer = next(opened)
    try:
        _install(publisher, default)
        _author(publisher)
        # A default installs by name from the lock its wheel embeds, so the build
        # refuses any other lock.
        relocked = tmp_path / "relocked"
        shutil.copytree(default, relocked)
        with (relocked / "uv.lock").open("a") as stream:
            stream.write("\n# another lock identity\n")
        with pytest.raises(RequestRefusedError) as refused:
            _build_with_default(publisher, relocked)
        assert refused.value.error_code == "cruxible.kit.provider_lock_not_embedded"
        release = _build_with_default(publisher, default)

        (provider,) = release.manifest.providers
        assert (provider.provider_id, provider.delivery, provider.interfaces) == (
            "fetch-default",
            "index",
            ("web.fetch",),
        )
        assert provider.dependencies == () and release.manifest.provider_files() == {}
        assert release.provider_files == ()
        # The carried interface is core's registration of the definition.
        assert release.contents()[INTERFACE_PATH] == render_provider_interface(
            web_fetch_interface_registration()
        )
        write_kit_directory(release, tmp_path / "kit")
        assert read_kit_directory(tmp_path / "kit") == release
        assert not (tmp_path / "kit" / "providers").exists()

        preview = _add(consumer, release)
        assert preview.status == "would_propose", preview
        assert [step.action for step in preview.providers] == ["would_install"]

        added = _add(consumer, release, dry_run=False)
        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["install"]
        tree = _tree(consumer)
        assert tree[INTERFACE_PATH] == release.contents()[INTERFACE_PATH]
        held = parse_provider(
            tree["providers/fetch-default.json"], path="providers/fetch-default.json"
        )
        assert held.runtime_artifact.distribution.sha256 == provider.wheel.sha256  # type: ignore[attr-defined]
        (status,) = playbill_api.playbill_kit_status(consumer.instance_id).kits
        assert [(item.provider_id, item.delivery, item.state) for item in status.providers] == [
            ("fetch-default", "index", "installed")
        ]
        again = _add(consumer, release, dry_run=False)
        assert again.status == "unchanged", again
        assert [step.action for step in again.providers] == ["unchanged"]

        # A second implementation of the contract installs onto the same
        # registration and fills the Blueprint's slot.
        _install(consumer, alternative)
        assert _tree(consumer)[INTERFACE_PATH] == tree[INTERFACE_PATH]
        assert sorted(
            item["provider_identity"] for item in _web_fetch_entry(consumer)["providers"]
        ) == ["Provider:fetch-alt", "Provider:fetch-default"]
        _instantiate_and_refuse(consumer)
    finally:
        for _ in opened:
            pass


def test_another_installed_implementation_satisfies_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened, default, alternative = _worlds(tmp_path, monkeypatch, provider_checkout)
    publisher, consumer = next(opened)
    try:
        _install(publisher, default)
        _author(publisher)
        release = _build_with_default(publisher, default)

        _install(consumer, alternative)
        preview = _add(consumer, release)
        assert preview.status == "would_propose", preview
        (step,) = preview.providers
        assert step.action == "satisfied" and "fetch-alt" in (step.detail or ""), step

        added = _add(consumer, release, dry_run=False)
        assert added.status == "accepted", added
        assert [step.action for step in added.providers] == ["satisfied"]
        assert "providers/fetch-default.json" not in _tree(consumer)
        (status,) = playbill_api.playbill_kit_status(consumer.instance_id).kits
        assert [(item.provider_id, item.state) for item in status.providers] == [
            ("fetch-default", "satisfied")
        ]
        _instantiate_and_refuse(consumer)
    finally:
        for _ in opened:
            pass


def test_an_index_default_is_named_by_its_build_and_carries_no_file() -> None:
    """The delivery field is additive: a bundled entry's manifest bytes (and so every
    existing release digest) are unchanged, and an index default names no file."""

    def file(name: str, digit: str) -> KitProviderFile:
        return KitProviderFile(filename=name, sha256="sha256:" + digit * 64)

    bundled = KitProvider(
        provider_id="fetch-default",
        package="fetch-default",
        version="0.2.0",
        wheel=file("fetch_default-0.2.0-py3-none-any.whl", "1"),
        lock=file("fetch-default-0.2.0.uv.lock", "2"),
        dependencies=(file("cruxible_provider_runtime-0.2.0-py3-none-any.whl", "3"),),
        interfaces=("web.fetch",),
    )
    assert "delivery" not in bundled.model_dump(mode="json")
    default = bundled.model_copy(update={"delivery": "index", "dependencies": ()})
    default = KitProvider.model_validate(default.model_dump(mode="json"))
    assert default.model_dump(mode="json")["delivery"] == "index"
    assert default.files() == ()
    manifest = KitManifest(
        kit_id="acme",
        version="1.0.0",
        owns=("acme.",),
        artifacts=(),
        providers=(default,),
    )
    assert manifest.provider_files() == {}
    assert KitBundle(manifest=manifest, artifacts=()).provider_contents() == {}
    with pytest.raises(ValueError, match="no dependency files"):
        KitProvider.model_validate(
            {
                **default.model_dump(mode="json"),
                "dependencies": [file("x-1-py3-none-any.whl", "4").model_dump()],
            }
        )
