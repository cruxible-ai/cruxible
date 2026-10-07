"""Procedure runs as read rows: listed from the journal index, never fully replayed.

A run's authoritative account is its journal chain, rebuilt by
``service_get_playbill_procedure_run``. Listing many runs never replays them:
the journal index locates each run's admission and final records, and only a
page's rows read those two payloads (the Procedure and Line from the admission,
the terminal status from the final record). Rows list the newest admissions
first, in an order a run's status never changes; a filter keeps running runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from cruxible_client.contracts import ProcedureRunState
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.operational_reads import (
    OPERATIONAL_CARD_LIST_LIMIT,
    GetProcedureRunCard,
    GetRunCurrentNode,
    GetRunNode,
    GetRunTrigger,
    RunRow,
    RunStatus,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureOperationalFailureCode,
    ProcedureRunAttribution,
    ProcedureRunAttributionWithheld,
    ProcedureRunReceiptWithheld,
)
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.journal_index import RunLocator, RunLocatorKey
from cruxible_core.exhaust.records import JournalStreamIdentityV1, parse_journal_payload
from cruxible_core.procedures.execution import parse_admission_payload, procedure_line_partition
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.operational_viewer import (
    OperationalViewer,
    may_see_arming,
)
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="playbill-run-reads", can_read_body=True)
_OPERATIONAL_FAILURES = frozenset(get_args(ProcedureOperationalFailureCode))


def procedure_run_journal(instance: PlaybillInstance) -> LocalJournalBackend | None:
    """The Procedure-run journal, or ``None`` when no run was ever journaled.

    A read never creates the journal directory.
    """

    root: Path = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    return LocalJournalBackend(root) if root.is_dir() else None


def _stream(instance: PlaybillInstance) -> JournalStreamIdentityV1:
    from cruxible_core.service.procedures.procedure_runs import _stream as run_stream

    return run_stream(instance)


def final_status(payload: object) -> RunStatus:
    """The served run status an ``attempt_finalized`` payload records."""

    raw = payload.get("status") if isinstance(payload, Mapping) else None
    if raw == "succeeded":
        return "succeeded"
    if raw == "halted":
        return "halted"
    if raw in {"refused", "budget_exhausted"}:
        return "node_refused"
    if raw == "failed" and isinstance(payload, Mapping):
        if payload.get("failure_code") in _OPERATIONAL_FAILURES:
            return "operational_failed"
    return "internal_failed"


def run_row(instance: PlaybillInstance, locator: RunLocator) -> RunRow:
    """One located run as a row, reading its admission and (if any) final payloads."""

    bodies = instance.body_store()
    admission = parse_admission_payload(
        parse_journal_payload(bodies.read(locator.admission_payload_digest, access=_ACCESS))
    ).admission
    status: RunStatus = "running"
    if locator.final_payload_digest is not None:
        status = final_status(
            parse_journal_payload(bodies.read(locator.final_payload_digest, access=_ACCESS))
        )
    line = getattr(admission, "line_identity", None)
    started = parse_datetime(locator.admitted_at)
    assert started is not None
    return RunRow(
        run=locator.run_id,
        procedure=admission.procedure_identity.qualified,
        status=status,
        started_at=started,
        line=None if line is None else line.qualified,
        nodes_done=locator.nodes_done,
    )


def run_rows(
    instance: PlaybillInstance,
    *,
    limit: int,
    line: ArtifactIdentity | None = None,
    running_only: bool = False,
    after: RunLocatorKey | None = None,
) -> tuple[tuple[RunRow, ...], RunLocatorKey | None]:
    """One page of runs, newest admission first, and where it stopped.

    ``line`` keeps one Line's runs; ``running_only`` keeps runs still running.
    ``RunPageInvalidated`` refuses a key minted before the index was rebuilt.
    The key is ``None`` when the page is the last one.
    """

    journal = procedure_run_journal(instance)
    if journal is None:
        return (), None
    partition = None if line is None else procedure_line_partition(line)
    locators, more = journal.index.run_locators(
        _stream(instance),
        limit=limit,
        partition_id=partition,
        running_only=running_only,
        after=after,
    )
    rows = tuple(run_row(instance, locator) for locator in locators)
    return rows, (locators[-1].key if more and locators else None)


def run_counts(
    instance: PlaybillInstance, *, line: ArtifactIdentity | None = None
) -> tuple[int, int]:
    """(admitted runs, of which still running), of one Line when ``line`` is given."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return 0, 0
    partition = None if line is None else procedure_line_partition(line)
    return journal.index.run_counts(_stream(instance), partition_id=partition)


