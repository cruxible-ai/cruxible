"""Scenario 3: a provider installs with no kit present and says what it serves (note §2.1, §2.4).

Transcript shape::

    $ cruxible kit status --offline
      base 1.0.0 (bundled)          # no kit declares pilot.echo or pilot.upper
    $ cruxible provider install pilot-echo
      installed pilot-echo/<build>
    $ cruxible provider list pilot-echo
      echo   serves pilot.echo    "Gives back the text it is sent."
             source tests/pilot_contract_batch/fixtures/providers.py (https://github.com/...)
      upper  serves pilot.upper   "Gives back the text it is sent, in capitals."
             source tests/pilot_contract_batch/fixtures/providers.py
    $ cruxible get Contract:pilot.echo.request
      not found                     # the carried copies are never installed as Contracts

Green when: the install succeeds on an instance where no kit and no accepted
Contract mentions the provider's shapes; the listing shows each operation's
``serves``, ``description`` and ``source`` exactly as registered; and no
``pilot.echo.*`` Contract appears among the instance's accepted Contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pilot_contract_batch import surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario
from tests.pilot_contract_batch.search_world import fresh_instance


@pytest.mark.pilot(
    "s3-provider-install",
    owner=surfaces.REGISTRATION,
    title="install a provider with no kit present; it lists what it serves, with descriptions "
    "and sources",
)
def test_a_provider_installs_without_a_kit_and_lists_what_it_serves(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_core.service.kits import service_kit_status

    instance = fresh_instance(tmp_path)
    package = providers.echo_package()

    pilot.call("cruxible kit status --offline")
    kits = service_kit_status(instance).kits
    pilot.got(", ".join(f"{kit.kit_id} {kit.version}" for kit in kits) or "no kits")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert not any(path.startswith("contracts/pilot.") for path in tree)

    pilot.call(f"cruxible provider install {package.provider_id}")
    installed = surfaces.install_provider_package(instance, package)
    pilot.got(f"installed {installed.build}")

    pilot.call(f"cruxible provider list {package.provider_id}")
    for operation in installed.operations:
        pilot.got(
            f"{operation.name:6} serves {', '.join(operation.serves):12} {operation.description!r}"
        )
        where = operation.source.get("repository")
        pilot.got(f"{'':6} source {operation.source['path']}" + (f" ({where})" if where else ""))

    after = instance.tree_at(instance.accepted_coordinate().git_oid)
    carried = sorted(path for path in after if path.startswith("contracts/pilot."))
    pilot.call("cruxible get Contract:pilot.echo.request")
    pilot.got("not found" if not carried else f"found: {carried}")

    registered = {item["name"]: item for item in package.registration["operations"]}
    assert installed.provider_id == package.provider_id
    assert {op.name for op in installed.operations} == set(registered)
    for operation in installed.operations:
        expected = registered[operation.name]
        assert operation.serves == tuple(expected["serves"])
        assert operation.description == expected["description"]
        assert dict(operation.source) == expected["source"]
    assert carried == []
