"""Scenario 7: a derive whose layout lacks the kind's record set is refused at binding (note §2.3).

``pilot-results-thin`` declares ``web.search.results`` and names
``capture<web.search-results>``, so its shapes fit by name; but the layout it
carries has no ``results`` record set. Bound and run, it writes an empty
``results``: a complete read with no records, on which ``when_absent: mirror``
retires every title (checked on today's engine with today's full extract; a
document with no ``results`` key at all is read as incomplete and retires
nothing). Layout coverage refuses it before it can run.

Transcript shape::

    ... scenario 5's world: pilot-results bound, one run, 3 titles live
    $ cruxible provider install pilot-results-thin
    layout coverage of web.search-results by pilot-results-thin: missing record set results
    $ cruxible authoring submit line.json   (results -> pilot-results-thin)
      refused cruxible.line.binding_unfit
      Slot `results` needs web.search.results@1. `pilot-results-thin#web.search.results`
      declares it but does not fit: ... record set results ...
    $ cruxible get Line:pilot.search-titles      results still -> pilot-results
    $ cruxible line run pilot.search-titles      succeeded; nothing proposed
      3 titles live; nothing retired

Green when: the coverage check names the missing ``results`` record set; the
Line acceptance is refused with ``cruxible.line.binding_unfit`` naming the slot,
the capability, the operation and the record set; the accepted Line still binds
``pilot-results``; a further run proposes nothing and every title stays live.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario


@pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"owner: {surfaces.COVERAGE}")
@pytest.mark.pilot(
    "s7-coverage",
    owner=surfaces.COVERAGE,
    title="a swapped-in derive whose layout lacks the kind's record set is refused at binding; "
    "nothing retires",
)
def test_a_derive_missing_a_record_set_is_refused_and_nothing_retires(
    pilot: Scenario, tmp_path: Path
) -> None:
    from cruxible_client.contracts.procedures.line_specs import line_spec_path
    from cruxible_core.errors import RequestRefusedError

    instance, base = search_world.setup(pilot, tmp_path, search_world.titles_spec())
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
    titles = search_world.titles(instance)
    pilot.call(f"cruxible line run {search_world.LINE}   (bound to pilot-results)")
    pilot.got(f"{first.status}; {len(titles)} titles live")

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
    pilot.got(f"{len(search_world.titles(instance))} titles live")

    assert gap is not None and "results" in gap
    assert code == "cruxible.line.binding_unfit"
    for part in ("`results`", f"{providers.RESULTS}@1", thin.spelling, "record set", "results"):
        assert part in message, (part, message)
    assert "pilot-results/" in json.dumps(line.get("bindings", {}))
    assert thin.provider_id not in json.dumps(line.get("bindings", {}))
    assert again.status == "succeeded", again.terminal
    assert proposal is None
    assert search_world.titles(instance) == titles