# -- one run, with live progress -------------------------------------------------------


def run_ids_with_prefix(instance: PlaybillInstance, prefix: str, *, limit: int) -> tuple[str, ...]:
    """Admitted run ids that start with ``prefix``; empty when no run was journaled."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return ()
    return journal.index.run_ids_with_prefix(_stream(instance), prefix, limit=limit)


def _graph(
    instance: PlaybillInstance, identity: ArtifactIdentity, coordinate: object
) -> tuple[dict[str, str], dict[str, dict[str, str]] | None, str | None]:
    """The run's Procedure at its bound coordinate: node kinds, edges, and its first node.

    Edges are ``None`` when the run's Procedure is not readable at its coordinate.
    """

    from cruxible_client.contracts.procedures.graph import analyze_procedure
    from cruxible_client.contracts.procedures.models import ProcedureDefinition

    bound = instance.resolve_accepted_coordinate(
        **coordinate.model_dump(mode="python", exclude={"tag"})  # type: ignore[attr-defined]
    )
    with instance.bind_accepted_projection(bound) as projection:
        procedure = projection.typed.source(identity.qualified)
    definition = getattr(procedure, "definition", None)
    nodes = tuple(getattr(definition, "nodes", ()))
    kinds = {str(node.node_id): str(node.kind) for node in nodes}
    first = str(nodes[0].node_id) if nodes else None
    edges: dict[str, dict[str, str]] | None = None
    try:
        if isinstance(definition, ProcedureDefinition):
            edges = analyze_procedure(definition).edges
    except Exception:  # noqa: BLE001 -- a graph the analyzer refuses has no known edges
        edges = None
    return kinds, edges, first


def _payload(instance: PlaybillInstance, digest: str) -> object:
    return parse_journal_payload(instance.body_store().read(digest, access=_ACCESS))


def _admission(instance: PlaybillInstance, run_id: str) -> object | None:
    """One journaled run's admission, or ``None`` when the journal holds no such run."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return None
    locators, _more = journal.index.run_locators(_stream(instance), limit=1, run_id=run_id)
    if not locators:
        return None
    return parse_admission_payload(
        _payload(instance, locators[0].admission_payload_digest)
    ).admission


def _arming_run(instance: PlaybillInstance, run_id: str, admission: object) -> str | None:
    """The run Line dispatch admitted for ``run_id``: itself, or a nested run's root.

    A nested run inherits its parent's Line and actor, but dispatch records
    only the root run it admitted. The root is reached through each
    ``parent_binding``, verified against the parent's own admission digest,
    never through a caller's ancestry claim. ``None`` when that chain does not
    verify, so nothing is attributed to an arm it cannot be traced to.
    """

    from cruxible_core.procedures.execution import (
        ProcedureRunAdmissionV2,
        ProcedureRunAdmissionV8,
    )

    current_id, current = run_id, admission
    seen = {run_id}
    while isinstance(current, ProcedureRunAdmissionV8):
        binding = current.parent_binding
        if binding.parent_run_id in seen:
            return None
        seen.add(binding.parent_run_id)
        parent = _admission(instance, binding.parent_run_id)
        if (
            not isinstance(parent, ProcedureRunAdmissionV2)
            or parent.admission_binding_digest != binding.parent_admission_digest
        ):
            return None
        current_id, current = binding.parent_run_id, parent
    return current_id


def _run_trigger(
    instance: PlaybillInstance,
    run_id: str,
    admission: object,
    viewer: OperationalViewer | None,
) -> GetRunTrigger | None:
    """The Line, occurrence and arm that admitted a Line run; ``None`` for a direct run.

    Who armed it is shown only to a reader who may see that arming credential.
    A nested run answers for the arm that admitted its root (``_arming_run``);
    one whose parent chain does not verify is withheld from all but an admin.
    """

    candidate = getattr(admission, "line_identity", None)
    if not isinstance(candidate, ArtifactIdentity):
        return None
    line = candidate.qualified
    occurrence = getattr(admission, "occurrence_id", None)
    arming = _arming_run(instance, run_id, admission)
    if arming is None:
        return GetRunTrigger(
            line=line,
            occurrence=occurrence,
            armed_by_withheld=viewer is None or not viewer.admin,
        )
    from cruxible_client.contracts.line_dispatch import LineArmPrincipal
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root

    fields: dict[str, object] = {}
    if dispatch_root(instance).exists():
        with LineDispatchStore(instance).locked() as conn:
            row = conn.execute(
                "SELECT s.payload FROM pending p JOIN sessions s ON s.session_id=p.session_id "
                "WHERE p.run_id=? LIMIT 1",
                (arming,),
            ).fetchone()
        if row is not None:
            data = json.loads(row[0])
            if data.get("arm_id"):
                fields["arm"] = str(data["arm_id"])
            by = data.get("armed_by")
            if isinstance(by, Mapping):
                principal = LineArmPrincipal.model_validate(by)
                fields["principal_kind"] = principal.kind
                if may_see_arming(viewer, principal):
                    fields["armed_by"] = principal.label
                else:
                    fields["armed_by_withheld"] = True
    return GetRunTrigger(line=line, occurrence=occurrence, **fields)  # type: ignore[arg-type]


