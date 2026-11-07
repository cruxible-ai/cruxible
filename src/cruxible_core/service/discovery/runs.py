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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from pydantic import TypeAdapter

from cruxible_client.contracts import ProcedureRunState
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.operational_reads import (
    OPERATIONAL_CARD_LIST_LIMIT,
    RUN_CARD_LIST_MEMBERS_MAX,
    GetProcedureRunCard,
    GetRunCurrentNode,
    GetRunListMember,
    GetRunNode,
    GetRunOutcome,
    GetRunTrigger,
    RunRow,
    RunStatus,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureOperationalFailureCode,
    ProcedureRunAttribution,
    ProcedureRunAttributionWithheld,
    ProcedureRunReceiptWithheld,
    ProcedureSourceObservation,
    ProcedureTerminal,
    ProcedureTerminalEgress,
)
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.journal_index import RunLocator, RunLocatorKey
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.exhaust.records import JournalStreamIdentityV1
from cruxible_core.procedures.execution import (
    ProcedureRunAdmission,
    parse_admission_payload,
    procedure_line_partition,
)
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


def procedure_run_stream(instance: PlaybillInstance) -> JournalStreamIdentityV1:
    """The Procedure-run journal stream of this instance."""

    from cruxible_core.service.procedures.procedure_runs import _stream as run_stream

    return run_stream(instance)


def final_status(payload: object) -> RunStatus:
    """The served run status an ``attempt_finalized`` payload records."""

    if isinstance(payload, UnavailablePayload):
        return "operational_failed"
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


def final_elapsed_us(payload: object) -> int | None:
    """The observed wall time, in microseconds, an ``attempt_finalized`` payload records.

    ``None`` when the payload is lost or records none.
    """

    budget = payload.get("budget") if isinstance(payload, Mapping) else None
    observed = budget.get("observed") if isinstance(budget, Mapping) else None
    wall = observed.get("wall_clock_microseconds") if isinstance(observed, Mapping) else None
    return wall if isinstance(wall, int) and not isinstance(wall, bool) and wall >= 0 else None


def run_identity(
    instance: PlaybillInstance, admission_digest: str
) -> tuple[str, str | None] | None:
    """A run's (Procedure, Line) from its ``admission_bound`` payload; ``None`` when it is lost."""

    payload = read_payload(instance.body_store(), admission_digest, access=_ACCESS)
    if isinstance(payload, UnavailablePayload):
        return None
    admission = parse_admission_payload(payload).admission
    line = admission.line_identity
    return admission.procedure_identity.qualified, None if line is None else line.qualified


def run_payload(instance: PlaybillInstance, digest: str) -> object:
    """One run record's payload, or ``UnavailablePayload`` when it is lost."""

    return read_payload(instance.body_store(), digest, access=_ACCESS)


