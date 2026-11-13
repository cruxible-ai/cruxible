"""Scenario 8: a kit that requires the base kit (note §7.4.4).

A publisher instance authors one Contract under its own prefix that nests the
base kit's ``web.search-result@1`` row, and builds kit ``pilot-dependent``: the
build leaves the base definition out and records ``requires: base``.

Transcript shape::

    $ cruxible list Capability                         ... the base kit's eight
    $ cruxible authoring submit pilot.hit-row@1 (hit: web.search-result@1)
    $ cruxible kit build pilot-dependent 0.1.0 --owns pilot.
      requires: base >= 1.0.0; web.search-result not in the bundle
    -- consumer A: base present
    $ cruxible kit add ./pilot-dependent --commit        accepted
    $ cruxible kit remove base --commit
      refused: ... pilot-dependent requires it
    -- consumer B: base removed first
    $ cruxible kit remove base --commit                  accepted (nothing requires it)
    $ cruxible kit add ./pilot-dependent --commit
      refused cruxible.kit.requires_missing: ... base ... cruxible kit add base

Green when: the bundle requires exactly ``(base, 1.0.0)`` and carries no base
definition; the add is accepted where base is installed and refused with
``cruxible.kit.requires_missing`` (naming ``base`` and the repair
``cruxible kit add base``) where it is not; removing base while the dependent is
installed is refused with a message naming ``pilot-dependent``, and base stays
installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.harness import Scenario

KIT = "pilot-dependent"
ROW = "pilot.hit-row"


def _timestamp() -> str:
    from cruxible_client.contracts.candidates import canonical_candidate_timestamp
    from cruxible_client.contracts.temporal import utc_now

    return canonical_candidate_timestamp(utc_now())


def _add(instance: Any, bundle: Any) -> Any:
    from cruxible_client.contracts.kits import KitAddRequest
    from cruxible_core.service.kits import service_add_kit

    return service_add_kit(
        instance,
        KitAddRequest(bundle=bundle, source="test", dry_run=False),
        actor_id="owner",
        timestamp=_timestamp(),
    )


def _remove(instance: Any, kit_id: str) -> Any:
    from cruxible_client.contracts.kits import KitRemoveRequest
    from cruxible_core.service.kits import service_remove_kit

    return service_remove_kit(
        instance,
        KitRemoveRequest(kit_id=kit_id, dry_run=False),
        actor_id="owner",
        timestamp=_timestamp(),
    )


def _kits(instance: Any) -> list[str]:
    from cruxible_core.service.kits import service_kit_status

    return [kit.kit_id for kit in service_kit_status(instance).kits]


def _stand_in_base() -> Any:
    """Kit ``base`` 1.0.0 from the wave 0 example Contracts under ``web.``.

    Stands in for the bundled base kit until it ships (slice 9): the same kit
    id, version and prefixes, and the Contracts this scenario nests. The
    example capabilities and kind are left out (this core cannot hold them yet).
    """

    from cruxible_client.contracts.kits import KitBuildRequest
    from cruxible_core.service.kits import build_kit
    from tests.pilot_contract_batch.fixtures.providers import EXAMPLE_BASE_KIT

    root = EXAMPLE_BASE_KIT
    tree = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / "contracts").glob("web.*/v*.json"))
    }
    return build_kit(
        tree, KitBuildRequest(kit_id="base", version="1.0.0", owns=("web.", "workspace."))
    )


def _instance_with_base(tmp_path: Path) -> Any:
    """A fresh instance holding kit ``base``: the bundled one once slice 9 installs it
    at creation, the stand-in until then."""

    instance = search_world.fresh_instance(tmp_path)
    if "base" not in _kits(instance):
        assert _add(instance, _stand_in_base()).status == "accepted"
    return instance


def _show_base(pilot: Scenario, instance: Any) -> None:
    """``cruxible list Capability`` once slice 9 lists them; until then the stand-in."""

    try:
        search_world.show_capabilities(pilot, instance)
    except surfaces.SurfaceMissing as missing:
        pilot.say(f"(stand-in for the base kit until {missing.owner}: kit base 1.0.0)")
        pilot.call("cruxible kit status --offline")
        pilot.got(", ".join(_kits(instance)))


@pytest.mark.pilot(
    "s8-kit-requires",
    owner=surfaces.KIT_REQUIRES,
    title="a kit that requires base: added with and without it; base cannot be removed under it",
)
def test_a_kit_requiring_base_installs_only_beside_it_and_holds_it(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput
    from cruxible_client.contracts.kits import KitBuildRequest
    from cruxible_core.errors import RequestRefusedError
    from cruxible_core.service.kits import service_build_kit
    from tests.support.operation_world import author

    publisher = _instance_with_base(tmp_path / "publisher")
    _show_base(pilot, publisher)

    pilot.call(f"cruxible authoring submit {ROW}@1   (hit: web.search-result@1)")
    author(
        publisher,
        ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "members": [
                    {
                        "kind": "contract",
                        "name": ROW,
                        "version": 1,
                        "description": "One search hit this kit keeps.",
                        "fields": {"hit": "web.search-result@1"},
                    }
                ],
            }
        ),
        name="dependent-row",
    )

    pilot.call(f"cruxible kit build {KIT} 0.1.0 --owns pilot.")
    bundle = service_build_kit(
        publisher,
        KitBuildRequest(kit_id=KIT, version="0.1.0", owns=("pilot.",)),
        template_actor_id="owner",
    ).bundle
    requires = surfaces.kit_requires(bundle)
    carried = sorted(path for path in bundle.contents() if path.startswith("contracts/web."))
    pilot.got(f"requires: {', '.join(f'{k} >= {v}' for k, v in requires) or 'nothing'}")
    pilot.got(f"base definitions in the bundle: {carried or 'none'}")

    pilot.say("-- consumer A: base present")
    consumer_a = _instance_with_base(tmp_path / "consumer-a")
    pilot.call(f"cruxible kit add ./{KIT} --commit")
    added = _add(consumer_a, bundle)
    pilot.got(f"{added.status}")
    pilot.call("cruxible kit remove base --commit")
    with pytest.raises(RequestRefusedError) as held:
        _remove(consumer_a, "base")
    _held_code, held_message = search_world.show_refusal(pilot, held.value)
    pilot.call("cruxible kit status --offline")
    pilot.got(", ".join(_kits(consumer_a)))

    pilot.say("-- consumer B: base removed first")
    consumer_b = _instance_with_base(tmp_path / "consumer-b")
    pilot.call("cruxible kit remove base --commit")
    removed = _remove(consumer_b, "base")
    pilot.got(f"{removed.status}")
    pilot.call(f"cruxible kit add ./{KIT} --commit")
    with pytest.raises(RequestRefusedError) as missing:
        _add(consumer_b, bundle)
    missing_code, missing_message = search_world.show_refusal(pilot, missing.value)

    assert requires == (("base", "1.0.0"),)
    assert carried == []
    assert added.status == "accepted", added
    assert KIT in held_message
    assert set(_kits(consumer_a)) == {"base", KIT}
    assert removed.status == "accepted", removed
    assert missing_code == "cruxible.kit.requires_missing"
    assert "base" in missing_message
    assert "cruxible kit add base" in missing_message or "cruxible kit add base" in str(
        missing.value.repair
    )
    assert KIT not in _kits(consumer_b)