def procedure_run_card(
    instance: PlaybillInstance,
    run_id: str,
    *,
    evaluation_time: datetime,
    render: Callable[[str, str | None], str],
    viewer: OperationalViewer | None = None,
) -> GetProcedureRunCard:
    """One run with its live progress, read from the journal index and a few payloads.

    A running run is never replayed: the index counts its finished nodes, and
    only the latest node and branch records are read to name the node that is
    current. A finished run reads its authoritative state for the receipt.
    Elapsed time is the read's ``evaluation_time`` minus the admission for a
    running run, and the run's measured wall clock once it finished. Every
    record of a run carries the run's evaluation instant (the deterministic
    executor clock), so per-node durations are not derivable and are absent.
    """

    journal = procedure_run_journal(instance)
    locators = (
        ()
        if journal is None
        else journal.index.run_locators(_stream(instance), limit=1, run_id=run_id)[0]
    )
    if not locators:
        raise ProcedureRunNotFoundForRead(run_id)
    assert journal is not None
    (locator,) = locators
    stream = _stream(instance)
    admission = parse_admission_payload(_payload(instance, locator.admission_payload_digest))
    bound = admission.admission
    started = parse_datetime(locator.admitted_at)
    assert started is not None
    kinds, edges, first = _graph(instance, bound.procedure_identity, bound.bound_coordinate)
    fired = journal.index.select(
        stream,
        run_id=run_id,
        event_kind="node_fired",
        descending=True,
        limit=OPERATIONAL_CARD_LIST_LIMIT,
    )
    nodes: list[GetRunNode] = []
    for stored in reversed(fired):
        payload = _payload(instance, stored.record.payload_digest)
        values = payload if isinstance(payload, Mapping) else {}
        nodes.append(
            GetRunNode(
                node=str(values.get("node_id", "?")),
                kind=None if values.get("kind") is None else str(values["kind"]),
                verdict=None if values.get("verdict") is None else str(values["verdict"]),
                sequence=stored.record.sequence,
            )
        )
    status: RunStatus = "running"
    current: GetRunCurrentNode | None = None
    receipt_digest: str | None = None
    terminal: str | None = None
    elapsed: int | None = None
    basis: Literal["read_time", "measured_wall_clock"] | None = None
    if locator.final_payload_digest is None:
        target = _current_node(instance, journal, stream, run_id, nodes, edges, first)
        if target is not None and target in kinds:
            latest = fired[0].record.recorded_at if fired else started
            current = GetRunCurrentNode(node=target, kind=kinds[target], started_at=latest)
        if evaluation_time >= started:
            elapsed = int((evaluation_time - started) / timedelta(microseconds=1))
            basis = "read_time"
    else:
        from cruxible_core.service.procedures.procedure_runs import (
            service_get_playbill_procedure_run,
        )

        state = service_get_playbill_procedure_run(instance, run_id=run_id)
        status = state.status
        receipt_digest = state.receipt_digest
        terminal = None if state.terminal is None else str(getattr(state.terminal, "code", ""))
        final = _payload(instance, locator.final_payload_digest)
        budget = final.get("budget") if isinstance(final, Mapping) else None
        observed = budget.get("observed") if isinstance(budget, Mapping) else None
        wall = observed.get("wall_clock_microseconds") if isinstance(observed, Mapping) else None
        if isinstance(wall, int) and wall >= 0:
            elapsed, basis = wall, "measured_wall_clock"
    line = getattr(bound, "line_identity", None)
    trigger = _run_trigger(instance, run_id, bound, viewer)
    # An armed run acts as its arming credential's label; that label is the
    # arming credential's to see, as on the Line card.
    actor = (
        None if trigger is not None and trigger.armed_by_withheld else bound.actor_context.actor_id
    )
    next_steps = [render(bound.procedure_identity.qualified, None)]
    if line is not None:
        next_steps.append(render(line.qualified, None))
    next_steps.append(render(f"ProcedureRun:{run_id}", "proof"))
    return GetProcedureRunCard(
        run=run_id,
        procedure=bound.procedure_identity.qualified,
        status=status,
        nodes_done=locator.nodes_done,
        nodes_total=len(kinds),
        current_node=current,
        started_at=started,
        elapsed_us=elapsed,
        elapsed_basis=basis,
        nodes=tuple(nodes),
        pending_inputs=(),
        triggered_by=trigger,
        actor=actor,
        receipt_digest=receipt_digest,
        terminal=terminal or None,
        next=tuple(next_steps),
    )