def run_row(instance: PlaybillInstance, locator: RunLocator) -> RunRow:
    """One located run as a row, reading its admission and (if any) final payloads."""

    started = parse_datetime(locator.admitted_at)
    assert started is not None
    identity = run_identity(instance, locator.admission_payload_digest)
    if identity is None:
        return RunRow(
            run=locator.run_id,
            procedure="unavailable",
            status="operational_failed",
            started_at=started,
            nodes_done=locator.nodes_done,
        )
    status: RunStatus = "running"
    if locator.final_payload_digest is not None:
        status = final_status(run_payload(instance, locator.final_payload_digest))
    return RunRow(
        run=locator.run_id,
        procedure=identity[0],
        status=status,
        started_at=started,
        line=identity[1],
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
        procedure_run_stream(instance),
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
    return journal.index.run_counts(procedure_run_stream(instance), partition_id=partition)


# -- one run, with live progress -------------------------------------------------------


def run_ids_with_prefix(instance: PlaybillInstance, prefix: str, *, limit: int) -> tuple[str, ...]:
    """Admitted run ids that start with ``prefix``; empty when no run was journaled."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return ()
    return journal.index.run_ids_with_prefix(procedure_run_stream(instance), prefix, limit=limit)


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


def _admission(instance: PlaybillInstance, run_id: str) -> ProcedureRunAdmission | None:
    """One journaled run's admission, or ``None`` when the journal holds no such run."""

    journal = procedure_run_journal(instance)
    if journal is None:
        return None
    locators, _more = journal.index.run_locators(
        procedure_run_stream(instance), limit=1, run_id=run_id
    )
    if not locators:
        return None
    payload = run_payload(instance, locators[0].admission_payload_digest)
    return (
        None
        if isinstance(payload, UnavailablePayload)
        else parse_admission_payload(payload).admission
    )


def _arming_run(
    instance: PlaybillInstance, run_id: str, admission: ProcedureRunAdmission
) -> str | None:
    """The run Line dispatch admitted for ``run_id``: itself, or a nested run's root.

    A nested run inherits its parent's Line and actor, but dispatch records
    only the root run it admitted. The root is reached through each
    ``parent_binding``, verified against the parent's own admission digest,
    never through a caller's ancestry claim. ``None`` when that chain does not
    verify, so nothing is attributed to an arm it cannot be traced to.
    """

    current_id, current = run_id, admission
    seen = {run_id}
    while (binding := current.parent_binding) is not None:
        if binding.parent_run_id in seen:
            return None
        seen.add(binding.parent_run_id)
        parent = _admission(instance, binding.parent_run_id)
        if parent is None or parent.admission_binding_digest != binding.parent_admission_digest:
            return None
        current_id, current = binding.parent_run_id, parent
    return current_id


def _run_trigger(
    instance: PlaybillInstance,
    run_id: str,
    admission: ProcedureRunAdmission,
    viewer: OperationalViewer | None,
) -> GetRunTrigger | None:
    """The Line, occurrence and arm that admitted a Line run; ``None`` for a direct run.

    Who armed it is shown only to a reader who may see that arming credential.
    A nested run answers for the arm that admitted its root (``_arming_run``);
    one whose parent chain does not verify is withheld from all but an admin.
    """

    candidate = admission.line_identity
    if candidate is None:
        return None
    line = candidate.qualified
    occurrence = admission.occurrence_id
    arming = _arming_run(instance, run_id, admission)
    if arming is None:
        return GetRunTrigger(
            line=line,
            occurrence=occurrence,
            enabled_by_withheld=viewer is None or not viewer.admin,
        )
    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
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
                fields["enablement"] = str(data["arm_id"])
            by = data.get("armed_by")
            if isinstance(by, Mapping):
                principal = LineEnablementPrincipal.model_validate(by)
                fields["principal_kind"] = principal.kind
                if may_see_arming(viewer, principal):
                    fields["enabled_by"] = principal.label
                else:
                    fields["enabled_by_withheld"] = True
    return GetRunTrigger(line=line, occurrence=occurrence, **fields)  # type: ignore[arg-type]


def procedure_run_card(
    instance: PlaybillInstance,
    run_id: str,
    *,
    evaluation_time: datetime,
    render: Callable[[str, str | None], str],
    viewer: OperationalViewer | None = None,
    access: BodyAccessContext | None = None,
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
        else journal.index.run_locators(procedure_run_stream(instance), limit=1, run_id=run_id)[0]
    )
    if not locators:
        raise ProcedureRunNotFoundForRead(run_id)
    assert journal is not None
    (locator,) = locators
    stream = procedure_run_stream(instance)
    admission_payload = run_payload(instance, locator.admission_payload_digest)
    if isinstance(admission_payload, UnavailablePayload):
        from cruxible_core.service.procedures.procedure_runs import (
            service_get_playbill_procedure_run,
        )

        state = service_get_playbill_procedure_run(instance, run_id=run_id)
        started = parse_datetime(locator.admitted_at)
        assert started is not None
        return GetProcedureRunCard(
            run=run_id,
            procedure="unavailable",
            status="operational_failed",
            nodes_done=locator.nodes_done,
            nodes_total=0,
            started_at=started,
            terminal=state.terminal,
        )
    admission = parse_admission_payload(admission_payload)
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
    from cruxible_core.service.procedures.procedure_runs import run_node

    nodes: list[GetRunNode] = [
        run_node(
            run_payload(instance, stored.record.payload_digest), sequence=stored.record.sequence
        )
        for stored in reversed(fired)
    ]
    members, truncated_nodes = _list_members(instance, journal, stream, run_id, nodes)
    status: RunStatus = "running"
    current: GetRunCurrentNode | None = None
    receipt_digest: str | None = None
    terminal: ProcedureTerminal | None = None
    result: object | None = None
    outcomes: tuple[GetRunOutcome, ...] = ()
    egress: tuple[ProcedureTerminalEgress, ...] = ()
    elapsed: int | None = None
    basis: Literal["read_time", "measured_wall_clock"] | None = None
    completeness = None
    withheld: BodyRestricted | None = None
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
        terminal = state.terminal
        result = state.result
        completeness = state.completeness
        outcomes = tuple(
            GetRunOutcome.model_validate(item.model_dump(mode="json")) for item in state.outcomes
        )
        egress = state.terminal_egress
        withheld = run_values_withheld(
            instance, run_id, access, source_observations=state.source_observations
        )
        wall = final_elapsed_us(run_payload(instance, locator.final_payload_digest))
        if wall is not None:
            elapsed, basis = wall, "measured_wall_clock"
    if locator.final_payload_digest is None:
        # A running run: what its fired nodes derive from, as journaled so far.
        withheld = run_values_withheld(instance, run_id, access)
    nodes = _with_members(nodes, members, truncated_nodes, completeness)
    if withheld is not None:
        # C14: values extracted from restricted Captures stay withheld, while
        # it runs and after: node summaries, the result, refusal details.
        result = None
        nodes = [node.model_copy(update={"summary": None}) for node in nodes]
        if terminal is not None:
            terminal = _TERMINAL.validate_python(
                withheld_terminal(terminal.model_dump(mode="json"))
            )
    line = getattr(bound, "line_identity", None)
    trigger = _run_trigger(instance, run_id, bound, viewer)
    # An armed run acts as its arming credential's label; that label is the
    # arming credential's to see, as on the Line card.
    actor = (
        None
        if trigger is not None and trigger.enabled_by_withheld
        else bound.actor_context.actor_id
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
        result=result,
        outcomes=outcomes,
        terminal=terminal,
        terminal_egress=egress,
        completeness=completeness,
        withheld=withheld,
        next=tuple(next_steps),
    )


def _list_member_field(payload: Mapping[str, object]) -> tuple[str, int] | None:
    """``(field, index)`` when ``payload`` names a list member, else ``None``.

    A list member's record names its field ``<field>/<index>``; a whole
    (scalar) capture field has none, so it never answers here.
    """

    field, slash, index = str(payload.get("field", "")).rpartition("/")
    if not slash or not index.isdigit():
        return None
    return field, int(index)


def _list_members(
    instance: PlaybillInstance,
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    run_id: str,
    nodes: Sequence[GetRunNode],
) -> tuple[dict[str, list[GetRunListMember]], frozenset[str]]:
    """Each call node's list members, from the run's ``source_acquisition`` records.

    One journal-index range of this run's acquisition records, bounded by the
    card's member limit; one payload read per record on it. A list member's
    record names its field ``<field>/<index>``; a whole capture field has
    none.

    The second value is every displayed call with list members past that
    range, found with one bounded indexed existence probe (``limit=1``) per
    displayed node whose own firing sequence falls after the range's last
    served record -- never a scan of all records, and never a query at all
    for a node fully inside it. Each probe's range is exactly that node's own
    span: after the previous *fired* node's firing sequence -- the run's
    actual predecessor, read once up front even when it is outside the
    card's displayed window, never the displayed list's own first entry --
    through this node's own (nodes run one at a time, so no other node's
    records fall in between). A probe that lands on a record checks that one
    record's own field: a scalar (whole-capture) acquisition answers no list
    member and does not flag the call, so a probe whose one matched record
    happens to be scalar while a later list member of the same call also
    went unserved can still read as complete; the bound stays one query.
    """

    stored = journal.index.select(
        stream,
        run_id=run_id,
        event_kind="source_acquisition",
        limit=RUN_CARD_LIST_MEMBERS_MAX + 1,
    )
    found: dict[str, list[GetRunListMember]] = {}
    for item in stored[:RUN_CARD_LIST_MEMBERS_MAX]:
        payload = run_payload(instance, item.record.payload_digest)
        if not isinstance(payload, Mapping):
            continue
        member = _list_member_field(payload)
        if member is None:
            continue
        field, index = member
        decision = payload.get("decision")
        result = payload.get("result")
        decision = decision if isinstance(decision, Mapping) else {}
        result = result if isinstance(result, Mapping) else {}
        selected = list(decision.get("selected_capture_digests") or ())
        considered = list(decision.get("considered_capture_digests") or ())
        reasons = list(decision.get("reason_codes") or ())
        status = decision.get("disposition")
        if status not in ("selected", "omitted", "defaulted", "refused"):
            continue
        reason = reasons[0] if reasons else result.get("reason_code")
        request = payload.get("member_request_digest")
        found.setdefault(str(payload.get("node_id", "?")), []).append(
            GetRunListMember(
                field=field,
                index=index,
                capture=(selected or considered or [None])[0],
                request=request if isinstance(request, str) else None,
                status=status,
                reason=reason if isinstance(reason, str) else None,
            )
        )
    truncated_nodes: set[str] = set()
    if len(stored) > RUN_CARD_LIST_MEMBERS_MAX and nodes:
        served_through = stored[RUN_CARD_LIST_MEMBERS_MAX - 1].record.sequence
        # The displayed list's own first entry is not necessarily the run's
        # first fired node: the card shows only the latest ones. Anchor the
        # first interval on the actual preceding node_fired record (one
        # lookup, done once), so an earlier, undisplayed call's own overflow
        # is never attributed to the first displayed node.
        previous_sequence = 0
        preceding = journal.index.select(
            stream,
            run_id=run_id,
            event_kind="node_fired",
            last_sequence=nodes[0].sequence - 1,
            descending=True,
            limit=1,
        )
        if preceding:
            previous_sequence = preceding[0].record.sequence
        for node in nodes:
            lower = max(served_through, previous_sequence) + 1
            if lower <= node.sequence:
                overflow = journal.index.select(
                    stream,
                    run_id=run_id,
                    event_kind="source_acquisition",
                    first_sequence=lower,
                    last_sequence=node.sequence,
                    limit=1,
                )
                if overflow:
                    payload = run_payload(instance, overflow[0].record.payload_digest)
                    if isinstance(payload, Mapping) and _list_member_field(payload) is not None:
                        truncated_nodes.add(node.node)
            previous_sequence = node.sequence
    return found, frozenset(truncated_nodes)


def _with_members(
    nodes: list[GetRunNode],
    members: Mapping[str, list[GetRunListMember]],
    truncated_nodes: frozenset[str],
    completeness: object,
) -> list[GetRunNode]:
    """Attach each call's list members and the list fields it ended incomplete.

    A list is incomplete when a member was dropped (``omitted``), or when the
    finished run's completeness records the call's own ``incomplete_input``
    (the provider's ``incomplete_captures`` word): naming fields when it does,
    else every list the call returned. A named field stays incomplete
    whatever other lists of the same call are visible on the page (one list
    can be fully served while a sibling the reason names is not). Both are
    derived from the run's own record independently of whether any member of
    this call made it onto the member page: an empty list the provider still
    declared incomplete carries what the run recorded rather than being
    silently dropped.

    ``members_truncated`` names only the calls in ``truncated_nodes`` -- each
    one a call the page's range cut off -- never every node: an extract step
    or any other call with no list members of its own never gets it, and a
    call whose own members were all served (even one that exhausted the
    page) does not either.
    """

    flagged: dict[str, set[str] | None] = {}
    for reason in getattr(completeness, "reasons", ()) or ():
        if reason.kind != "incomplete_input":
            continue
        named = {name.partition(".")[2] for name in reason.inputs if "." in name}
        held = flagged.get(reason.node, set())
        flagged[reason.node] = None if not named or held is None else held | named
    out = []
    for node in nodes:
        listed = members.get(node.node, [])
        fields = {item.field for item in listed}
        omitted = {item.field for item in listed if item.status == "omitted"}
        said = flagged.get(node.node, set())
        # A named field stays incomplete regardless of what else is visible;
        # only the unnamed case ("every list the call returned") needs the
        # page's own field names, since there is nothing else to name it by.
        declared = fields if said is None else said
        incomplete = omitted | declared
        node_truncated = node.node in truncated_nodes
        if not listed and not incomplete and not node_truncated:
            # Nothing recorded against this call: leave it untouched.
            out.append(node)
            continue
        out.append(
            node.model_copy(
                update={
                    "members": tuple(sorted(listed, key=lambda item: (item.field, item.index))),
                    "members_truncated": node_truncated,
                    "incomplete_captures": tuple(sorted(incomplete)),
                }
            )
        )
    return out


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
    return trigger is not None and bool(trigger.enabled_by_withheld)


def run_consumed_captures(
    instance: PlaybillInstance, run_id: str | None
) -> tuple[tuple[str, ...], bool]:
    """The Captures a run (with its children) consumed, as its journal records them.

    Read from the run's latest ``node_fired`` and its final record, which
    carry the cumulative, versioned set (O(1) records). The second value is
    False -- the caller fails closed -- when the run is not in the journal,
    or a record is unreadable or carries no versioned consumption (written
    before C14: no legacy reader reconstructs it, D-R25 R4). A run admitted
    with no node fired yet has consumed nothing.
    """

    from cruxible_core.procedures.execution import CONSUMPTION_VERSION

    if run_id is None:
        return (), True
    journal = procedure_run_journal(instance)
    if journal is None:
        return (), False
    stream = procedure_run_stream(instance)
    locators = journal.index.run_locators(stream, limit=1, run_id=run_id)[0]
    if not locators:
        return (), False
    digests: list[str] = [
        stored.record.payload_digest
        for stored in journal.index.select(
            stream, run_id=run_id, event_kind="node_fired", descending=True, limit=1
        )
    ]
    if locators[0].final_payload_digest is not None:
        digests.append(locators[0].final_payload_digest)
    found: set[str] = set()
    known = True
    for digest in digests:
        payload = run_payload(instance, digest)
        consumption = payload.get("consumption") if isinstance(payload, Mapping) else None
        captures = consumption.get("captures") if isinstance(consumption, Mapping) else None
        if (
            not isinstance(consumption, Mapping)
            or consumption.get("v") != CONSUMPTION_VERSION
            or not isinstance(captures, list)
        ):
            known = False
            continue
        found.update(str(item) for item in captures)
    return tuple(sorted(found)), known


def run_values_withheld(
    instance: PlaybillInstance,
    run_id: str | None,
    access: BodyAccessContext | None,
    *,
    source_observations: Iterable[ProcedureSourceObservation] = (),
    consumed: tuple[tuple[str, ...], bool] | None = None,
) -> BodyRestricted | None:
    """Whether a run's values are withheld: it consumed Captures of a restricted kind (C14).

    A run's output, findings, node summaries and refusal details carry values
    extracted from the Captures it consumed -- a provider's, a trigger-bound
    or parameter Capture, a child run's -- so they inherit their restriction.
    The consumed set is the run's journaled provenance; an unreadable record
    or an unresolvable kind withholds (fail closed).
    """

    reader = access or BodyAccessContext(principal_id="playbill-run-read")
    if reader.can_read_restricted:
        return None
    captures, known = consumed if consumed is not None else run_consumed_captures(instance, run_id)
    digests = tuple(
        sorted(
            {
                *captures,
                *(item.capture_digest for item in source_observations if item.capture_digest),
            }
        )
    )
    if not known:
        from cruxible_core.service.evidence.body_access import capture_access_resolver

        notice = capture_access_resolver(instance).withheld(digests, reader)[1]
        return notice or BodyRestricted(provenance_unavailable=True)
    if not digests:
        return None
    from cruxible_core.service.evidence.body_access import capture_access_resolver

    return capture_access_resolver(instance).withheld(digests, reader)[1]


_TERMINAL: TypeAdapter[ProcedureTerminal] = TypeAdapter(ProcedureTerminal)
#: The marker that replaces a withheld refusal's details.
_WITHHELD_DETAILS = {"withheld": "body_restricted"}
#: Terminal kinds whose message or details may quote what a run read.
_DETAILED_TERMINALS = frozenset(
    {
        "playbill-procedure-node-refusal-v1",
        "playbill-procedure-admission-refusal-v1",
        "playbill-procedure-operational-failure-v1",
        "playbill-procedure-internal-failure-v1",
    }
)


def withheld_terminal(terminal: object) -> object:
    """A served terminal without what it quotes: classification, code and node stay.

    A refusal's ``message`` and ``details`` (an extract header's lexeme, a
    guard's operands, a provider's message) are replaced by the withheld marker.
    """

    if not isinstance(terminal, Mapping) or terminal.get("tag") not in _DETAILED_TERMINALS:
        return terminal
    result = dict(terminal)
    if "message" in result:
        result["message"] = f"{terminal.get('code', 'refused')}: details withheld (body_restricted)"
    if "details" in result:
        result["details"] = dict(_WITHHELD_DETAILS)
    cause = result.get("cause")
    if isinstance(cause, Mapping):
        result["cause"] = {**cause, "message": ""}
    return result


def _withheld_dump(dump: dict[str, object], notice: BodyRestricted) -> dict[str, object]:
    receipt = dump.get("receipt")
    if isinstance(receipt, Mapping) and "terminal" in receipt:
        dump["receipt"] = {**receipt, "terminal": withheld_terminal(receipt["terminal"])}
    nodes = dump.get("nodes")
    dump.update(
        result=None,
        findings=[],
        terminal=withheld_terminal(dump.get("terminal")),
        nodes=[
            {key: value for key, value in node.items() if key != "summary"}
            for node in (nodes if isinstance(nodes, list) else [])
            if isinstance(node, Mapping)
        ],
        withheld=notice.model_dump(mode="json"),
    )
    return dump


def withhold_run_values(
    state: ProcedureRunState, notice: BodyRestricted | None
) -> ProcedureRunState:
    """The run's state without its restricted-derived values (C14).

    Output, findings, node summaries and the refusal details of the terminal
    (and of the receipt's terminal) go; statuses, classifications, codes,
    counts, digests and journal locators stay.
    """

    if notice is None:
        return state
    return ProcedureRunState.model_validate(_withheld_dump(state.model_dump(mode="json"), notice))


def procedure_run_status(
    instance: PlaybillInstance,
    run_id: str,
    *,
    viewer: OperationalViewer | None,
    access: BodyAccessContext | None = None,
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
    state = withhold_run_values(
        state,
        run_values_withheld(
            instance, run_id, access, source_observations=state.source_observations
        ),
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
    instance: PlaybillInstance,
    run_id: str,
    *,
    viewer: OperationalViewer | None,
    access: BodyAccessContext | None = None,
) -> dict[str, object]:
    """A run's full served state, under ``procedure_run_status``'s withholding."""

    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )

    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    proof: dict[str, object] = state.model_dump(mode="json")
    notice = run_values_withheld(
        instance, run_id, access, source_observations=state.source_observations
    )
    if notice is not None:
        proof = _withheld_dump(proof, notice)
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
        payload = run_payload(instance, branch[0].record.payload_digest) if branch else None
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
