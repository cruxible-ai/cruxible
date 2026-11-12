"""Scenario 1: a new instance knows the base kit and nothing else (note §7.4).

Transcript shape::

    $ cruxible init --principal-id owner
      instance inst_... created in 0.NN s; Internet connections: none
    $ cruxible list Capability
      web.fetch@1     external_read  kit base
      ... seven rows, the base kit's capabilities (§7.4.1)
    $ cruxible kit status --offline
      base 1.0.0 (bundled), no providers
    $ cruxible provider list
      cruxible-builtin (core's file reader) and nothing else

Green when: the seven capabilities are listed, each owned by kit ``base``;
``kit status`` shows ``base`` at 1.0.0 with source ``bundled`` and no providers;
the only live provider is core's builtin; creation opened no Internet socket.
Cost (when green): instance creation with the base kit, median of three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pilot_contract_batch import surfaces
from tests.pilot_contract_batch.harness import RUNS, Scenario, Stopwatch, no_network
from tests.pilot_contract_batch.search_world import BASE_CAPABILITIES


def _create(root: Path) -> tuple[object, float]:
    from tests.core_support._support import initialize_fresh

    root.mkdir(parents=True, exist_ok=True)
    with Stopwatch() as watch:
        instance, _owner = initialize_fresh(root)
    return instance, watch.seconds


@pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"owner: {surfaces.BASE_KIT}")
@pytest.mark.pilot(
    "s1-base-kit",
    owner=surfaces.BASE_KIT,
    title="create an instance; it knows the base kit's seven capabilities and no providers",
)
def test_a_new_instance_holds_the_base_kit_and_no_providers(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_core.service.kits import service_kit_status
    from cruxible_core.service.procedures.provider_installation import installed_providers

    with no_network() as network:
        instance, seconds = _create(tmp_path / "instance-0")
    pilot.call("cruxible init --principal-id owner")
    pilot.got(
        f"instance {instance.descriptor.instance_id} created in {seconds:.2f} s; "  # type: ignore[attr-defined]
        f"Internet connections: {len(network.attempts) or 'none'}"
    )

    pilot.call("cruxible list Capability")
    listed = surfaces.list_capabilities(instance)
    for entry in listed:
        pilot.got(f"{entry.pin.spelling:24} {entry.effect:14} kit {entry.kit_id}")

    pilot.call("cruxible kit status --offline")
    status = service_kit_status(instance)  # type: ignore[arg-type]
    for kit in status.kits:
        pilot.got(
            f"{kit.kit_id} {kit.version} ({kit.source}), "
            f"providers: {', '.join(p.package for p in kit.providers) or 'none'}"
        )

    pilot.call("cruxible provider list")
    live = installed_providers(instance)  # type: ignore[arg-type]
    for provider in live:
        pilot.got(
            f"{provider.provider}  {provider.build}  operations: {', '.join(provider.operations)}"
        )

    assert sorted(entry.pin.name for entry in listed) == sorted(BASE_CAPABILITIES)
    assert {entry.pin.version for entry in listed} == {1}
    assert {entry.kit_id for entry in listed} == {"base"}
    assert [(kit.kit_id, kit.version, kit.source) for kit in status.kits] == [
        ("base", "1.0.0", "bundled")
    ]
    assert status.kits[0].providers == ()
    assert [provider.provider for provider in live] == ["cruxible-builtin"]
    assert network.attempts == []

    pilot.say()
    samples = [seconds]
    for index in range(1, RUNS):
        with no_network() as again:
            _instance, took = _create(tmp_path / f"instance-{index}")
        assert again.attempts == []
        samples.append(took)
    pilot.median("instance_create_with_base_kit", samples)