def run_arming_withheld(
    instance: PlaybillInstance, run_id: str, *, viewer: OperationalViewer | None
) -> bool:
    """Whether this run acts as an arming credential ``viewer`` may not see.

    The Line and run cards' rule (``OperationalViewer.may_see``): a runtime
    arming credential is shown only to an admin, that credential, or another
    credential bound to the same principal. A direct run has no arm; a nested
    run answers for its root's.
    """

    admission = _admission(instance, run_id)
    if admission is None:
        return False
    trigger = _run_trigger(instance, run_id, admission, viewer)
    return trigger is not None and bool(trigger.armed_by_withheld)


def procedure_run_status(
    instance: PlaybillInstance, run_id: str, *, viewer: OperationalViewer | None
) -> ProcedureRunState:
    """A run's served state, with its arming credential withheld as on the card.

    An armed run acts as its arming credential's principal, which the state's
    attribution and receipt both carry. For a reader who may not see that
    credential the attribution answers without its actor and the receipt as a
    withheld marker; ``receipt_digest`` still names the exact receipt.
    """

    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )

    state = ProcedureRunState.model_validate(
        service_get_playbill_procedure_run(instance, run_id=run_id).model_dump(mode="json")
    )
    if not run_arming_withheld(instance, run_id, viewer=viewer):
        return state
    attribution = state.attribution
    return state.model_copy(
        update={
            "attribution": (
                ProcedureRunAttributionWithheld.of(attribution)
                if isinstance(attribution, ProcedureRunAttribution)
                else attribution
            ),
            "receipt": None if state.receipt is None else ProcedureRunReceiptWithheld(),
        }
    )


def procedure_run_proof(
    instance: PlaybillInstance, run_id: str, *, viewer: OperationalViewer | None
) -> dict[str, object]:
    """A run's full served state, under ``procedure_run_status``'s withholding."""

    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )

    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    proof: dict[str, object] = state.model_dump(mode="json")
    if run_arming_withheld(instance, run_id, viewer=viewer):
        if state.attribution is not None:
            proof["attribution"] = ProcedureRunAttributionWithheld.of(state.attribution).model_dump(
                mode="json"
            )
        if state.receipt is not None:
            proof["receipt"] = ProcedureRunReceiptWithheld().model_dump(mode="json")
    return proof


def _current_node(
    instance: PlaybillInstance,
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    run_id: str,
    nodes: list[GetRunNode],
    edges: dict[str, dict[str, str]] | None,
    first: str | None,
) -> str | None:
    """The node a running run is on: the first node, else the edge its last node took."""

    if not nodes:
        return first
    if edges is None:
        return None
    last = nodes[-1]
    out = edges.get(last.node, {})
    target: str | None
    if "next" in out:
        target = out["next"]
    else:
        branch = journal.index.select(
            stream, run_id=run_id, event_kind="branch_evaluated", descending=True, limit=1
        )
        payload = _payload(instance, branch[0].record.payload_digest) if branch else None
        arm = payload.get("selected_arm") if isinstance(payload, Mapping) else None
        target = out.get(str(arm)) if arm is not None else None
    return None if target is None or target.startswith("$") else target


class ProcedureRunNotFoundForRead(Exception):
    """No run with this id was ever admitted on the instance."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


__all__ = [
    "ProcedureRunNotFoundForRead",
    "final_status",
    "procedure_run_card",
    "procedure_run_journal",
    "procedure_run_proof",
    "procedure_run_status",
    "run_arming_withheld",
    "run_counts",
    "run_ids_with_prefix",
    "run_row",
    "run_rows",
]
