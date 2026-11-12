"""Scenario 4: binding by capability and fit, and the three ways not to fit (note §2.2, §2.6).

Transcript shape::

    $ cruxible list Capability                       ... the base kit's eight
    $ cruxible provider install pilot-search / pilot-results / pilot-fetch-only /
      pilot-results-narrow / pilot-results-untyped
    $ cruxible authoring submit line.json   (search -> pilot-search; results left to fit)
      accepted; results -> pilot-results#web.search.results (web.search.results@1, identical)
    $ ... results -> pilot-fetch-only
      refused cruxible.line.binding_undeclared
      Slot `results` needs web.search.results@1. `pilot-fetch-only#web.fetch` serves
      web.fetch, not web.search.results.
    $ ... results -> pilot-results-narrow
      refused cruxible.line.binding_unfit
      Slot `results` needs web.search.results@1. `pilot-results-narrow#web.search.results`
      declares it but does not fit: request.response: ...
    $ ... results -> pilot-results-untyped
      refused cruxible.line.binding_unfit
      ... result.results: the operation returns capture<*>; the capability expects
      capture<web.search-results>.

Green when: the unnamed slot binds the one operation that serves the capability
and fits, recorded with the capability pin and ``fit: identical``; each of the
three refusals carries the code above and names the slot, the capability, the
operation and the first failing path (with both sides' types for the result).
Cost (when green): Line acceptance (the binding check) with 1 and with 50
installed providers serving other capabilities; the second must not grow.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from tests.pilot_contract_batch import search_world, surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import RUNS, Scenario, Stopwatch

SLOT = "results"
CAPABILITY = f"{providers.RESULTS}@1"


def _refusal(instance: object, pilot: Scenario, package: providers.PilotPackage) -> tuple[str, str]:
    from cruxible_core.errors import RequestRefusedError

    name = f"{search_world.LINE}.{package.provider_id}"
    pilot.call(f"cruxible authoring submit line.json   ({name}: {SLOT} -> {package.provider_id})")
    with pytest.raises(RequestRefusedError) as refused:
        surfaces.accept_line(
            instance,
            search_world.line_input(
                {"search": "pilot-search", SLOT: package.provider_id}, name=name
            ),
        )
    return search_world.show_refusal(pilot, refused.value)


def _acceptance_seconds(instance: object, label: str) -> list[float]:
    samples = []
    for index in range(RUNS):
        with Stopwatch() as watch:
            surfaces.accept_line(
                instance,
                search_world.line_input(
                    {"search": "pilot-search"}, name=f"{search_world.LINE}.{label}-{index}"
                ),
            )
        samples.append(watch.seconds)
    return samples


@pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"owner: {surfaces.BINDING}")
@pytest.mark.pilot(
    "s4-binding",
    owner=surfaces.BINDING,
    title="bind a fitting provider; three that do not fit are refused with the note's messages",
)
def test_a_fitting_operation_binds_and_three_unfit_ones_are_refused(
    pilot: Scenario, tmp_path: Path
) -> None:
    instance, base = search_world.setup(pilot, tmp_path, search_world.calls_spec())
    fitting = providers.results_package(base)
    undeclared = providers.fetch_only_package(base)
    narrow = providers.narrow_request_package(base)
    untyped = providers.untyped_result_package(base)
    for package in (fitting, undeclared, narrow, untyped):
        search_world.install(pilot, instance, package)

    pilot.call(f"cruxible authoring submit line.json   (search -> pilot-search; {SLOT} unnamed)")
    accepted = surfaces.accept_line(instance, search_world.line_input({"search": "pilot-search"}))
    bound = accepted.slots[SLOT]
    pilot.got(f"accepted; {SLOT} -> {bound.operation} ({bound.capability.spelling}, {bound.fit})")

    undeclared_code, undeclared_message = _refusal(instance, pilot, undeclared)
    narrow_code, narrow_message = _refusal(instance, pilot, narrow)
    untyped_code, untyped_message = _refusal(instance, pilot, untyped)

    assert bound.operation == fitting.spelling
    assert bound.capability.spelling == CAPABILITY
    assert bound.fit == "identical"

    assert undeclared_code == "cruxible.line.binding_undeclared"
    for part in (f"`{SLOT}`", CAPABILITY, undeclared.spelling, f"serves {providers.FETCH}"):
        assert part in undeclared_message, (part, undeclared_message)

    assert narrow_code == "cruxible.line.binding_unfit"
    for part in (f"`{SLOT}`", CAPABILITY, narrow.spelling, "request.response"):
        assert part in narrow_message, (part, narrow_message)

    assert untyped_code == "cruxible.line.binding_unfit"
    for part in (
        f"`{SLOT}`",
        CAPABILITY,
        untyped.spelling,
        "result.results",
        "capture<*>",
        f"capture<{providers.RESULTS_KIND}>",
    ):
        assert part in untyped_message, (part, untyped_message)

    pilot.say()
    pilot.say("Cost: accepting one Line (its two slots bound by capability and fit), median of")
    pilot.say(f"{RUNS}, with 1 and then 50 installed providers serving other capabilities.")
    others = providers.other_capability_packages(50)
    search_world.install(pilot, instance, others[0])
    one = statistics.median(_acceptance_seconds(instance, "one-other"))
    for package in others[1:]:
        surfaces.install_provider_package(instance, package)
    fifty = statistics.median(_acceptance_seconds(instance, "fifty-others"))
    pilot.measure("binding_line_acceptance_1_other", one, "s")
    pilot.measure("binding_line_acceptance_50_others", fifty, "s")
    # Noise allowance only: the check seeks by capability name, so 49 more providers of
    # other capabilities add nothing it reads.
    assert fifty <= one * 1.25 + 0.05, (one, fifty)
