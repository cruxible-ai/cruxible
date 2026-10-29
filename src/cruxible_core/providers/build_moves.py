"""Move live Lines from provider builds to another build, as one change.

A Procedure names no provider build: each provider-calling node names a slot,
and a Line binds every slot to one exact ``{build, operation}``. Moving to
another build is therefore a change to Lines (``provider move``, or a ``kit
add`` that ships a new build of a provider some Line binds): each binding to
the old build moves to the new build's operation serving the same exact
signature, in a Line successor, in the same change set.

A Line successor is a new Line digest, so a grant never follows it (ruling C3):
each Mandate pinning the old Line digest takes a same-name propose-only
successor pinning the new one, settle fields cleared. A Line over a live
Procedure successor (a kit's new Procedure version) takes a successor pinning
that version the same way. Triggers stay byte-identical. Retirement still
retires dependent automation.

Cost: O(moved Lines + their dependents). The live binders of a build come from
the maintained ``provider_pinners`` relation, the dependents from the indexed
reverse-pin walk, and the Triggers of a stopped Line from ``triggers_by_target``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    parse_procedure,
)
from cruxible_client.contracts.provider_installation import (
    AutomationCarryV1,
    ProviderRepinV1,
    StoppedAutomationV1,
)
from cruxible_client.contracts.provider_operations import LineBinding, ProviderOperation
from cruxible_client.contracts.providers import (
    ProviderV2,
    parse_provider,
    provider_digest,
    provider_path,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    ClaimTypeMigrationInventoryItemV1,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.providers.automation_carry import (
    carry_line,
    carry_mandate,
    parse_carry_line,
    validate_carried_line,
)

#: Families a move stops (retires) or carries instead of re-pinning in place.
_STOPPED_KINDS = {"line": "line", "procedure-mandate": "mandate"}


class ProviderBuildMoveRefused(ValueError):
    """A Line cannot move to the target build: the build serves no operation with a
    bound slot's exact signature (``reason`` = ``contract_changed``)."""

    def __init__(self, message: str, *, reason: str = "contract_changed") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ProviderBuildMove:
    """Move every Line binding to one provider build onto another build."""

    source: ArtifactPin
    target: ArtifactPin
    source_operations: tuple[ProviderOperation, ...]
    target_operations: tuple[ProviderOperation, ...]

    def moved(self, binding: LineBinding) -> LineBinding:
        """The binding on the target build: its operation with the same exact signature.

        The operation of the same name wins when it serves that signature; else the
        one target operation that does. Anything else refuses ``contract_changed``.
        """

        old = next(
            (item for item in self.source_operations if item.name == binding.operation), None
        )
        if old is None:
            raise ProviderBuildMoveRefused(
                f"{binding.spelling} names no operation of {self.source.target.name}"
            )
        fitting = [
            item for item in self.target_operations if item.signature().same_as(old.signature())
        ]
        chosen = next((item for item in fitting if item.name == old.name), None)
        if chosen is None and len(fitting) == 1:
            chosen = fitting[0]
        if chosen is None:
            raise ProviderBuildMoveRefused(
                f"{self.target.target.name} serves "
                + ("no" if not fitting else "several")
                + f" operation with {old.signature().spelling()}"
            )
        return LineBinding(build=self.target, operation=chosen.name)


@dataclass(frozen=True)
class Mover:
    """One live Line affected by a move to another build."""

    identity: ArtifactIdentity
    path: str
    from_build: str
    to_build: str


@dataclass
class SettledMoves:
    """Everything one change set writes to settle its definitions and moves."""

    writes: dict[str, bytes] = field(default_factory=dict)
    repins: list[ProviderRepinV1] = field(default_factory=list)
    stops: list[StoppedAutomationV1] = field(default_factory=list)
    automation_carries: list[AutomationCarryV1] = field(default_factory=list)
    #: Changed definition identity -> its live dependents (each carried or retired).
    counts: dict[str, int] = field(default_factory=dict)
    #: Dependents carried (re-pinned) beyond the moved definitions themselves.
    carried: int = 0
    #: Why the change cannot be settled; empty when it can.
    refused: list[str] = field(default_factory=list)


def build_move(tree: Mapping[str, bytes], source: str, target: str) -> ProviderBuildMove:
    """The move from build ``source`` to build ``target`` (identity names)."""

    def accepted(name: str) -> tuple[ProviderV2, ArtifactPin]:
        path = provider_path(name)
        provider = parse_provider(tree[path], path=path)
        if not isinstance(provider, ProviderV2):
            raise ProviderBuildMoveRefused(f"{name} runs no package build", reason="not_movable")
        pin = ArtifactPin(
            role="provider",
            target=provider.identity,
            artifact_digest=provider_digest(provider).tagged,
        )
        return provider, pin

    old, old_pin = accepted(source)
    new, new_pin = accepted(target)
    return ProviderBuildMove(
        source=old_pin,
        target=new_pin,
        source_operations=old.operations,
        target_operations=new.operations,
    )


