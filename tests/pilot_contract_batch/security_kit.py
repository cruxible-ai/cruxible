"""The security kit's start Runbook, offline, through the kit's own gate code.

The kit's gates (``kits/security/source/pilot`` in cruxible-kits) already build
the nearest offline equivalent of ``kit add`` plus ``runbook run security.start``:
``kit_world`` authors every kit definition into a fresh in-process instance
(the same payloads ``build_release.py`` builds the committed kit from, with
provider builds lowered from the real registrations), the automation template
is adopted as a change set, and ``security.start`` runs every step with each
provider answered from recordings: the real web adapter over recorded KEV and
EPSS bytes, the real security-osv-provider code over recorded OSV exchanges.

This module only reuses that code. It never records traffic and never writes
into the kits checkout (bytecode writing is off while its modules load).

Why not ``kit add`` of the committed directory: its providers install through
the real install path, which materializes each provider environment from the
package index (``provider_index_urls``), so it needs the network.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.pilot_contract_batch.harness import RUNS, Scenario, Stopwatch
from tests.pilot_contract_batch.kit_capabilities import (
    accept_kit_capabilities,
    accept_kit_layouts,
    converted_kit,
)

AFFECTED_BY = "security.component.affected_by"
OSV_STEP_IDS = ("matched", "affected")


def load(checkout: Path) -> SimpleNamespace:
    """The kit gate's modules from ``checkout``, which must be on ``PYTHONPATH``.

    Bytecode writing is turned off for the rest of the process first: the kit's
    modules import more of the kit lazily (its provider package among them), and
    none of it may write into the kits checkout.
    """

    import pytest

    pilot = (checkout / "kits/security/source/pilot").resolve()
    if not any(entry and Path(entry).resolve() == pilot for entry in sys.path):
        pytest.fail(
            f"put {pilot} on PYTHONPATH (python -m tests.pilot_contract_batch does it); "
            "the kit gate's modules import each other by name"
        )
    sys.dont_write_bytecode = True
    import definitions
    import inventory
    import osv
    import template
    import test_pilot_gate
    import test_start_gate

    return SimpleNamespace(
        definitions=definitions,
        inventory=inventory,
        osv=osv,
        template=template,
        pilot_gate=test_pilot_gate,
        start_gate=test_start_gate,
        root=checkout / "kits/security",
    )


@dataclass
class LineRun:
    line: str
    seconds: float
    status: str
    state: Any


@dataclass
class StartRun:
    """One fresh instance taken through kit install, template adoption and security.start."""

    instance: Any
    create_seconds: float
    kit_seconds: float
    template_seconds: float
    runbook: Any
    lines: list[LineRun] = field(default_factory=list)

    def line(self, name: str) -> LineRun:
        (found,) = [item for item in self.lines if item.line == name]
        return found


@contextmanager
def _patched(target: Any, name: str, replacement: Any) -> Iterator[Any]:
    original = getattr(target, name)
    setattr(target, name, replacement(original))
    try:
        yield original
    finally:
        setattr(target, name, original)


def start_run(
    kit: SimpleNamespace, root: Path, *, prepare: Callable[[Any], None] | None = None
) -> StartRun:
    """Create an instance, install the kit, adopt the template, run security.start.

    Each phase is timed by wrapping the kit gate's own calls: ``kit_world`` (less
    the instance it opens, ``initialize_local``), the template's ``submit``, and
    every ``service_run_playbill_line`` the Runbook makes. ``prepare`` runs on the
    instance between the kit install and the template (untimed): a scenario's
    own authoring, such as a successor of a kit definition.
    """

    import tests.core_support._support as core_support
    from cruxible_core.service.procedures import procedure_runs
    from tests.core_support._support import initialize_fresh

    for directory in (root / "fresh", root / "world"):
        directory.mkdir(parents=True, exist_ok=True)
    with Stopwatch() as created:
        initialize_fresh(root / "fresh")
    times: dict[str, float] = {"open": 0.0, "kit": 0.0, "template": 0.0}
    lines: list[LineRun] = []

    def timed_open(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with Stopwatch() as watch:
                result = original(*args, **kwargs)
            times["open"] += watch.seconds
            return result

        return wrapper

    def timed_kit(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with Stopwatch() as watch:
                result = original(*args, **kwargs)
            times["kit"] += watch.seconds
            if prepare is not None:
                prepare(result)
            return result

        return wrapper

    def timed_submit(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(instance: Any, payload: Any, *, name: str) -> Any:
            with Stopwatch() as watch:
                result = original(instance, payload, name=name)
            if name == "automation-template":
                times["template"] += watch.seconds
            return result

        return wrapper

    def with_capabilities(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(instance: Any, payload: Any, *, name: str) -> Any:
            result = original(instance, payload, name=name)
            if name == "vocabulary":
                # The pilot's copy of the kit: its slots name capabilities (slots
                # slice), authored here once every Contract they pin is accepted;
                # its extracted kinds gain their layouts (layout slice) before
                # any Procedure reads them.
                accept_kit_capabilities(instance)
                accept_kit_layouts(instance)
            return result

        return wrapper

    def timed_line(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with Stopwatch() as watch:
                state = original(*args, **kwargs)
            line = kwargs["request"].line
            lines.append(LineRun(line, watch.seconds, str(state.status), state))
            return state

        return wrapper

    gate = kit.start_gate
    sbom = (kit.root / "examples/sboms/log4j-demo-app.cdx.json").read_bytes()
    with (
        converted_kit(kit),
        _patched(core_support, "initialize_local", timed_open),
        _patched(gate, "kit_world", timed_kit),
        _patched(gate, "submit", timed_submit),
        _patched(kit.pilot_gate, "submit", with_capabilities),
    ):
        instance = gate.adopted_world(root / "world", sbom)
    with _patched(procedure_runs, "service_run_playbill_line", timed_line):
        runbook = gate.run_runbook(instance, gate.demo_replay(), kit.template.START)
    return StartRun(
        instance=instance,
        create_seconds=created.seconds,
        kit_seconds=times["kit"] - times["open"],
        template_seconds=times["template"],
        runbook=runbook,
        lines=lines,
    )


def live_claims(instance: Any, predicate: str) -> list[Any]:
    from cruxible_client.contracts.claims import parse_claim

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    found = []
    for path in tree:
        if path.startswith("claims/"):
            claim = parse_claim(tree[path], path=path)
            if claim.statement.predicate == predicate and claim.lifecycle.state == "live":
                found.append(claim)
    return found


def affected_pairs(instance: Any) -> set[tuple[str, str]]:
    """(purl, CVE) for every live ``affected_by`` Claim, read through the key Claims."""

    keys: dict[str, str] = {}
    for predicate in ("security.component.purl", "security.vulnerability.cve_id"):
        for claim in live_claims(instance, predicate):
            keys[claim.statement.subject.artifact_path] = str(claim.statement.object.value)
    return {
        (
            keys[claim.statement.subject.artifact_path],
            keys[claim.statement.object.address.artifact_path],
        )
        for claim in live_claims(instance, AFFECTED_BY)
    }


def osv_steps(kit: SimpleNamespace) -> dict[str, Any]:
    """The OSV Procedure's ``matched`` call and ``affected`` extract, as the kit authors them."""

    steps = kit.osv.osv_spec()["steps"]
    return {
        step_id: next(
            step for step in steps if step.get("call") == step_id or step.get("extract") == step_id
        )
        for step_id in OSV_STEP_IDS
    }


