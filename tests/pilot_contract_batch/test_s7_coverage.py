"""Scenario 7: a derive whose layout lacks the kind's record set is refused at binding (note §2.3).

``pilot-results-thin`` declares ``web.search.results`` and names
``capture<web.search-results>``, so its shapes fit by name; but its copy of the
kind declares no ``results`` record set (it carries the kind's decoding and no
layout, since a layout declares at least one record set). Bound and run, it
writes an empty ``results``: a complete read with no records, on which
``when_absent: mirror`` retires every rank (checked on today's engine with
today's full extract; a document with no ``results`` key at all is read as
incomplete and retires nothing). Layout coverage refuses it before it can run.

The same document from a producer whose layout **does** declare ``results``
(``pilot-results-empty``: exact copies of the Contracts and of the kind's
layout) is the case the wave 0 report called dangerous: the record set is
present but empty where the kind's layout declares it. What green is for it:
the run reads zero records as a complete read only when the producer's layout
declares that record set, and the binding check is what refuses a producer that
does not declare it. So ``pilot-results-empty`` binds (``fit: identical``), its
run's extract reads 0 records with ``complete: true``, and ``mirror`` proposes
retiring every rank; the proposal, activated, leaves no rank live. Core does
not look inside the bytes to tell "no results" from "did not produce results"
(decision 8); the producer's declared layout is its word that the set is there.

Transcript shape::

    ... scenario 5's world: pilot-results bound, one run, 3 ranks live
    $ cruxible provider install pilot-results-thin
    layout coverage of web.search-results by pilot-results-thin: missing record set results
    $ cruxible authoring submit line.json   (results -> pilot-results-thin)
      refused cruxible.line.binding_unfit
      Slot `results` needs web.search.results@1. `pilot-results-thin#web.search.results`
      declares it but does not fit: ... record set results ...
    $ cruxible get Line:pilot.search-ranks      results still -> pilot-results
    $ cruxible line run pilot.search-ranks      succeeded; nothing proposed
      3 ranks live; nothing retired
    $ cruxible provider install pilot-results-empty
    layout coverage of web.search-results by pilot-results-empty: covered
    $ cruxible authoring submit line.json   (results -> pilot-results-empty)
      accepted; results: web.search.results@1, identical
    $ cruxible line run pilot.search-ranks
      succeeded; extract hits read 0 records, complete; proposal <id>
    $ cruxible proposal activate <id>
      0 ranks live; 3 retired

Green when: the coverage check names the missing ``results`` record set; the
Line acceptance is refused with ``cruxible.line.binding_unfit`` naming the slot,
the capability, the operation and the record set; the accepted Line still binds
``pilot-results``; a further run proposes nothing and every rank stays live.
Then, for ``pilot-results-empty``: coverage finds no gap; the Line rebinding
``results`` to it is accepted with ``fit: identical``; its run succeeds, its
extract reads 0 records as a complete read, and it proposes; once that
proposal is activated no rank is live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario


def _extract_read(state: Any) -> tuple[int, bool]:
    """(records read, complete) of the run's ``hits`` extract."""

    (node,) = (node for node in state.nodes if node.node == "hits")
    return node.summary.records, node.summary.complete


@pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"owner: {surfaces.COVERAGE}")
@pytest.mark.pilot(
    "s7-coverage",
    owner=surfaces.COVERAGE,
    title="a swapped-in derive whose layout lacks the kind's record set is refused at binding; "
    "nothing retires; one whose layout declares it and writes it empty binds and retires all",
)
def test_a_derive_missing_a_record_set_is_refused_and_nothing_retires(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_client.contracts.procedures.line_specs import line_spec_path
    from cruxible_core.errors import RequestRefusedError

    instance, base = search_world.setup(pilot, tmp_path, search_world.ranks_spec())
    search = providers.search_package(base)
    exact = providers.results_package(base)
    thin = providers.thin_layout_package(base)
    search_world.install(pilot, instance, exact)
    surfaces.accept_line(
        instance, search_world.line_input({"search": "pilot-search", "results": "pilot-results"})
    )
    search_world.grant(instance)
    first = search_world.run(instance, {"search": search, providers.RESULTS: exact})
    assert first.status == "succeeded", first.terminal
    search_world.activate(instance, first)
    ranks = search_world.ranks(instance)
    pilot.call(f"cruxible line run {search_world.LINE}   (bound to pilot-results)")
    pilot.got(f"{first.status}; {len(ranks)} ranks live")

    search_world.install(pilot, instance, thin)
    gap = surfaces.layout_coverage_gap(instance, kind=providers.RESULTS_KIND, package=thin)
    pilot.say(f"layout coverage of {providers.RESULTS_KIND} by {thin.provider_id}: {gap}")

    pilot.call("cruxible authoring submit line.json   (results -> pilot-results-thin)")
    with pytest.raises(RequestRefusedError) as refused:
        surfaces.accept_line(
            instance,
            search_world.line_input({"search": "pilot-search", "results": thin.provider_id}),
        )
    code, message = search_world.show_refusal(pilot, refused.value)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = json.loads(tree[line_spec_path(search_world.LINE)])
    pilot.call(f"cruxible get Line:{search_world.LINE}")
    pilot.show("bindings", line.get("bindings"))

    pilot.call(f"cruxible line run {search_world.LINE}")
    again = search_world.run(instance, {"search": search, providers.RESULTS: exact}, minute=1)
    proposal = again.terminal_egress[0].proposal_id
    pilot.got(f"{again.status}; proposal: {proposal or 'none'}")
    pilot.got(f"{len(search_world.ranks(instance))} ranks live")

    assert gap is not None and "results" in gap
    assert code == "cruxible.line.binding_unfit"
    for part in ("`results`", f"{providers.RESULTS}@1", thin.spelling, "record set", "results"):
        assert part in message, (part, message)
    assert "pilot-results/" in json.dumps(line.get("bindings", {}))
    assert thin.provider_id not in json.dumps(line.get("bindings", {}))
    assert again.status == "succeeded", again.terminal
    assert proposal is None
    assert search_world.ranks(instance) == ranks

    # The dangerous document from a producer that declares the record set: honoured.
    empty = providers.empty_results_package(base)
    search_world.install(pilot, instance, empty)
    covered = surfaces.layout_coverage_gap(instance, kind=providers.RESULTS_KIND, package=empty)
    pilot.say(
        f"layout coverage of {providers.RESULTS_KIND} by {empty.provider_id}: "
        f"{covered or 'covered'}"
    )

    pilot.call(f"cruxible authoring submit line.json   (results -> {empty.provider_id})")
    rebound = surfaces.accept_line(
        instance, search_world.line_input({"search": "pilot-search", "results": empty.provider_id})
    )
    slot = rebound.slots["results"]
    pilot.got(f"accepted; results: {slot.capability.spelling}, {slot.fit}")
    search_world.grant(instance)

    pilot.call(f"cruxible line run {search_world.LINE}")
    emptied = search_world.run(instance, {"search": search, providers.RESULTS: empty}, minute=2)
    records, complete = _extract_read(emptied)
    retiring = emptied.terminal_egress[0].proposal_id
    pilot.got(
        f"{emptied.status}; extract hits read {records} records, "
        f"{'complete' if complete else 'incomplete'}; proposal {retiring or 'none'}"
    )
    pilot.call(f"cruxible proposal activate {retiring}")
    search_world.activate(instance, emptied)
    left = search_world.ranks(instance)
    pilot.got(f"{len(left)} ranks live; {len(ranks) - len(left)} retired")

    assert covered is None
    assert slot.capability.spelling == f"{providers.RESULTS}@1"
    assert slot.fit == "identical"
    assert emptied.status == "succeeded", emptied.terminal
    assert (records, complete) == (0, True)
    assert retiring is not None
    assert left == {}