def _rebound_line(
    tree: Mapping[str, bytes],
    overlay: Mapping[str, bytes],
    path: str,
    moves: Mapping[str, ProviderBuildMove],
) -> bytes | None:
    """The affected Line's successor with each moved binding on its new build."""
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        parse_line_spec,
        render_line_spec,
    )

    previous = parse_line_spec(tree[path], path=path)
    line = parse_line_spec(overlay[path], path=path) if path in overlay else previous
    bindings = dict(line.bindings)
    for key, binding in line.bindings.items():
        if path in overlay and previous.bindings.get(key) != binding:
            continue  # Preserve an explicit binding authored with the release.
        move = moves.get(binding.build.target.qualified)
        if move is None or move.source.artifact_digest != binding.build.artifact_digest:
            continue
        bindings[key] = move.moved(binding)
    if bindings == line.bindings:
        return None
    return render_line_spec(
        line.model_copy(
            update={
                "bindings": bindings,
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=line_spec_digest(previous).tagged
                ),
            }
        )
    )


def _stopped(
    state: ArtifactDependencyStateV1, content: bytes, replacements: Mapping[str, str]
) -> bytes:
    """A Line or Mandate retired, its pins moved to the moved versions.

    The retired successor still names exact versions present in the change,
    so the closure the change owes holds; it never runs again.
    """

    payload = json.loads(content)
    for key in ("procedure", "line", "bindings"):
        if key in payload:
            payload[key] = _replace_digests(payload[key], replacements)
    payload["lifecycle"] = {"state": "retired", "predecessor_digest": state.artifact_digest}
    return pretty_canonical_bytes(payload)


def _replace_digests(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_digests(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_digests(item, replacements) for key, item in value.items()}
    return value


def _retired_trigger(path: str, content: bytes, *, by_name: bool = False) -> bytes:
    state = parse_dependency_artifact(path, content)
    assert state is not None
    payload = json.loads(content)
    payload["lifecycle"] = {"state": "retired", "predecessor_digest": state.artifact_digest}
    retired = pretty_canonical_bytes(payload)
    if not by_name:
        return retired
    from cruxible_client.contracts.named_references import name_trigger
    from cruxible_client.contracts.triggers import parse_trigger, render_trigger

    return render_trigger(name_trigger(parse_trigger(retired, path=path)))


def live_triggers_of(connection: sqlite3.Connection, line: str) -> tuple[tuple[str, str], ...]:
    """(identity, path) of the live Triggers aimed at one Line (``triggers_by_target``)."""

    return tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT identity,path FROM main.triggers WHERE target_kind='line' AND target=? "
            "AND lifecycle='live' ORDER BY identity",
            (line,),
        )
    )


def _mandate_line_digest(path: str, content: bytes) -> str:
    from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate_any

    return parse_procedure_mandate_any(content, path=path).line.artifact_digest


def _pinned_digests(kind: str, path: str, content: bytes) -> tuple[str, ...]:
    if kind == "line":
        line = parse_carry_line(content, path=path)
        return (
            line.procedure.artifact_digest,
            *(binding.build.artifact_digest for binding in line.bindings.values()),
        )
    return (_mandate_line_digest(path, content),)


