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

Per step it also lists what the run takes from outside accepted state: the
workspace files and globs it reads, the fetches it makes and the Capture a
Trigger hands it, each with its location (resolved from the Line's
parameters), the parameter and its authored description, and the
CaptureContract's format and limits. The one read beyond definitions is
whether each workspace file is there, by the daemon reader's own rules: a
no-follow stat per path component and a no-follow open of the file closed
unread (read access), never a listing, never a byte of the file.
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
    RunbookPreviewInput,
    RunbookPreviewInputLimits,
    RunbookPreviewRead,
    RunbookPreviewStep,
    RunbookPreviewWrite,
    RunbookSettleChange,
    RunbookSettleScope,
    runbook_digest,
)
from cruxible_client.contracts.shapes import Contract
from cruxible_client.contracts.subject_kinds import SubjectKind
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    ActionTarget,
    CaptureLandingSchedule,
    GenerationAcceptedSchedule,
    Trigger,
)
from cruxible_core.procedures.declared_inputs import (
    DeclaredInput,
    declared_inputs,
    parameter_description,
)

if TYPE_CHECKING:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
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


def _presence(
    reader: WorkspaceFileReader | None,
    declared: DeclaredInput,
    *,
    max_bytes: int | None,
    work: Counter[str],
) -> tuple[str | None, str | None]:
    """``(state, refused path class)`` for one workspace-file input; none for other kinds.

    The one read beyond definitions (approved as consistent with D-R36): a
    no-follow stat per path component and an unread open of the file through
    the daemon's reader, counted in ``work``; never a listing, never a byte of
    the file.
    """

    from cruxible_core.documents.workspace_file import WorkspaceFileReadRefused

    if declared.kind != "workspace_file" or declared.location is None:
        return None, None
    if reader is None:
        return "no_workspace", None
    work["stat:workspace_file"] += 1
    try:
        state, path_class = reader.presence(
            declared.location, workspace=declared.workspace, max_bytes=max_bytes
        )
    except WorkspaceFileReadRefused as exc:
        if exc.path_class == "binding":
            return "no_workspace", None
        return "refused", exc.path_class
    return state, path_class


def _inputs(
    reader: DefinitionReader,
    definition: Any,
    line: LineSpec,
    workspace_reader: WorkspaceFileReader | None,
) -> tuple[RunbookPreviewInput, ...]:
    declared = declared_inputs(definition, line)
    if not declared:
        return ()
    contract: Contract | None = None
    if definition.parameter_contract is not None:
        found = reader.source(definition.parameter_contract.target.qualified)
        contract = found if isinstance(found, Contract) else None
    inputs = []
    for item in declared:
        capture = (
            None
            if item.capture_contract is None
            else reader.source(f"CaptureContract:{item.capture_contract}")
        )
        budget = getattr(capture, "selection_budget", None)
        decoding = getattr(capture, "decoding", None)
        max_bytes = None if budget is None else budget.max_bytes
        state, refused = _presence(workspace_reader, item, max_bytes=max_bytes, work=reader.work)
        inputs.append(
            RunbookPreviewInput(
                node_id=item.node_id,
                kind=item.kind,
                location=item.location,
                parameter=item.parameter,
                description=parameter_description(contract, item.parameter),
                workspace=item.workspace,
                capture_contract=item.capture_contract,
                format=None if decoding is None else decoding.format,
                limits=RunbookPreviewInputLimits(
                    max_bytes=max_bytes,
                    decoded_bytes=None if decoding is None else decoding.limits.decoded_bytes,
                    depth=None if decoding is None else decoding.limits.depth,
                    records=None if decoding is None else decoding.limits.records,
                    max_files=item.max_files,
                ),
                origins=item.origins,
                state=state,  # type: ignore[arg-type]
                refused=refused,  # type: ignore[arg-type]
            )
        )
    return tuple(inputs)


_VALUE_BASES: frozenset[str] = frozenset({"extracted", "measured", "derived"})


def _record_reference(value: object) -> bool:
    """A value read straight off the extracted record: ``$item.fields.*`` / ``$item.key.*``."""

    if isinstance(value, str):
        return value.startswith(("$item.fields.", "$item.key."))
    if isinstance(value, dict) and set(value) <= {"kind", "key", "create"}:
        key = value.get("key")
        return isinstance(key, dict) and bool(key) and all(map(_record_reference, key.values()))
    return False


def _value_bases(template: Any) -> frozenset[str]:
    """The evidence bases a template's value can carry, as far as definitions prove.

    A derivation is ``derived``. A value read straight off the extract's record
    is ``extracted``. Anything else may be any value basis: the preview never
    claims more than the definitions say.
    """

    if template.role == "derivation" or template.inputs:
        return frozenset({"derived"})
    if _record_reference(template.value):
        return frozenset({"extracted"})
    return _VALUE_BASES