def accepted_osv_steps(instance: Any, kit: SimpleNamespace) -> dict[str, Any]:
    """The same two nodes as the accepted Procedure stores them."""

    from cruxible_client.contracts.procedures.artifacts import procedure_path

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    document = json.loads(tree[procedure_path(kit.osv.OSV_PROCEDURE)])
    nodes = document["definition"]["nodes"]
    return {node["id"]: node for node in nodes if node.get("id") in OSV_STEP_IDS}


def slot_binding_seconds(instance: Any, kit: SimpleNamespace) -> dict[str, float]:
    """Per slot of each template Line: seconds to resolve its binding at the head.

    The call measured is ``resolve_slot_bindings`` (the function Line acceptance and
    direct runs use) for that one slot, with the binding the template names, on
    an open accepted projection; opening the projection is outside the timing.
    Median of ``RUNS`` calls per slot.
    """

    import statistics

    from cruxible_core.capabilities.lookup import capability_in_projection
    from cruxible_core.providers.operation_index import line_slot_map, resolve_slot_bindings

    template = json.loads(kit.template.TEMPLATE.read_text())
    lines = [m for m in template["members"] if m["kind"] == "line" and m.get("bindings")]
    out: dict[str, float] = {}
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        typed = projection.typed

        def child(identity: Any) -> Any:
            source = typed.source(identity.qualified)
            return None if source is None else source.definition

        for line in lines:
            procedure = typed.source(f"Procedure:{line['procedure_name']}")
            slots = line_slot_map(
                procedure.definition,
                child,
                lambda ref: capability_in_projection(projection, ref),
            )
            for slot, reference in sorted(line["bindings"].items()):
                samples = []
                for _ in range(RUNS):
                    with Stopwatch() as watch:
                        resolve_slot_bindings(
                            typed.connection, {slot: slots[slot]}, {slot: reference}
                        )
                    samples.append(watch.seconds)
                out[f"{line['name']}.{slot}"] = statistics.median(samples)
    return out


