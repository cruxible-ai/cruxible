"""Scenario 5: swap one fitting provider for another on an accepted Line (note §2.2, §2.5).

Transcript shape::

    $ cruxible authoring submit line.json   (search -> pilot-search, results -> pilot-results)
      accepted; results: web.search.results@1, identical
    $ cruxible line run pilot.search-ranks
      succeeded; 3 pilot.page.rank Claims proposed, each citing 1 Capture
      receipts: results web.search.results@1 fit identical
    $ cruxible proposal activate ...
    $ cruxible authoring submit line.json   (results -> pilot-results-wide)
      accepted; results: web.search.results@1, structural
    $ cruxible line run pilot.search-ranks
      succeeded; nothing to propose: same derived Capture, same Claims
      receipts: results web.search.results@1 fit structural

Green when: both runs succeed; after the swap the live rank Claims are the
same (url -> rank), the derived Capture the extract read is byte-identical
(same digest), and nothing is proposed; each run's receipts record the same
capability pin, with ``fit: identical`` before the swap and ``structural``
after (the wide provider's own request accepts one more optional field), and
the operation's own signature digest differs between the two.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario


def _derived(state: Any) -> tuple[str, ...]:
    (call,) = (node for node in state.nodes if node.node == "ranked")
    return tuple(call.summary.capture_digests)


# Stops at ``_derived(second) == _derived(first)``: every step before it holds (both
# Lines accepted, identical then structural; both runs succeed; same ranks; nothing
# proposed), and the receipts' capability pins and fits hold after it. The derived
# Captures differ because a Capture's envelope digest covers its provider invocation
# evidence (build, operation signature, occurrence plan).
@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="owner: binding (needs capture-as-field merged)"
)
@pytest.mark.pilot(
    "s5-swap",
    owner=surfaces.SWAP,
    title="swap a fitting provider for another on an accepted Line: same Claims, fit recorded",
)
def test_swapping_a_fitting_provider_keeps_claims_and_records_the_fit(
    pilot: Scenario, tmp_path: Path
) -> None:
    instance, base = search_world.setup(pilot, tmp_path, search_world.ranks_spec())
    search = providers.search_package(base)
    exact = providers.results_package(base)
    wide = providers.wide_results_package(base)
    for package in (exact, wide):
        search_world.install(pilot, instance, package)

    pilot.call(
        "cruxible authoring submit line.json   (search -> pilot-search, results -> pilot-results)"
    )
    first_line = surfaces.accept_line(
        instance, search_world.line_input({"search": "pilot-search", "results": "pilot-results"})
    )
    pilot.got(
        f"accepted; results: {first_line.slots['results'].capability.spelling}, "
        f"{first_line.slots['results'].fit}"
    )
    search_world.grant(instance)

    pilot.call(f"cruxible line run {search_world.LINE}")
    first = search_world.run(instance, {"search": search, providers.RESULTS: exact})
    shape = search_world.evidence_shape(instance, first)
    before = surfaces.run_slot_records(instance, first)
    pilot.got(f"{first.status}; {len(shape)} {search_world.RANK} Claims proposed")
    pilot.got(
        f"receipts: results {before['results'].capability.spelling} fit {before['results'].fit}"
    )
    search_world.activate(instance, first)
    ranks = search_world.ranks(instance)
    pilot.show("live ranks", ranks)

    pilot.call("cruxible authoring submit line.json   (results -> pilot-results-wide)")
    second_line = surfaces.accept_line(
        instance,
        search_world.line_input({"search": "pilot-search", "results": "pilot-results-wide"}),
    )
    pilot.got(
        f"accepted; results: {second_line.slots['results'].capability.spelling}, "
        f"{second_line.slots['results'].fit}"
    )

    pilot.call(f"cruxible line run {search_world.LINE}")
    second = search_world.run(instance, {"search": search, providers.RESULTS: wide}, minute=1)
    after = surfaces.run_slot_records(instance, second)
    proposal = second.terminal_egress[0].proposal_id
    pilot.got(f"{second.status}; proposal: {proposal or 'none (nothing changed)'}")
    pilot.got(
        f"receipts: results {after['results'].capability.spelling} fit {after['results'].fit}"
    )

    assert first.status == "succeeded", first.terminal
    assert shape == sorted((url, 1) for url in search_world.URLS)
    assert ranks == {
        hit["link"]: rank
        for rank, hit in enumerate(providers.RECORDED_HITS[search_world.QUERY], start=1)
    }
    assert first_line.slots["results"].fit == "identical"
    assert second_line.slots["results"].fit == "structural"

    assert second.status == "succeeded", second.terminal
    assert proposal is None
    assert search_world.ranks(instance) == ranks
    assert _derived(second) == _derived(first)

    assert before["results"].capability == after["results"].capability
    assert before["results"].capability.spelling == f"{providers.RESULTS}@1"
    assert (before["results"].fit, after["results"].fit) == ("identical", "structural")
    assert (
        before["results"].operation_signature_digest != after["results"].operation_signature_digest
    )
