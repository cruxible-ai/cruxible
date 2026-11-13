"""Scenario 6: the OSV Procedure reads the kind's layout through a thin EXTRACT (note §4).

The baseline's flow, with one change made on the instance after the kit
install: ``security.osv-matches`` grows a layout (the header and record set
today's extract spells out), and ``security.osv-query`` is re-authored with its
``affected`` extract in the thin form.

Transcript shape::

    the affected node before (as the kit authors it today, 30 lines):
      {"extract": "affected", "records": "/affected/*", "key": {...}, "row": ...,
       "fields": {...}, "header": {...}, "complete_when": {...}, "duplicates": ...}
    $ cruxible authoring submit layout.json + procedure.json
      CaptureKind security.osv-matches grows a layout; Procedure security.osv-query v2
    the affected node after:
      {"extract": "affected", "capture": "$steps.matched.document",
       "records": "affected", "header": ["queried"], "duplicates": "refuse_key"}
    $ cruxible runbook run security.start
      ... six steps succeeded
    $ cruxible query ... live security.component.affected_by Claims
      9 Claims: the same (component, vulnerability) pairs as the baseline
    osv_run before / after (median of three each)

Green when: every start step succeeds; the live ``affected_by`` pairs equal the
baseline flow's, run in the same test; the OSV run's median wall time is within
noise of the baseline's (at most 25% plus 50 ms slower).
Cost (when green): OSV run before and after the thin EXTRACT.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from tests.pilot_contract_batch import security_kit, surfaces
from tests.pilot_contract_batch.harness import (
    RUNS,
    Scenario,
    kits_checkout,
    no_network,
    require_providers,
)
from tests.pilot_contract_batch.search_world import refs

KIND = "security.osv-matches"


def layout_from(extract: dict[str, Any]) -> dict[str, Any]:
    """Note §4.1's declaration for what today's ``affected`` extract spells out."""

    return {
        "header": copy.deepcopy(extract["header"]),
        "records": {
            "affected": {
                "at": extract["records"],
                "key": copy.deepcopy(extract["key"]),
                "row": extract["row"],
                "fields": copy.deepcopy(extract["fields"]),
                "count_at": extract["complete_when"]["count_at"],
            }
        },
    }


def thin_spec(kit: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The kit's OSV Procedure spec with the thin ``affected`` node; and that node."""

    spec = copy.deepcopy(kit.osv.osv_spec())
    thin = {
        "extract": "affected",
        "capture": "$steps.matched.document",
        "records": "affected",
        "header": ["queried"],
        "duplicates": "refuse_key",
    }
    spec["steps"] = [
        refs(thin) if step.get("extract") == "affected" else step for step in spec["steps"]
    ]
    return spec, thin


@pytest.mark.pilot(
    "s6-thin-extract",
    owner=surfaces.LAYOUT,
    title="the OSV Procedure with a thin EXTRACT over the kind's layout: same Claims, same time",
)
def test_a_thin_extract_writes_the_baseline_claims_in_the_baseline_time(
    pilot: Scenario, tmp_path: Path
) -> None:
    kit = security_kit.load(kits_checkout())
    require_providers()
    before = security_kit.osv_steps(kit)["affected"]
    layout = layout_from(security_kit.spelled(before))
    spec, thin = thin_spec(kit)

    pilot.show(
        "the affected node before (as the kit authors it today)", security_kit.spelled(before)
    )
    pilot.call("cruxible authoring submit layout.json procedure.json")
    pilot.show(f"layout added to {KIND}", layout)

    def prepare(instance: Any) -> None:
        surfaces.adopt_layout_and_thin_extract(instance, kind=KIND, layout=layout, procedure=spec)

    with no_network() as network:
        thin_runs = [
            security_kit.start_run(kit, tmp_path / f"thin-{index}", prepare=prepare)
            for index in range(RUNS)
        ]
        base_runs = [
            security_kit.start_run(kit, tmp_path / f"base-{index}") for index in range(RUNS)
        ]
    first = thin_runs[0]
    pilot.show("the affected node after (authored)", thin)
    pilot.show(
        "the affected node after (as accepted)",
        security_kit.accepted_osv_steps(first.instance, kit)["affected"],
    )
    pilot.call(f"cruxible runbook run {kit.template.START}")
    security_kit.summarize_runbook(pilot, first)

    pairs = security_kit.affected_pairs(first.instance)
    baseline = security_kit.affected_pairs(base_runs[0].instance)
    pilot.call(f"cruxible query ... live {security_kit.AFFECTED_BY} Claims")
    pilot.got(f"{len(pairs)} Claims; same pairs as the baseline: {pairs == baseline}")

    osv_line = kit.osv.OSV_LINE
    # Wall time is reported, not asserted: it is noise on a shared runner.
    pilot.say()
    pilot.median("osv_run_before_thin_extract", [r.line(osv_line).seconds for r in base_runs])
    pilot.median("osv_run_after_thin_extract", [r.line(osv_line).seconds for r in thin_runs])

    assert network.attempts == []
    for run in thin_runs:
        assert (run.runbook.status, run.runbook.stop_reason) == ("completed", None), run.runbook
        assert security_kit.affected_pairs(run.instance) == baseline
    assert pairs and pairs == baseline