def line_acceptance_seconds(instance: Any, kit: SimpleNamespace) -> dict[str, float]:
    """Seconds to author and accept one Line, for each template Line with slots.

    Each is accepted again under a new name (``<line>.pilot-copy``, propose only,
    same Procedure, bindings, parameters, acquisition and findings), so its slots
    are bound at acceptance exactly as the template's were. Runs after the
    measured Runbook, on the same instance.
    """

    from cruxible_client.contracts.authoring.inputs import LineInput
    from tests.support.operation_world import author

    template = json.loads(kit.template.TEMPLATE.read_text())
    out: dict[str, float] = {}
    for member in template["members"]:
        if member["kind"] != "line" or not member.get("bindings"):
            continue
        copy = {**member, "name": member["name"] + ".pilot-copy", "max_authority": "propose"}
        with Stopwatch() as watch:
            author(instance, LineInput.model_validate(copy), name=copy["name"])
        out[member["name"]] = watch.seconds
    return out


def spelled(value: Any) -> Any:
    """Authoring references (``{"$ref": ...}``) shown as written: ``$steps.x.y``, ``$params.x``."""

    if isinstance(value, dict):
        if "$ref" in value:
            from cruxible_client.contracts.refs import Ref

            return Ref.model_validate(value).graph_text()
        return {key: spelled(item) for key, item in value.items()}
    if isinstance(value, list):
        return [spelled(item) for item in value]
    return value


def summarize_runbook(pilot: Scenario, run: StartRun) -> None:
    pilot.got(f"status: {run.runbook.status}   stop_reason: {run.runbook.stop_reason}")
    for step in run.runbook.steps:
        name = step.target.line.target.name
        seconds = next((item.seconds for item in run.lines if item.line == name), None)
        took = "" if seconds is None else f"   {seconds:.2f} s"
        pilot.got(f"step {step.step}: {name:42} {step.status}{took}")


def kit_inventory(instance: Any) -> Mapping[str, int]:
    """How many of each definition kind the instance holds after the kit install."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    counts: dict[str, int] = {}
    for path in tree:
        head = path.split("/", 1)[0]
        if head in {
            "contracts",
            "capture-contracts",
            "claim-types",
            "subject-kinds",
            "query-definitions",
            "procedures",
            "providers",
        }:
            counts[head] = counts.get(head, 0) + 1
    return dict(sorted(counts.items()))