def _needed_changes(node: ClaimsNode) -> dict[str, set[tuple[str, str | None]]]:
    """Per ClaimType, every (change kind, basis) the node can produce."""

    needed: dict[str, set[tuple[str, str | None]]] = {}
    for template in node.claims:
        name = template.claim_type.target.name
        bases = _value_bases(template)
        changes = needed.setdefault(name, set())
        for change in ("create", "revise"):
            changes.update(
                (change, basis) if bases != _VALUE_BASES else (change, None) for basis in bases
            )
        if node.expect is not None and template.when_absent in ("mirror", "inverse"):
            changes.add(("retire", "absence"))
        if node.expect is not None and template.when_absent == "inverse":
            changes.update({("create", "absence"), ("revise", "absence")})
    return needed


def _authority(
    line: LineSpec, mandates: tuple[ProcedureMandate, ...], definition: Any
) -> RunbookPreviewAuthority:
    from cruxible_client.contracts.mandate_scope import SettleScopeGap, settle_scope_gap_key

    live = tuple(item for item in mandates if not item.suspended)
    settle = next((item for item in live if item.grants == "settle"), None)
    observe = line.max_authority == "observe"
    grant = settle if settle is not None and line.max_authority == "settle" else None
    nodes = [node for node in definition.nodes if isinstance(node, ClaimsNode)]
    needed: dict[str, set[tuple[str, str | None]]] = {}
    created: set[str] = set()
    for node in nodes:
        for claim_type, changes in _needed_changes(node).items():
            needed.setdefault(claim_type, set()).update(changes)
        if node.subject.create == "if_absent":
            created.add(node.subject.kind)
        created.update(_created_object_kinds(node))
    scope = {} if grant is None else {item.claim_type.target.name: item for item in grant.scope}
    gaps: dict[bytes, SettleScopeGap] = {}
    proposes: set[str] = set()
    if not observe:
        for claim_type, changes in sorted(needed.items()):
            item = scope.get(claim_type)
            for change, basis in sorted(changes, key=lambda pair: (pair[0], pair[1] or "")):
                term: str | None = None
                if item is None:
                    term = "claim_type"
                else:
                    covered = item.changes.of(change)  # type: ignore[arg-type]
                    if covered is None:
                        term = "change_kind"
                    elif basis is None or basis not in covered.basis:
                        term = "basis"
                    elif item.value is not None:
                        term = "value"
                    elif grant is not None and grant.subject_scope is not None:
                        term = "subject"
                if term is None:
                    continue
                proposes.add(claim_type)
                if grant is not None:
                    gap = SettleScopeGap(
                        claim_type=claim_type,
                        change=change,  # type: ignore[arg-type]
                        basis=basis,  # type: ignore[arg-type]
                        term=term,  # type: ignore[arg-type]
                    )
                    gaps[settle_scope_gap_key(gap)] = gap
    creation_scope = set() if grant is None else set(grant.subject_creation)
    condition = None if grant is None else grant.condition
    proposes_creation = set() if observe else created - creation_scope
    conditional = condition is not None and not observe
    return RunbookPreviewAuthority(
        max_authority=line.max_authority,
        mandates=_sorted(item.identity.qualified for item in live),
        grants=None if not live else ("settle" if settle is not None else "propose"),
        settles=tuple(
            RunbookSettleScope(
                claim_type=name,
                changes=tuple(
                    RunbookSettleChange(change=kind, basis=scoped.changes.of(kind).basis)  # type: ignore[union-attr]
                    for kind in scoped.change_kinds
                ),
                binding_subject_role=scoped.binding_subject_role,
                value_from=None if scoped.value is None else scoped.value.from_,
                value_to=None if scoped.value is None else scoped.value.to,
            )
            for name, scoped in sorted(scope.items())
        ),
        settles_creation=_sorted(creation_scope),
        subject_scope=None
        if grant is None or grant.subject_scope is None
        else tuple(item.artifact_path for item in grant.subject_scope),
        subjects=None if grant is None else grant.subjects,
        conditional=conditional,
        condition_query=None if condition is None else condition.query.target.qualified,
        condition_fallback=None if condition is None else condition.fallback,
        review_gaps=tuple(gaps[key] for key in sorted(gaps)),
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
    reader: DefinitionReader,
    runbook: Runbook,
    *,
    generation_root: str,
    workspace_reader: WorkspaceFileReader | None = None,
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
        authority = _authority(line, reader.mandates(line), definition)
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
                inputs=_inputs(reader, definition, line, workspace_reader),
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
    instance: PlaybillInstance,
    *,
    name: str,
    work: Counter[str] | None = None,
    workspace_file_reader: WorkspaceFileReader | None = None,
) -> RunbookPreview:
    """The Runbook's static preview at the accepted head (``runbook preview``).

    ``workspace_file_reader`` is the daemon's own reader: each workspace file a
    step reads is reported present, absent or refused by its rules. Without
    one, those inputs report ``no_workspace``.
    """

    from cruxible_core.service.runbooks import RunbookNotFoundError

    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        reader = DefinitionReader(projection.typed, work)
        runbook = reader.source(f"Runbook:{name}")
        if not isinstance(runbook, Runbook) or runbook.lifecycle.state != "live":
            raise RunbookNotFoundError(
                f"No live accepted Runbook named {name!r}; author one with a runbook input."
            )
        return compose_runbook_preview(
            reader,
            runbook,
            generation_root=coordinate.generation_root,
            workspace_reader=workspace_file_reader,
        )


__all__ = ["DefinitionReader", "compose_runbook_preview", "service_preview_runbook"]
