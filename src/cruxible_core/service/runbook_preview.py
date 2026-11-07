"""Compose a Runbook's static preview from accepted definitions only (D-R36, D-R35).

The preview executes nothing and reads no state beyond definitions: no Claim,
Subject, Capture, run journal or trigger journal is opened, and no provider
is called. Every read is a typed definition source or a definition index seek
at the accepted head, so its work is linear in the Runbook's steps and the
definitions they reach (each step's Line, Procedure, Mandates, Queries and
the ClaimTypes and SubjectKinds behind them, and the Triggers and writers
those reach through the dependency index), and constant in every other
definition the instance holds.

Per step it says what the step CAN do: what it reads (including late reads,
D-R38), what it can write and create, which provider slots it calls and the
bindings behind them, what its Mandate scope settles and what would go to
a proposal (and so stop the run), and which capture_landing and
generation_accepted Triggers its writes can set off, inside the Runbook or
outside it. Across steps it reports order problems: a read of what only a
later step writes, and a read of what nothing writes (direct entry only).
It reports them and never reorders anything. It never predicts an outcome.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifact
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.procedures.graph import late_state_taps
from cruxible_client.contracts.procedures.line_specs import LineSpec, line_spec_digest
from cruxible_client.contracts.procedures.nodes import CallNode, CaptureEgressNode, ReadNode
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.runbooks import (
    Runbook,
    RunbookOrderProblem,
    RunbookPreview,
    RunbookPreviewAuthority,
    RunbookPreviewCall,
    RunbookPreviewCascade,
    RunbookPreviewRead,
    RunbookPreviewStep,
    RunbookPreviewWrite,
    RunbookSettleScope,
    runbook_digest,
)
from cruxible_client.contracts.subject_kinds import SubjectKind
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    ActionTarget,
    CaptureLandingSchedule,
    GenerationAcceptedSchedule,
    Trigger,
)

if TYPE_CHECKING:
    from cruxible_core.runtime.instance import PlaybillInstance


class DefinitionReader:
    """Counted, memoized reads of accepted definitions at one bound projection.

    ``work`` counts each distinct definition source read (by kind) and each
    index seek (by name): the preview's work, which the tests hold linear in
    the Runbook's steps and constant in unrelated definitions.
    """

    def __init__(self, typed: Any, work: Counter[str] | None = None) -> None:
        self._typed = typed
        self._sources: dict[str, Any] = {}
        self.work: Counter[str] = Counter() if work is None else work

    def source(self, identity: str) -> Any | None:
        if identity not in self._sources:
            self.work["source:" + identity.partition(":")[0]] += 1
            self._sources[identity] = self._typed.source(identity)
        return self._sources[identity]

    def _seek(self, name: str, sql: str, parameters: tuple[object, ...]) -> list[tuple[Any, ...]]:
        self.work["seek:" + name] += 1
        return list(self._typed.connection.execute(sql, parameters).fetchall())

    def mandates(self, line: LineSpec) -> tuple[ProcedureMandate, ...]:
        rows = self._seek(
            "mandates_by_line",
            "SELECT identity FROM procedure_mandates INDEXED BY procedure_mandates_by_line "
            "WHERE line_identity=? AND line_digest=? AND lifecycle='live' ORDER BY identity",
            (line.identity.qualified, line_spec_digest(line).tagged),
        )
        found = (self.source(str(row[0])) for row in rows)
        return tuple(item for item in found if isinstance(item, ProcedureMandate))

    def dependents(self, target: str, role: str, kind: str) -> tuple[str, ...]:
        """Sources of ``kind`` that name ``target`` in ``role``: one dependency-index seek."""

        rows = self._seek(
            "pins_by_target",
            "SELECT DISTINCT source_identity FROM pins INDEXED BY pins_by_target "
            "WHERE target_identity=? AND edge_kind='required_pin' AND pin_role=? "
            "AND source_identity>=? AND source_identity<? ORDER BY source_identity",
            (target, role, kind + ":", kind + ";"),
        )
        return tuple(str(row[0]) for row in rows)

    def lines_running(self, procedure: str) -> tuple[str, ...]:
        rows = self._seek(
            "lines_by_procedure",
            "SELECT identity FROM lines INDEXED BY lines_by_procedure "
            "WHERE procedure_identity=? AND lifecycle='live' ORDER BY identity",
            (procedure,),
        )
        return tuple(str(row[0]) for row in rows)

    def generation_triggers(self) -> tuple[Trigger, ...]:
        rows = self._seek(
            "triggers_by_schedule",
            "SELECT identity FROM triggers INDEXED BY triggers_by_schedule "
            "WHERE schedule_kind='generation_accepted' AND lifecycle='live' ORDER BY identity",
            (),
        )
        found = (self.source(str(row[0])) for row in rows)
        return tuple(item for item in found if isinstance(item, Trigger))


def _sorted(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(items), key=lambda item: item.encode("utf-8")))


def _target(trigger: Trigger) -> tuple[str, str]:
    if trigger.line is not None:
        return "line", trigger.line.qualified
    if trigger.runbook is not None:
        return "runbook", trigger.runbook.qualified
    return "action", str(trigger.action)


def _created_object_kinds(node: ClaimsNode) -> tuple[str, ...]:
    kinds = []
    for template in node.claims:
        value = template.value
        if isinstance(value, dict) and value.get("create") == "if_absent":
            kind = value.get("kind")
            if isinstance(kind, str):
                kinds.append(kind)
    return _sorted(kinds)


def _reads(reader: DefinitionReader, definition: Any) -> tuple[RunbookPreviewRead, ...]:
    late = late_state_taps(definition)
    reads = []
    for node in definition.nodes:
        if not isinstance(node, ReadNode):
            continue
        query_name: str | None = None
        kinds: set[str] = set()
        if node.query is not None:
            query_name = node.query.target.name
            query = reader.source(node.query.target.qualified)
            predicates: tuple[str, ...] = ()
            if isinstance(query, QueryDefinition):
                predicates = tuple(query.referenced_predicates)
                # The kinds it enters and walks to, even with no predicate read
                # (a Query projecting only subject_id reads its entry kind).
                kinds.update(query.subject_kinds)
        else:
            assert node.claim_type is not None
            predicates = (node.claim_type.target.name,)
        if node.subject_kind is not None:
            kinds.add(node.subject_kind)
        for predicate in predicates:
            claim_type = reader.source(f"ClaimType:{predicate}")
            if isinstance(claim_type, ClaimType):
                kinds.update(claim_type.allowed_subject_kinds)
        keys: set[str] = set()
        for kind in kinds:
            declared = reader.source(f"SubjectKind:{kind}")
            if isinstance(declared, SubjectKind):
                keys.update(declared.keys)
        reads.append(
            RunbookPreviewRead(
                node_id=node.node_id,
                query=query_name,
                claim_types=_sorted(predicates),
                subject_kinds=_sorted(kinds),
                key_claim_types=_sorted(keys),
                late=node.as_ in late,
            )
        )
    return tuple(reads)


def _writes(definition: Any) -> tuple[RunbookPreviewWrite, ...]:
    return tuple(
        RunbookPreviewWrite(
            node_id=node.node_id,
            subject_kind=node.subject.kind,
            creates_subjects=node.subject.create == "if_absent",
            claim_types=_sorted(template.claim_type.target.name for template in node.claims),
            creates_object_kinds=_created_object_kinds(node),
            judges_absence=node.expect is not None,
        )
        for node in definition.nodes
        if isinstance(node, ClaimsNode)
    )


def _calls(definition: Any, line: LineSpec) -> tuple[RunbookPreviewCall, ...]:
    calls = []
    for node in definition.nodes:
        if not isinstance(node, CallNode):
            continue
        slot = definition.slots.get(node.slot)
        binding = line.bindings.get(node.slot)
        calls.append(
            RunbookPreviewCall(
                node_id=node.node_id,
                slot=node.slot,
                signature_in=None if slot is None else f"{slot.in_.name}@{slot.in_.version}",
                signature_out=None if slot is None else f"{slot.out.name}@{slot.out.version}",
                effect=None if slot is None else slot.effect,
                build=None if binding is None else binding.build.target.qualified,
                operation=None if binding is None else binding.operation,
                captures=_sorted(ref.target.name for ref in node.captures.values()),
            )
        )
    return tuple(calls)


def _authority(
    line: LineSpec, mandates: tuple[ProcedureMandate, ...], writes: tuple[RunbookPreviewWrite, ...]
) -> RunbookPreviewAuthority:
    live = tuple(item for item in mandates if not item.suspended)
    settle = next((item for item in live if item.grants == "settle"), None)
    observe = line.max_authority == "observe"
    can_settle = settle is not None and line.max_authority == "settle"
    scope = (
        {item.claim_type.target.name: frozenset(item.change_kinds) for item in settle.scope}
        if can_settle and settle is not None
        else {}
    )
    proposes: set[str] = set()
    created: set[str] = set()
    for write in writes:
        needed = {"create", "revise", *(("retire",) if write.judges_absence else ())}
        for claim_type in write.claim_types:
            if not needed <= scope.get(claim_type, frozenset()):
                proposes.add(claim_type)
        if write.creates_subjects:
            created.add(write.subject_kind)
        created.update(write.creates_object_kinds)
    creation_scope = set(settle.subject_creation) if can_settle and settle is not None else set()
    conditional = (
        can_settle
        and settle is not None
        and settle.condition is not None
        and settle.condition.fallback == "propose"
    )
    proposes_creation = set() if observe else created - creation_scope
    if observe:
        proposes = set()
    return RunbookPreviewAuthority(
        max_authority=line.max_authority,
        mandates=_sorted(item.identity.qualified for item in live),
        grants=None if not live else ("settle" if settle is not None else "propose"),
        settles=tuple(
            RunbookSettleScope(claim_type=claim_type, changes=_sorted(changes))
            for claim_type, changes in sorted(scope.items())
        ),
        settles_creation=_sorted(creation_scope),
        conditional=conditional,
        proposes=_sorted(proposes),
        proposes_creation=_sorted(proposes_creation),
        may_stop_run=bool(proposes or proposes_creation or conditional),
    )


def _cascades(
    reader: DefinitionReader,
    *,
    captures: tuple[str, ...],
    writes: bool,
    runbook_lines: frozenset[str],
) -> tuple[RunbookPreviewCascade, ...]:
    found: dict[str, RunbookPreviewCascade] = {}
    for contract in captures:
        for identity in reader.dependents(
            f"CaptureContract:{contract}", "capture-contract", "Trigger"
        ):
            trigger = reader.source(identity)
            if not (
                isinstance(trigger, Trigger)
                and trigger.lifecycle.state == "live"
                and isinstance(trigger.schedule, CaptureLandingSchedule)
                and trigger.schedule.event.capture_contract_identity.name == contract
            ):
                continue
            kind, target = _target(trigger)
            found[identity] = RunbookPreviewCascade(
                trigger=identity,
                schedule="capture_landing",
                target_kind=kind,  # type: ignore[arg-type]
                target=target,
                capture_contract=contract,
                in_runbook=target in runbook_lines,
            )
    if writes:
        for trigger in reader.generation_triggers():
            if not isinstance(trigger.schedule, GenerationAcceptedSchedule):
                continue
            kind, target = _target(trigger)
            found[trigger.identity.qualified] = RunbookPreviewCascade(
                trigger=trigger.identity.qualified,
                schedule="generation_accepted",
                target_kind=kind,  # type: ignore[arg-type]
                target=target,
                in_runbook=target in runbook_lines,
            )
    return tuple(found[key] for key in sorted(found, key=lambda item: item.encode("utf-8")))


def _outside_writers(
    reader: DefinitionReader, claim_type: str, runbook_lines: frozenset[str]
) -> tuple[str, ...]:
    """Live Lines outside the Runbook whose Procedure can write ``claim_type``.

    Through the dependency index: the Procedures that name the ClaimType, then
    the live Lines that run each one that writes it -- O(those), never a scan.
    """

    writers: set[str] = set()
    for identity in reader.dependents(f"ClaimType:{claim_type}", "claim-type", "Procedure"):
        procedure = reader.source(identity)
        if not isinstance(procedure, ProcedureArtifact) or procedure.lifecycle.state != "live":
            continue
        if not any(claim_type in write.claim_types for write in _writes(procedure.definition)):
            continue
        writers.update(line for line in reader.lines_running(identity) if line not in runbook_lines)
    return _sorted(writers)


def compose_runbook_preview(
    reader: DefinitionReader, runbook: Runbook, *, generation_root: str
) -> RunbookPreview:
    runbook_lines = frozenset(identity.qualified for identity in runbook.lines)
    steps: list[RunbookPreviewStep] = []
    read_types: list[tuple[int, tuple[str, ...], frozenset[str]]] = []
    written_by: dict[str, list[int]] = {}
    for index, target in enumerate(runbook.steps, start=1):
        if isinstance(target, ActionTarget):
            spec = INTERNAL_ACTIONS.get(target.action)
            steps.append(
                RunbookPreviewStep(
                    step=index,
                    target=target,
                    unavailable=spec is None,
                    action_effect=None if spec is None else spec.effect,
                )
            )
            continue
        line = reader.source(target.line.target.qualified)
        procedure = (
            reader.source(line.procedure.target.qualified) if isinstance(line, LineSpec) else None
        )
        if (
            not isinstance(line, LineSpec)
            or line.lifecycle.state != "live"
            or not isinstance(procedure, ProcedureArtifact)
        ):
            steps.append(RunbookPreviewStep(step=index, target=target, unavailable=True))
            continue
        definition = procedure.definition
        reads = _reads(reader, definition)
        writes = _writes(definition)
        calls = _calls(definition, line)
        authority = _authority(line, reader.mandates(line), writes)
        captures = _sorted(
            [contract for call in calls for contract in call.captures]
            + [
                node.capture_contract.target.name
                for node in definition.nodes
                if isinstance(node, CaptureEgressNode)
            ]
        )
        can_write = bool(writes) and line.max_authority != "observe"
        steps.append(
            RunbookPreviewStep(
                step=index,
                target=target,
                procedure=procedure.identity.qualified,
                reads=reads,
                writes=writes,
                calls=calls,
                authority=authority,
                cascades=_cascades(
                    reader, captures=captures, writes=can_write, runbook_lines=runbook_lines
                ),
            )
        )
        read_types.append(
            (
                index,
                _sorted(t for read in reads for t in read.claim_types),
                frozenset(t for read in reads for t in read.key_claim_types),
            )
        )
        if can_write:
            for write in writes:
                # Creating a Subject writes its key Claims: they are this step's writes too.
                created = (write.subject_kind,) if write.creates_subjects else ()
                for kind in (*created, *write.creates_object_kinds):
                    declared = reader.source(f"SubjectKind:{kind}")
                    if isinstance(declared, SubjectKind):
                        for key in declared.keys:
                            written_by.setdefault(key, []).append(index)
                for claim_type in write.claim_types:
                    written_by.setdefault(claim_type, []).append(index)
    problems: list[RunbookOrderProblem] = []
    for index, claim_types, keys in read_types:
        for claim_type in claim_types:
            writers = written_by.get(claim_type, [])
            if any(step <= index for step in writers):
                continue  # an earlier step (or the step itself) writes it
            later = tuple(dict.fromkeys(step for step in writers if step > index))
            if not later and claim_type in keys:
                # A key ClaimType is written by whatever creates its Subjects,
                # which the dependency index does not name: never "unwritten".
                continue
            outside = _outside_writers(reader, claim_type, runbook_lines)
            if later:
                problems.append(
                    RunbookOrderProblem(
                        kind="reads_later_write",
                        step=index,
                        claim_type=claim_type,
                        written_by_steps=later,
                        outside_writers=outside,
                    )
                )
            elif not outside:
                problems.append(
                    RunbookOrderProblem(kind="reads_unwritten", step=index, claim_type=claim_type)
                )
    return RunbookPreview(
        runbook=runbook.identity.name,
        runbook_artifact_digest=runbook_digest(runbook).tagged,
        description=runbook.description,
        generation_root=generation_root,
        steps=tuple(steps),
        order_problems=tuple(problems),
    )


def service_preview_runbook(
    instance: PlaybillInstance, *, name: str, work: Counter[str] | None = None
) -> RunbookPreview:
    """The Runbook's static preview at the accepted head (``runbook preview``)."""

    from cruxible_core.service.runbooks import RunbookNotFoundError

    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        reader = DefinitionReader(projection.typed, work)
        runbook = reader.source(f"Runbook:{name}")
        if not isinstance(runbook, Runbook) or runbook.lifecycle.state != "live":
            raise RunbookNotFoundError(
                f"No live accepted Runbook named {name!r}; author one with a runbook input."
            )
        return compose_runbook_preview(reader, runbook, generation_root=coordinate.generation_root)


__all__ = ["DefinitionReader", "compose_runbook_preview", "service_preview_runbook"]
