"""Scenario 2: a Procedure's slot names a capability, and only that (note §2.1, §2.4).

Transcript shape::

    $ cruxible list Capability
      ... the base kit's seven
    $ cruxible authoring submit procedure.json   (slot search: {capability: web.search@1})
      accepted pilot.search-titles; slot search pins web.search@1 sha256:...
    $ cruxible get Procedure:pilot.search-titles
      slots.search = {"capability": {"name": "web.search", "version": 1, "digest": "sha256:..."}}
    $ cruxible authoring submit procedure.json   (slot search: {capability: pilot.no-such@1})
      refused cruxible....: ... pilot.no-such ...
      nothing accepted

Green when: the slot is stored as exactly one capability pin whose digest is
the listed capability's; no provider was consulted (none is installed); a slot
naming a capability the instance does not hold is refused at authoring with a
message naming it, and the refused Procedure is not in the accepted tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario

UNKNOWN = "pilot.no-such.capability"


def _search_only(capability: str, name: str) -> dict[str, object]:
    return search_world.refs(
        {
            "procedure": name,
            "description": "Ask the search capability about the project.",
            "input": {},
            "output": {},
            "slots": {"search": {"capability": capability}},
            "hard_caps": search_world.HARD_CAPS,
            "steps": [search_world.SEARCH_STEP],
        }
    )


@pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"owner: {surfaces.SLOTS}")
@pytest.mark.pilot(
    "s2-capability-slots",
    owner=surfaces.SLOTS,
    title="a slot names a capability: accepted as one pin; an unknown capability is refused "
    "at authoring",
)
def test_a_slot_is_one_capability_pin_and_an_unknown_capability_is_refused(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_client.contracts.procedures.artifacts import procedure_path
    from cruxible_core.errors import RequestRefusedError
    from cruxible_core.service.procedures.provider_installation import installed_providers

    instance = search_world.fresh_instance(tmp_path)
    listed = {
        entry.pin.name: entry.pin for entry in search_world.show_capabilities(pilot, instance)
    }
    search_world.author_vocabulary(pilot, instance)

    name = "pilot.search-once"
    pilot.call(f"cruxible authoring submit procedure.json   ({name}, slot search: web.search@1)")
    pins = surfaces.author_capability_procedure(
        instance, _search_only(f"{providers.SEARCH}@1", name)
    )
    for slot, pin in sorted(pins.items()):
        pilot.got(f"accepted; slot {slot} pins {pin.spelling} {pin.digest}")

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    stored = json.loads(tree[procedure_path(name)])["definition"]["slots"]
    pilot.call(f"cruxible get Procedure:{name}")
    pilot.show("slots", stored)

    unknown = "pilot.search-unknown"
    pilot.call(f"cruxible authoring submit procedure.json   ({unknown}, slot search: {UNKNOWN}@1)")
    with pytest.raises(RequestRefusedError) as refused:
        surfaces.author_capability_procedure(instance, _search_only(f"{UNKNOWN}@1", unknown))
    code, message = search_world.show_refusal(pilot, refused.value)
    after = instance.tree_at(instance.accepted_coordinate().git_oid)
    pilot.got("nothing accepted" if procedure_path(unknown) not in after else "ACCEPTED")

    assert [provider.provider for provider in installed_providers(instance)] == ["cruxible-builtin"]
    assert pins == {"search": listed[providers.SEARCH]}
    assert stored == {
        "search": {
            "capability": {
                "name": providers.SEARCH,
                "version": 1,
                "digest": listed[providers.SEARCH].digest,
            }
        }
    }
    assert code.startswith("cruxible.")
    assert UNKNOWN in message
    assert procedure_path(unknown) not in after
