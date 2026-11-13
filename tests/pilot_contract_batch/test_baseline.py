"""Baseline: the security kit's start Runbook, offline, as it stands before the batch.

Green today; never an expected failure. Its transcript shows the OSV Procedure's
``matched`` call and ``affected`` extract as authored today and the
``affected_by`` Claims the run writes; its numbers are what later scenarios
compare with (scenario 6 reruns the same flow with a thin EXTRACT).
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from tests.pilot_contract_batch import security_kit
from tests.pilot_contract_batch.harness import (
    RUNS,
    Scenario,
    kits_checkout,
    no_network,
    require_providers,
)


@pytest.mark.xfail(strict=True, reason="owner: capture-as-field")
@pytest.mark.pilot(
    "baseline",
    owner="none (baseline)",
    title="security kit installed into a fresh instance; security.start run offline",
)
def test_baseline_security_start_runs_offline(pilot: Scenario, tmp_path: Path) -> None:
    kit = security_kit.load(kits_checkout())
    require_providers()
    pilot.say("Route: the kit gate's own code (kits/security/source/pilot), not `kit add` of")
    pilot.say("the committed directory: that path installs providers from the package index.")
    pilot.say("Every provider call is answered from recordings; Internet sockets are refused.")
    pilot.say()

    runs = []
    with no_network() as network:
        for index in range(RUNS):
            runs.append(security_kit.start_run(kit, tmp_path / f"run-{index}"))
    assert network.attempts == []
    first = runs[0]

    pilot.call("cruxible init   (in process: PlaybillInstance.initialize, fresh keys and ledger)")
    pilot.got(f"created in {first.create_seconds:.2f} s")
    pilot.call("install the kit's definitions   (kit gate: kit_world)")
    pilot.got(f"done in {first.kit_seconds:.2f} s; the instance now holds:")
    for directory, count in security_kit.kit_inventory(first.instance).items():
        pilot.got(f"  {directory:20} {count}")

    pilot.say()
    pilot.say(f"The OSV Procedure ({kit.osv.OSV_PROCEDURE}) as authored today:")
    for step_id, step in security_kit.osv_steps(kit).items():
        pilot.show(f"step {step_id}", security_kit.spelled(step))
    accepted = security_kit.accepted_osv_steps(first.instance, kit)
    assert set(accepted) == set(security_kit.OSV_STEP_IDS), sorted(accepted)

    template = json.loads(kit.template.TEMPLATE.read_text())
    kinds = [m["kind"] for m in template["members"]]
    lines = [m["name"] for m in template["members"] if m["kind"] == "line"]
    pilot.say()
    pilot.call("cruxible authoring submit automation.json   (the kit's automation template)")
    pilot.got(
        f"accepted in {first.template_seconds:.2f} s: {kinds.count('line')} Lines, "
        f"{kinds.count('procedure_mandate')} Mandates, {kinds.count('runbook')} Runbooks, "
        f"{kinds.count('trigger')} Trigger"
    )
    for name in lines:
        pilot.got(f"  line {name}")

    pilot.say()
    pilot.call(f"cruxible runbook run {kit.template.START}")
    security_kit.summarize_runbook(pilot, first)
    for run in runs:
        assert (run.runbook.status, run.runbook.stop_reason) == ("completed", None), run.runbook
        assert [s.status for s in run.runbook.steps] == ["succeeded"] * len(
            kit.template.START_LINES
        )

    osv_line = kit.osv.OSV_LINE
    pairs = sorted(security_kit.affected_pairs(first.instance))
    written = [len(security_kit.affected_pairs(run.instance)) for run in runs]
    pilot.say()
    pilot.call(f"cruxible query ... live {security_kit.AFFECTED_BY} Claims")
    pilot.got(f"{len(pairs)} Claims written by {osv_line} (component, vulnerability):")
    for purl, cve in pairs:
        pilot.got(f"  {purl}  ->  {cve}")
    assert pairs, "the demo SBOM's recorded OSV answers name affected components"
    assert written == [len(pairs)] * RUNS, written

    pilot.say()
    pilot.say(f"Cost: median of {RUNS} fresh runs.")
    pilot.median("instance_create", [r.create_seconds for r in runs])
    pilot.median("kit_add", [r.kit_seconds for r in runs])
    pilot.median("line_acceptance_template", [r.template_seconds for r in runs])
    pilot.say(
        f"  (one change set: {kinds.count('line')} Lines with their Mandates, Runbooks and Trigger)"
    )
    pilot.median("osv_run", [r.line(osv_line).seconds for r in runs])
    pilot.measure("osv_claims_written", len(pairs))
    pilot.median("start_runbook", [sum(item.seconds for item in r.lines) for r in runs])

    pilot.say()
    pilot.say("Binding per slot: resolve_slot_bindings for that one slot with the binding the")
    pilot.say("template names, on an open accepted projection (projection open not timed);")
    pilot.say(f"median of {RUNS} calls per slot, then the median of that across the {RUNS} runs.")
    per_run = [security_kit.slot_binding_seconds(run.instance, kit) for run in runs]
    for slot in sorted(per_run[0]):
        value = statistics.median(item[slot] for item in per_run) * 1000
        pilot.measure(f"binding.{slot}", value, "ms")

    pilot.say()
    pilot.say("Line acceptance, one Line at a time: each template Line with slots accepted again")
    pilot.say("under a new name (same Procedure, bindings and parameters; propose only), after")
    pilot.say("the Runbook, on each run's instance:")
    acceptance = [security_kit.line_acceptance_seconds(run.instance, kit) for run in runs]
    for line in sorted(acceptance[0]):
        pilot.median(f"line_acceptance.{line}", [item[line] for item in acceptance])
