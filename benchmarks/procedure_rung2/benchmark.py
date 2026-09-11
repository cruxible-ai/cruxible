"""Phase timings for one Source Line occurrence, before and after the rung-2 bridge.

Never collected by pytest and never run by CI: it lives under `benchmarks/`
and carries no `test_` prefix. Run it explicitly:

    uv run python benchmarks/procedure_rung2/benchmark.py run \\
        --root /path/to/scratch --out results.json
    uv run python benchmarks/procedure_rung2/benchmark.py report results.json

The workload is fixed: one graph-v4 Procedure that reads one workspace file
through the seeded `workspace.file` Provider, shapes it, and hands the shaped
row to a `propose_change_set` terminal carrying one or eight items. It is run as
one manual Line occurrence per sample under a live rung-2 ProcedureMandate.

Cells are the product of two accepted-Claim populations and two retained
history sizes. Each cell runs in a **fresh process**: the first sample is the
cold one, and the following samples are warm operations in the same process,
each a new occurrence one minute later on the daemon clock.

Phases are measured at the seams the bridge touches, by wrapping the exact
functions that run there, so the same harness reports the proposal-side phases
as *unavailable* while the bridge is absent and as timings once it exists. The
manager-side phases (inspect, activate, readback) are measured only when a run
actually produced a proposal.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

PHASES = (
    "line_total",
    "admission",
    "execute",
    "source_read",
    "provider",
    "item_closure",
    "lowering",
    "proposal_submit",
    "receipt_persist",
    "manager_inspect",
    "manager_activate",
    "manager_readback",
)

PROPOSAL_PHASES = frozenset(
    {
        "lowering",
        "proposal_submit",
        "receipt_persist",
        "manager_inspect",
        "manager_activate",
        "manager_readback",
    }
)


@dataclass
class Counters:
    git_invocations: int = 0
    blobs_read: int = 0
    blob_bytes_read: int = 0
    trees_listed: int = 0
    journal_appends: int = 0
    journal_bytes: int = 0
    proposal_submits: int = 0
    lowerings: int = 0
    phase_seconds: dict[str, float] = field(default_factory=dict)
    phase_calls: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "git_invocations": self.git_invocations,
            "blobs_read": self.blobs_read,
            "blob_bytes_read": self.blob_bytes_read,
            "trees_listed": self.trees_listed,
            "journal_appends": self.journal_appends,
            "journal_bytes": self.journal_bytes,
            "proposal_submits": self.proposal_submits,
            "lowerings": self.lowerings,
            "phase_seconds": dict(self.phase_seconds),
            "phase_calls": dict(self.phase_calls),
        }

    def reset(self) -> None:
        self.git_invocations = 0
        self.blobs_read = 0
        self.blob_bytes_read = 0
        self.trees_listed = 0
        self.journal_appends = 0
        self.journal_bytes = 0
        self.proposal_submits = 0
        self.lowerings = 0
        self.phase_seconds = {}
        self.phase_calls = {}

    def add(self, phase: str, seconds: float) -> None:
        self.phase_seconds[phase] = self.phase_seconds.get(phase, 0.0) + seconds
        self.phase_calls[phase] = self.phase_calls.get(phase, 0) + 1


COUNTERS = Counters()


def _timed(phase: str, original):  # type: ignore[no-untyped-def]
    @functools.wraps(original)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            COUNTERS.add(phase, time.perf_counter() - started)

    return wrapper


def _instrument() -> None:
    """Wrap the exact seams the bridge runs through; nothing changes what they do."""

    from cruxible_core.authoring import lowering
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.ledger.git import GitLedger
    from cruxible_core.procedures import execution
    from cruxible_core.proposals import proposals
    from cruxible_core.service.procedures import procedure_runs as playbill_procedure_runs
    from cruxible_core.service.procedures import procedures as playbill_procedures

    original_git = GitLedger._git
    original_blobs = GitLedger.read_blobs
    original_list = GitLedger._list_tree

    def counted_git(self, arguments, **kwargs):  # type: ignore[no-untyped-def]
        COUNTERS.git_invocations += 1
        return original_git(self, arguments, **kwargs)

    def counted_blobs(self, oids):  # type: ignore[no-untyped-def]
        blobs = original_blobs(self, oids)
        COUNTERS.blobs_read += len(blobs)
        COUNTERS.blob_bytes_read += sum(len(value) for value in blobs.values())
        return blobs

    def counted_list(self, oid, *, with_sizes):  # type: ignore[no-untyped-def]
        COUNTERS.trees_listed += 1
        return original_list(self, oid, with_sizes=with_sizes)

    GitLedger._git = counted_git  # type: ignore[method-assign]
    GitLedger.read_blobs = counted_blobs  # type: ignore[method-assign]
    GitLedger._list_tree = counted_list  # type: ignore[method-assign]

    WorkspaceFileReader.read = _timed("source_read", WorkspaceFileReader.read)  # type: ignore[method-assign]
    execution.ProcedureExecutor._invoke_provider_v4 = _timed(  # type: ignore[method-assign]
        "provider", execution.ProcedureExecutor._invoke_provider_v4
    )
    execution.ProcedureExecutor._record_terminal_items = _timed(  # type: ignore[method-assign]
        "item_closure", execution.ProcedureExecutor._record_terminal_items
    )

    original_append = execution.ProcedureExecutor._append_event

    @functools.wraps(original_append)
    def counted_append(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        stored = original_append(self, admission, records, event_kind, payload)
        elapsed = time.perf_counter() - started
        COUNTERS.journal_appends += 1
        COUNTERS.journal_bytes += len(execution.journal_payload_bytes(payload))
        if event_kind == "terminal_egress":
            COUNTERS.add("receipt_persist", elapsed)
        return stored

    execution.ProcedureExecutor._append_event = counted_append  # type: ignore[method-assign]

    original_submit = proposals.ProposalService.submit

    @functools.wraps(original_submit)
    def counted_submit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        COUNTERS.proposal_submits += 1
        return _timed("proposal_submit", original_submit)(self, *args, **kwargs)

    proposals.ProposalService.submit = counted_submit  # type: ignore[method-assign]

    original_lower = lowering.lower_authoring

    @functools.wraps(original_lower)
    def counted_lower(*args, **kwargs):  # type: ignore[no-untyped-def]
        COUNTERS.lowerings += 1
        return _timed("lowering", original_lower)(*args, **kwargs)

    lowering.lower_authoring = counted_lower  # type: ignore[assignment]
    try:
        from cruxible_core.procedures import proposal_delivery

        if hasattr(proposal_delivery, "lower_authoring"):
            proposal_delivery.lower_authoring = counted_lower  # type: ignore[attr-defined]
    except ImportError:
        pass

    playbill_procedures.service_execute_direct_procedure = _timed(  # type: ignore[assignment]
        "execute", playbill_procedures.service_execute_direct_procedure
    )
    playbill_procedure_runs.service_execute_direct_procedure = (  # type: ignore[attr-defined]
        playbill_procedures.service_execute_direct_procedure
    )


def _peak_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if sys.platform == "darwin" else usage * 1024


# ---------------------------------------------------------------------------
# The fixed workload
# ---------------------------------------------------------------------------


SUBJECT_KIND = "security.advisory"
SUBJECT_ID = "osv-2026-0001"
PREDICATE = "security.advisory.severity"


def _claim_type(capture_contract_digest_value: str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.claim_types import ClaimType
    from cruxible_client.contracts.policies import (
        ClaimAdmissionPolicyV1,
        ClaimEvidenceAdmissionPolicyV1,
        ClaimEvidenceAdmissionRuleV1,
        ClaimResolutionPolicyV1,
    )

    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        predicate=PREDICATE,
        allowed_subject_kinds=(SUBJECT_KIND,),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="workspace-record",
                    claim_roles=("observation",),
                    capture_contract_digests=(capture_contract_digest_value,),
                    evidence_kinds=("database_record",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _item_template(index: int = 0) -> dict[str, object]:
    """One Claim proposal item; items after the first are qualified so they are distinct."""

    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_client.contracts.subjects import subject_path

    return {
        "tag": "playbill-procedure-claim-proposal-item-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": SemanticAddress.whole_artifact(
                subject_path(SUBJECT_KIND, SUBJECT_ID)
            ).model_dump(mode="json"),
            "predicate": PREDICATE,
            "qualifier": None if index == 0 else f"item-{index}",
            "object": {"kind": "literal", "value": "$steps.result.severity"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Severity as read from the accepted advisory document.",
        "revises": None,
    }


def _build_world(root: Path, *, population: int, history: int, items: int):  # type: ignore[no-untyped-def]
    """Accept the fixed workload plus the requested population and history."""

    from tests.core_support._pc_c_support import capture_contract
    from tests.test_procedures import test_procedure_source_runs as fixtures

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.captures import capture_contract_digest
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_client.contracts.procedures.graph import (
        compute_procedure_definition_digest_v4,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_client.contracts.procedures.models import ProposeChangeSetNodeV3
    from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path

    phases: dict[str, float] = {}
    started = time.perf_counter()
    instance, owner, procedure, workspace_root, policy = fixtures._world(
        root, accept_procedure=False
    )
    phases["world"] = time.perf_counter() - started

    started = time.perf_counter()
    source, shape = procedure.definition.nodes
    shape = shape.model_copy(update={"next": "propose"})
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source,
                shape,
                ProposeChangeSetNodeV3(
                    node_id="propose",
                    candidate_templates=tuple(_item_template(index) for index in range(items)),
                ),
            ),
        }
    )
    terminal_procedure = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    line = fixtures._served_line(terminal_procedure, policy).model_copy(
        update={"requested_terminal_rung": 2}
    )
    mandate = fixtures._line_mandate(terminal_procedure)
    contract = capture_contract()
    claim_type = _claim_type(capture_contract_digest(contract).tagged)
    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{SUBJECT_ID}"),
        subject_kind=SUBJECT_KIND,
        subject_id=SUBJECT_ID,
    )
    fixtures._accept_more(
        instance,
        owner,
        {
            procedure_path(fixtures.PROCEDURE_NAME): render_procedure(terminal_procedure),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            claim_type_path(claim_type.predicate): render_claim_type(claim_type),
            subject_path(SUBJECT_KIND, SUBJECT_ID): render_subject(shell),
        },
        name="rung2-workload",
    )
    phases["workload"] = time.perf_counter() - started

    started = time.perf_counter()
    if population:
        _seed_population(instance, owner, population)
    phases["population"] = time.perf_counter() - started

    started = time.perf_counter()
    for index in range(history):
        filler = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/filler-{index:04d}"),
            subject_kind=SUBJECT_KIND,
            subject_id=f"filler-{index:04d}",
        )
        fixtures._accept_more(
            instance,
            owner,
            {subject_path(SUBJECT_KIND, filler.subject_id): render_subject(filler)},
            name=f"history-{index:04d}",
        )
    phases["history"] = time.perf_counter() - started
    return instance, owner, workspace_root, line, phases


def _seed_population(instance, owner, population: int) -> None:  # type: ignore[no-untyped-def]
    """Accept `population` foreign-source work-item Claims, one generation each."""

    from tests.core_support import _knowledge_loop_support as loop
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.test_claims.test_claims import _claim_type
    from tests.test_procedures import test_procedure_source_runs as fixtures

    from cruxible_client.contracts.captures import (
        DirectForeignSourceSelectionV1,
        capture_contract_digest,
        capture_contract_path,
        foreign_source_capture_contract,
        render_capture_contract,
    )
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicyV1,
        ClaimEvidenceAdmissionRuleV1,
    )
    from cruxible_client.contracts.semantic import ContentSpan
    from cruxible_client.contracts.subjects import render_subject, subject_path

    source_id = "fixture.work-items"
    contract = foreign_source_capture_contract(source_id)
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="coordinator-source",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    members = {
        claim_type_path(claim_type.predicate): render_claim_type(claim_type),
        capture_contract_path(contract.identity.name): render_capture_contract(contract),
    }
    for index in range(population):
        shell = loop.subject_shell(f"wi-{index:04d}")
        members[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    fixtures._accept_more(instance, owner, members, name="population-surface")
    for index in range(population):
        body = f"status: ready {index}".encode()
        stored = instance.body_store().store(body)
        proposed = service_propose_playbill_claim(
            instance,
            authoring=loop.authoring(f"wi-{index:04d}", "ready", with_claim_type=False).model_copy(
                update={
                    "source_selection": DirectForeignSourceSelectionV1(
                        logical_source_identity=source_id,
                        span=ContentSpan(
                            content_digest=stored.digest,
                            start_byte=0,
                            end_byte=len(body),
                        ),
                    )
                }
            ),
            actor_id="owner",
            proposal_name=f"population-{index:04d}",
            timestamp=loop.TIMESTAMP,
        )
        loop.activate(instance, owner, proposed)


def _manager_loop(instance, owner, state, *, activate: bool) -> dict[str, float]:  # type: ignore[no-untyped-def]
    """Inspect, activate, and read back the proposal a run produced, if any."""

    egress = getattr(state, "terminal_egress", ())
    delivered = [
        item
        for item in egress
        if getattr(item, "proposal_id", None) is not None
        and getattr(item, "candidate_digest", None) is not None
    ]
    if not delivered:
        return {}
    from tests.core_support._knowledge_loop_support import accept_proposal

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal

    receipt = delivered[0]
    timings: dict[str, float] = {}
    started = time.perf_counter()
    inspection = service_inspect_playbill_proposal(instance, proposal_id=receipt.proposal_id)
    timings["manager_inspect"] = time.perf_counter() - started
    assert inspection.proposal.candidate is not None
    assert inspection.proposal.candidate.candidate_digest == receipt.candidate_digest
    if not activate:
        # Every sample but the last leaves its proposal open: once one is
        # activated the Claim slot is occupied, and a further fresh Claim into
        # it is refused by the disposition law rather than measured. The last
        # sample measures acceptance and readback once.
        return timings
    started = time.perf_counter()
    accept_proposal(instance, owner, inspection)
    timings["manager_activate"] = time.perf_counter() - started
    started = time.perf_counter()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for child in receipt.children:
        path = getattr(child, "path", None)
        if path is not None and path.startswith("claims/"):
            parse_claim(tree[path], path=path)
    timings["manager_readback"] = time.perf_counter() - started
    return timings


def _sample(instance, owner, workspace_root, line, *, clock, activate) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    from tests.test_procedures import test_procedure_source_runs as fixtures

    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequestV1,
        service_run_playbill_line,
    )

    COUNTERS.reset()
    identity_digest = line_identity_digest(line.identity)
    started = time.perf_counter()
    state = service_run_playbill_line(
        instance,
        path_identity_digest=identity_digest,
        request=LineRunRequestV1(
            line_identity_digest=identity_digest,
            occurrence_id=None,
            evaluation_time=None,
        ),
        actor_context=fixtures._actor(instance).model_copy(
            update={"timestamp": clock.evaluation_time}
        ),
        caller_rung=2,
        provider_runtime_operator=fixtures._Operator(fixtures._WorkspaceInvoker()),  # type: ignore[arg-type]
        workspace_file_reader=fixtures._reader(instance, workspace_root),
        daemon_clock=clock,
    )
    total = time.perf_counter() - started
    COUNTERS.add("line_total", total)
    execute = COUNTERS.phase_seconds.get("execute", 0.0)
    COUNTERS.add("admission", total - execute)
    manager = _manager_loop(instance, owner, state, activate=activate)
    for phase, seconds in manager.items():
        COUNTERS.add(phase, seconds)
    terminal = state.terminal
    return {
        "status": state.status,
        "terminal_code": None if terminal is None else getattr(terminal, "code", None),
        "run_id": state.run_id,
        "proposal_delivered": bool(manager),
        "counters": COUNTERS.snapshot(),
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def cell(args: argparse.Namespace) -> None:
    _instrument()
    from tests.test_procedures import test_procedure_source_runs as fixtures

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    instance, owner, workspace_root, line, setup = _build_world(
        root,
        population=args.population,
        history=args.history,
        items=args.items,
    )
    setup["total"] = time.perf_counter() - started
    samples = []
    for index in range(1 + args.warm):
        clock = fixtures._TestClock(fixtures.NOW + timedelta(minutes=index))
        samples.append(
            _sample(instance, owner, workspace_root, line, clock=clock, activate=index == args.warm)
        )
    result = {
        "population": args.population,
        "history": args.history,
        "items": args.items,
        "generations_after_setup": len(instance.accepted_history()),
        "setup_seconds": setup,
        "cold": samples[0],
        "warm": samples[1:],
    }
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    cells = []
    for population in args.populations:
        for history in args.histories:
            for items in args.items:
                name = f"p{population}-h{history}-i{items}"
                out = root / f"{name}.json"
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "cell",
                        "--root",
                        str(root / name),
                        "--population",
                        str(population),
                        "--history",
                        str(history),
                        "--items",
                        str(items),
                        "--warm",
                        str(args.warm),
                        "--out",
                        str(out),
                    ],
                    check=True,
                    cwd=str(REPOSITORY_ROOT),
                    env={**os.environ, "PYTHONHASHSEED": "0"},
                )
                cells.append(json.loads(out.read_text(encoding="utf-8")))
    Path(args.out).write_text(
        json.dumps({"cells": cells, "warm": args.warm}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(_render(cells))


def _render(cells: list[dict[str, Any]]) -> str:
    lines = [
        "| cell | gens | status | phase | cold s | warm median s | warm max s | calls |",
        "|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for item in cells:
        name = f"p{item['population']}-h{item['history']}-i{item['items']}"
        cold = item["cold"]["counters"]["phase_seconds"]
        warm = [sample["counters"]["phase_seconds"] for sample in item["warm"]]
        status = f"{item['cold']['status']}/{item['cold']['terminal_code'] or 'ok'}"
        for phase in PHASES:
            cold_value = cold.get(phase)
            warm_values = [sample[phase] for sample in warm if phase in sample]
            if cold_value is None and not warm_values:
                if phase in PROPOSAL_PHASES:
                    lines.append(
                        f"| {name} | {item['generations_after_setup']} | {status} | {phase} "
                        "| unavailable | unavailable | unavailable | 0 |"
                    )
                continue
            calls = item["cold"]["counters"]["phase_calls"].get(phase, 0)
            lines.append(
                f"| {name} | {item['generations_after_setup']} | {status} | {phase} | "
                f"{cold_value if cold_value is not None else float('nan'):.4f} | "
                f"{statistics.median(warm_values) if warm_values else float('nan'):.4f} | "
                f"{max(warm_values) if warm_values else float('nan'):.4f} | {calls} |"
            )
        counters = item["cold"]["counters"]
        lines.append(
            f"| {name} | {item['generations_after_setup']} | {status} | counters (cold) | "
            f"git={counters['git_invocations']} blobs={counters['blobs_read']} "
            f"journal={counters['journal_appends']} ({counters['journal_bytes']} B) | "
            f"submits={counters['proposal_submits']} lowerings={counters['lowerings']} | "
            f"rss={item['cold']['peak_rss_bytes'] // (1024 * 1024)} MiB | - |"
        )
    return "\n".join(lines)


def report(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.results).read_text(encoding="utf-8"))
    print(_render(payload["cells"]))


def _phase_stats(
    cell: dict[str, Any], phase: str
) -> tuple[float | None, float | None, float | None]:
    cold = cell["cold"]["counters"]["phase_seconds"].get(phase)
    warm = [
        sample["counters"]["phase_seconds"][phase]
        for sample in cell["warm"]
        if phase in sample["counters"]["phase_seconds"]
    ]
    if cold is None and not warm:
        return None, None, None
    return cold, (statistics.median(warm) if warm else None), (max(warm) if warm else None)


def _fmt(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.4f}"


def compare(args: argparse.Namespace) -> None:
    """Render before/after phase tables from two `run` result files, cell by cell."""

    before = {
        (c["population"], c["history"], c["items"]): c
        for c in json.loads(Path(args.before).read_text(encoding="utf-8"))["cells"]
    }
    after = {
        (c["population"], c["history"], c["items"]): c
        for c in json.loads(Path(args.after).read_text(encoding="utf-8"))["cells"]
    }
    lines = [
        "| cell | phase | before cold | before warm median | after cold | after warm median "
        "| after warm max |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(set(before) | set(after)):
        name = f"p{key[0]}-h{key[1]}-i{key[2]}"
        b = before.get(key)
        a = after.get(key)
        for phase in PHASES:
            bc, bm, _bx = _phase_stats(b, phase) if b else (None, None, None)
            ac, am, ax = _phase_stats(a, phase) if a else (None, None, None)
            if bc is None and bm is None and ac is None and am is None:
                continue
            lines.append(
                f"| {name} | {phase} | {_fmt(bc)} | {_fmt(bm)} | {_fmt(ac)} | {_fmt(am)} "
                f"| {_fmt(ax)} |"
            )
        if a is not None:
            counters = a["cold"]["counters"]
            status = f"{a['cold']['status']}/{a['cold']['terminal_code'] or 'ok'}"
            lines.append(
                f"| {name} | after cold counters ({status}) | - | - | "
                f"git={counters['git_invocations']} blobs={counters['blobs_read']} "
                f"journal={counters['journal_appends']} ({counters['journal_bytes']} B) | "
                f"submits={counters['proposal_submits']} lowerings={counters['lowerings']} "
                f"rss={a['cold']['peak_rss_bytes'] // (1024 * 1024)} MiB | - |"
            )
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--root", required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--populations", type=int, nargs="+", default=[0, 60])
    run_parser.add_argument("--histories", type=int, nargs="+", default=[0, 30])
    run_parser.add_argument("--items", type=int, nargs="+", default=[1, 8])
    run_parser.add_argument("--warm", type=int, default=5)
    run_parser.set_defaults(func=run)

    cell_parser = commands.add_parser("cell")
    cell_parser.add_argument("--root", required=True)
    cell_parser.add_argument("--population", type=int, required=True)
    cell_parser.add_argument("--history", type=int, required=True)
    cell_parser.add_argument("--items", type=int, required=True)
    cell_parser.add_argument("--warm", type=int, required=True)
    cell_parser.add_argument("--out", required=True)
    cell_parser.set_defaults(func=cell)

    report_parser = commands.add_parser("report")
    report_parser.add_argument("results")
    report_parser.set_defaults(func=report)

    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("before")
    compare_parser.add_argument("after")
    compare_parser.set_defaults(func=compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