def settle_changes(
    tree: Mapping[str, bytes],
    *,
    writes: Mapping[str, bytes],
    connection: sqlite3.Connection,
    movers: Sequence[Mover] = (),
    moves: Mapping[str, ProviderBuildMove] | None = None,
    keep: frozenset[str] = frozenset(),
    retire_with: frozenset[str] = frozenset(),
    by_name: bool = False,
) -> SettledMoves:
    """Every write, the moved Lines, and one successor per dependent they owe.

    ``writes`` are the change's own definitions: a dependent of one that already
    exists here is carried to its final bytes, or retired with it when its
    identity is in ``retire_with``. Each mover takes the build move unless its
    identity is in ``keep``. Lines over live Procedure successors arrive
    disabled; every Mandate pinning a Line that changed arrives propose-only.
    Triggers are untouched except on retirement.
    """

    moves = dict(moves or {})
    settled = SettledMoves()
    changed = {
        path: content for path, content in writes.items() if path in tree and content != tree[path]
    }
    movable: list[Mover] = []
    for mover in movers:
        name = mover.identity.qualified
        if name in keep:
            settled.repins.append(
                ProviderRepinV1(
                    identity=name,
                    from_build=mover.from_build,
                    to_build=mover.to_build,
                    action="kept",
                    reason="keep_build",
                )
            )
            continue
        try:
            rebound = _rebound_line(tree, writes, mover.path, moves)
        except ProviderBuildMoveRefused as exc:
            settled.repins.append(
                ProviderRepinV1(
                    identity=name,
                    from_build=mover.from_build,
                    to_build=mover.to_build,
                    action="refused",
                    reason="not_movable" if exc.reason == "not_movable" else "contract_changed",
                )
            )
            continue
        if rebound is None:
            continue
        changed[mover.path] = rebound
        movable.append(mover)
        settled.repins.append(
            ProviderRepinV1(
                identity=name,
                from_build=mover.from_build,
                to_build=mover.to_build,
                action="successor",
            )
        )
    fixed = frozenset(
        path
        for path in {*writes, *changed}
        if not path.startswith(("lines/", "procedure-mandates/"))
    )
    roots: dict[str, ArtifactIdentity] = {}
    for path in sorted(changed):
        state = parse_dependency_artifact(path, tree[path])
        if state is not None:
            roots[state.identity.qualified] = state.identity
    try:
        closures = {
            name: dependent_closure_inventory(
                tree,
                roots=(identity,),
                fixed_paths=fixed,
                by_name=by_name and name not in retire_with,
            )
            for name, identity in roots.items()
        }
    except ClaimTypeMigrationError as error:
        settled.refused.append(str(error))
        return settled
    union: dict[str, ClaimTypeMigrationInventoryItemV1] = {}
    for items in closures.values():
        for item in items:
            union.setdefault(item.path, item)
    settled.counts = {name: len(items) for name, items in closures.items() if items}
    retiring = {item.path for name in retire_with for item in closures.get(name, ())}
    mover_paths = {mover.path for mover in movable}
    stopped: list[ClaimTypeMigrationInventoryItemV1] = []
    inventory: list[ClaimTypeMigrationInventoryItemV1] = []
    dispositions: list[ClaimTypeDependentDisposition] = []
    for path in sorted(union):
        item = union[path]
        if path in mover_paths:
            continue
        if (
            by_name
            and path not in retiring
            and item.artifact_kind
            in {"procedure", "query-definition", "trigger", "capture-contract", "provider"}
        ):
            # Unchanged governed referrers are listed/rechecked by admission;
            # only exact dependents (Lines, Mandates, evidence) need a successor.
            continue
        if item.artifact_kind in _STOPPED_KINDS or (
            by_name and item.artifact_kind == "trigger" and path in retiring
        ):
            if path in writes:
                settled.refused.append(
                    f"{item.identity.qualified}: explicit automation write conflicts with carry; "
                    "author it separately"
                )
            stopped.append(item)
            continue
        if item.path in retiring and "retire" in item.permitted_dispositions:
            disposition = ClaimTypeDependentDisposition(
                identity=item.identity,
                disposition="retire",
                claim_retirement_reason="was-rescinded" if item.artifact_kind == "claim" else None,
            )
        elif "successor" in item.permitted_dispositions:
            disposition = ClaimTypeDependentDisposition(
                identity=item.identity, disposition="successor"
            )
        else:
            settled.refused.append(
                f"{item.identity.qualified} cannot be carried to "
                f"{item.triggering_identity.qualified}"
            )
            continue
        inventory.append(item)
        dispositions.append(disposition)
        settled.carried += 1
    if settled.refused:
        return settled
    try:
        carried, _normalized = build_dependent_closure_candidate(
            tree=tree,
            changed=changed,
            inventory=tuple(inventory),
            dispositions=tuple(dispositions),
            move_graphs=True,
        )
    except ClaimTypeMigrationError as error:
        settled.refused.append(str(error))
        return settled
    final = {**writes, **changed, **carried}
    replacements: dict[str, str] = {}
    for path, content in final.items():
        before = tree.get(path)
        if before is None:
            continue
        old = parse_dependency_artifact(path, before)
        new = parse_dependency_artifact(path, content)
        if old is not None and new is not None and old.artifact_digest != new.artifact_digest:
            replacements[old.artifact_digest] = new.artifact_digest
    # Only live, same-identity Procedure successors are Line carry targets.
    # Retirement continues through the stop path. All pins use FINAL digests.
    live_procedures: dict[str, str] = {}
    final_procedures: dict[str, AcceptedProcedure] = {}
    for path, content in final.items():
        if not path.startswith("procedures/") or path not in tree:
            continue
        old_procedure = parse_dependency_artifact(path, tree[path])
        new_procedure = parse_dependency_artifact(path, content)
        if (
            old_procedure is not None
            and new_procedure is not None
            and old_procedure.identity == new_procedure.identity
            and new_procedure.lifecycle.state == "live"
            and old_procedure.artifact_digest != new_procedure.artifact_digest
        ):
            live_procedures[old_procedure.artifact_digest] = new_procedure.artifact_digest
            final_procedures[old_procedure.artifact_digest] = AcceptedProcedure(
                path=path,
                procedure=parse_procedure(content, path=path),
                artifact_digest=new_procedure.artifact_digest,
            )
    # Moved Lines are live successors too: their Mandates carry to them.
    live_lines: dict[str, str] = {}
    for path in mover_paths:
        old_line = parse_dependency_artifact(path, tree[path])
        new_line = parse_dependency_artifact(path, final[path])
        if old_line is not None and new_line is not None:
            live_lines[old_line.artifact_digest] = new_line.artifact_digest
    # Lines first: a carried Line's new digest is what its Mandates carry to.
    for item in sorted(stopped, key=lambda item: (item.artifact_kind != "line", item.path)):
        state = parse_dependency_artifact(item.path, tree[item.path])
        assert state is not None
        if by_name and item.artifact_kind == "trigger":
            if item.path not in final:
                final[item.path] = _retired_trigger(item.path, tree[item.path], by_name=True)
                settled.stops.append(
                    StoppedAutomationV1(
                        identity=item.identity.qualified,
                        kind="trigger",
                        stopped_by=item.triggering_identity.qualified,
                    )
                )
            continue
        if item.path not in retiring:
            try:
                if item.artifact_kind == "line":
                    procedure_digest = parse_carry_line(
                        tree[item.path], path=item.path
                    ).procedure.artifact_digest
                    if procedure_digest in live_procedures:
                        validate_carried_line(
                            tree[item.path],
                            path=item.path,
                            procedure=final_procedures[procedure_digest],
                        )
                        line_content, outcome = carry_line(
                            item.path, tree[item.path], live_procedures
                        )
                        assert line_content is not None
                        final[item.path] = line_content
                        new_state = parse_dependency_artifact(item.path, line_content)
                        assert new_state is not None
                        live_lines[state.artifact_digest] = new_state.artifact_digest
                        replacements[state.artifact_digest] = new_state.artifact_digest
                        settled.automation_carries.append(outcome)
                        settled.carried += 1
                        continue
                elif _mandate_line_digest(item.path, tree[item.path]) in live_lines:
                    mandate_content, outcome = carry_mandate(item.path, tree[item.path], live_lines)
                    assert mandate_content is not None
                    final[item.path] = mandate_content
                    settled.automation_carries.append(outcome)
                    settled.carried += 1
                    continue
            except ValueError as exc:
                settled.refused.append(
                    f"{item.identity.qualified}: cannot carry automation: {exc}; "
                    "author a compatible successor"
                )
                continue
            if not any(
                digest in replacements
                for digest in _pinned_digests(item.artifact_kind, item.path, tree[item.path])
            ):
                # Automation reached through some OTHER changed dependency needs
                # its own authoring; never retire it merely because traversal
                # reached it.
                settled.refused.append(
                    f"{item.identity.qualified}: cannot carry this dependency change; "
                    "author a compatible successor"
                )
                continue
        final[item.path] = _stopped(state, tree[item.path], replacements)
        settled.stops.append(
            StoppedAutomationV1(
                identity=item.identity.qualified,
                kind=_STOPPED_KINDS[item.artifact_kind],  # type: ignore[arg-type]
                stopped_by=item.triggering_identity.qualified,
            )
        )
        if item.artifact_kind == "line":
            for trigger, trigger_path in live_triggers_of(connection, item.identity.qualified):
                if trigger_path in final:
                    continue
                final[trigger_path] = _retired_trigger(
                    trigger_path, tree[trigger_path], by_name=by_name
                )
                settled.stops.append(
                    StoppedAutomationV1(
                        identity=trigger, kind="trigger", stopped_by=item.identity.qualified
                    )
                )
    settled.writes = final if not settled.refused else {}
    settled.automation_carries.sort(key=lambda item: item.identity)
    settled.stops.sort(key=lambda item: item.identity)
    settled.repins.sort(key=lambda item: item.identity)
    return settled


__all__ = [
    "Mover",
    "ProviderBuildMove",
    "ProviderBuildMoveRefused",
    "SettledMoves",
    "build_move",
    "live_triggers_of",
    "settle_changes",
]
