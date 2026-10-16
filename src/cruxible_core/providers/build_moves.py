"""Move live definitions from provider builds to another build, as one change.

A Procedure pins one exact provider build. Moving it to another build is its own
governed change (``provider move``, or a ``kit add`` that ships a new build of a
provider the Procedure uses): each occurrence moves to the new build's
implementation of the same interface contract, and the Procedure's dependents
are carried with it in the same change set. A Procedure instantiated from a
Blueprint is instantiated again, from the Blueprint's current version, with
the moved slot bound to the new build.

Lines take same-identity successors pinning the final live Procedure, disabled.
Mandates take same-name propose-only successors with settle fields cleared.
Triggers stay byte-identical. Retirement still retires dependent automation.

Cost: O(moved definitions + their dependents). The live pinners of a build come
from the typed pin index (``pins_by_target``), the dependents from the indexed
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
    parse_procedure,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.blueprints import (
    BlueprintInstantiationError,
    blueprint_digest,
    blueprint_path,
    instantiate_blueprint,
    parse_blueprint,
)
from cruxible_client.contracts.procedures.models import iter_pin_bindings
from cruxible_client.contracts.provider_installation import (
    AutomationCarryV1,
    ProviderRepinV1,
    StoppedAutomationV1,
)
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderV2,
    parse_provider,
    provider_digest,
    provider_interface_pin,
    provider_path,
)
from cruxible_core.claims.artifact_references import (
    ProviderBuildMove,
    ProviderBuildMoveRefused,
    move_references,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    ClaimTypeMigrationInventoryItemV1,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.providers.automation_carry import carry_automation

#: Families a move stops (retires) instead of carrying.
_STOPPED_KINDS = {"line": "line", "procedure-mandate": "mandate"}


@dataclass(frozen=True)
class Mover:
    """One live Procedure pinning a build that should move to another build."""

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
    """The move from build ``source`` to build ``target`` (identity names).

    Each implementation of the old build maps to the new build's implementation
    of the same interface id and digest, and each registration the old build
    binds to the one the new build binds for the same contract. What does not
    map refuses only the occurrences that use it.
    """

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
    implementations = {}
    for record in old.implementations:
        matches = [
            item.implementation_digest
            for item in new.implementations
            if item.interface_id == record.interface_id
            and item.interface_digest == record.interface_digest
        ]
        if len(matches) == 1:
            implementations[record.implementation_digest] = matches[0]
    interfaces: dict[str, ArtifactPin] = {}
    for pin in old.pins:
        if pin.role != "provider-interface":
            continue
        replacement = provider_interface_pin(new, pin.target.name.partition("/")[0])
        if replacement is None:
            continue
        before = _registration_digest(tree, pin)
        after = _registration_digest(tree, replacement)
        if before is not None and before == after:
            interfaces[pin.artifact_digest] = replacement
    return ProviderBuildMove(
        source=old_pin, target=new_pin, implementations=implementations, interfaces=interfaces
    )


def _registration_digest(tree: Mapping[str, bytes], pin: ArtifactPin) -> str | None:
    path = provider_interface_path(pin.target.name)
    content = tree.get(path)
    return (
        None if content is None else parse_provider_interface(content, path=path).interface_digest
    )


def _reinstantiated(
    tree: Mapping[str, bytes],
    overlay: Mapping[str, bytes],
    path: str,
    moves: Mapping[str, ProviderBuildMove],
) -> bytes | None:
    """A Blueprint instance moved by instantiating its Blueprint again, or None
    when the Procedure records no Blueprint origin.

    The Blueprint is read at its current version in this change (a kit may be
    replacing it), each slot bound to its recorded Provider or, where that Provider
    build moves, to the new build. Raises ``ProviderBuildMoveRefused`` when the
    Blueprint is gone or a slot's new build does not fit it.
    """

    if not path.startswith("procedures/"):
        return None
    procedure = parse_procedure(tree[path], path=path)
    origin = procedure.blueprint
    if origin is None:
        return None

    def read(target: str) -> bytes | None:
        return overlay[target] if target in overlay else tree.get(target)

    blueprint_target = blueprint_path(origin.blueprint.target.name)
    content = read(blueprint_target)
    blueprint = None if content is None else parse_blueprint(content, path=blueprint_target)
    if blueprint is None or blueprint.lifecycle.state != "live":
        raise ProviderBuildMoveRefused(
            f"{procedure.identity.qualified} is a {origin.blueprint.target.qualified} instance "
            "and that Blueprint is not live to instantiate again",
            reason="not_movable",
        )
    providers: dict[str, AcceptedProvider] = {}
    for binding in origin.bindings:
        pin = binding.artifact_pin
        move = moves.get(pin.target.qualified)
        if move is not None and move.source.artifact_digest == pin.artifact_digest:
            pin = move.target
        provider_target = provider_path(pin.target.name)
        raw = read(provider_target)
        if raw is None:
            raise ProviderBuildMoveRefused(
                f"{pin.target.qualified} is not installed", reason="not_movable"
            )
        provider = parse_provider(raw, path=provider_target)
        providers[binding.slot_name] = AcceptedProvider(
            path=provider_target,
            provider=provider,
            artifact_digest=provider_digest(provider).tagged,
        )
    referenced = {
        (pin.role, pin.target.qualified, pin.artifact_digest)
        for pin in iter_pin_bindings(procedure.definition)
        if isinstance(pin, ArtifactPin)
    }
    try:
        moved = instantiate_blueprint(
            blueprint,
            blueprint_artifact_digest=blueprint_digest(blueprint).tagged,
            name=procedure.identity.name,
            providers=providers,
            activation_policy=procedure.activation_policy,
            extra_pins=tuple(
                pin
                for pin in procedure.pins
                if (pin.role, pin.target.qualified, pin.artifact_digest) not in referenced
            ),
            lifecycle=ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(procedure).tagged
            ),
        )
    except (BlueprintInstantiationError, ValueError) as exc:
        raise ProviderBuildMoveRefused(str(exc), reason="contract_changed") from exc
    return render_procedure(moved)


def _stopped(
    state: ArtifactDependencyStateV1, content: bytes, replacements: Mapping[str, str]
) -> bytes:
    """A Line or Mandate retired, its Procedure pins moved to the moved version.

    The retired successor still names exact versions present in the change,
    so the closure the change owes holds; it never runs again.
    """

    payload = json.loads(content)
    for key in ("procedure", "pins"):
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


def _retired_trigger(path: str, content: bytes) -> bytes:
    state = parse_dependency_artifact(path, content)
    assert state is not None
    payload = json.loads(content)
    payload["lifecycle"] = {"state": "retired", "predecessor_digest": state.artifact_digest}
    return pretty_canonical_bytes(payload)


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


def settle_changes(
    tree: Mapping[str, bytes],
    *,
    writes: Mapping[str, bytes],
    connection: sqlite3.Connection,
    movers: Sequence[Mover] = (),
    moves: Mapping[str, ProviderBuildMove] | None = None,
    keep: frozenset[str] = frozenset(),
    retire_with: frozenset[str] = frozenset(),
) -> SettledMoves:
    """Every write, the moved definitions, and one successor per dependent they owe.

    ``writes`` are the change's own definitions: a dependent of one that already
    exists here is carried to its final bytes, or retired with it when its
    identity is in ``retire_with``. Each mover takes the build move unless its
    identity is in ``keep``. Lines over live Procedure successors arrive disabled;
    Mandates arrive propose-only. Triggers are untouched except on retirement.
    """

    moves = dict(moves or {})
    settled = SettledMoves()
    changed = {
        path: content for path, content in writes.items() if path in tree and content != tree[path]
    }
    unmoved: set[str] = set()
    movable: list[Mover] = []
    for mover in movers:
        name = mover.identity.qualified
        if name in keep:
            unmoved.add(name)
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
            reinstantiated = (
                None if mover.path in writes else _reinstantiated(tree, writes, mover.path, moves)
            )
            if reinstantiated is None:
                # Try the move alone, so a contract change refuses this one
                # definition (it stays on its build) rather than the change.
                move_references(mover.path, json.loads(tree[mover.path]), {}, moves)
            else:
                changed[mover.path] = reinstantiated
        except ProviderBuildMoveRefused as exc:
            unmoved.add(name)
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
            name: dependent_closure_inventory(tree, roots=(identity,), fixed_paths=fixed)
            for name, identity in roots.items()
        }
        mover_closures = {
            mover.identity.qualified: dependent_closure_inventory(
                tree, roots=(mover.identity,), fixed_paths=fixed
            )
            for mover in movable
        }
    except ClaimTypeMigrationError as error:
        settled.refused.append(str(error))
        return settled
    union: dict[str, ClaimTypeMigrationInventoryItemV1] = {}
    for mover in movable:
        if mover.path in changed:
            continue
        state = parse_dependency_artifact(mover.path, tree[mover.path])
        assert state is not None
        union[mover.path] = ClaimTypeMigrationInventoryItemV1(
            identity=mover.identity,
            path=mover.path,
            artifact_kind=state.artifact_kind,
            current_artifact_digest=state.artifact_digest,
            triggering_identity=ArtifactIdentity(kind="Provider", name=mover.from_build),
            dependency_edge_role="provider",
            permitted_dispositions=("retire", "successor"),
        )
    for items in (*closures.values(), *mover_closures.values()):
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
        if item.artifact_kind in _STOPPED_KINDS:
            if path in writes:
                settled.refused.append(
                    f"{item.identity.qualified}: explicit automation write conflicts with carry; "
                    "author it separately"
                )
            stopped.append(item)
            continue
        if item.artifact_kind == "procedure" and item.path not in changed:
            try:
                reinstantiated = _reinstantiated(tree, {**writes, **changed}, item.path, moves)
            except ProviderBuildMoveRefused as exc:
                settled.refused.append(f"{item.identity.qualified} cannot be carried: {exc}")
                continue
            if reinstantiated is not None:
                changed[item.path] = reinstantiated
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
        if path not in mover_paths:
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
            builds=moves,
            unmoved=frozenset(unmoved),
        )
    except (ClaimTypeMigrationError, ProviderBuildMoveRefused) as error:
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
    # Only live, same-identity Procedure successors are carry targets. Retirement
    # continues through the existing stop path. All pins use FINAL digests.
    live_procedures: dict[str, str] = {}
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
    for item in stopped:
        state = parse_dependency_artifact(item.path, tree[item.path])
        assert state is not None
        procedure_pins = [pin for pin in state.pins if pin.role == "procedure"]
        if any(pin.artifact_digest in live_procedures for pin in procedure_pins):
            try:
                carried_content, outcome = carry_automation(
                    item.path, tree[item.path], live_procedures
                )
                assert carried_content is not None
                final[item.path] = carried_content
                settled.automation_carries.append(outcome)
                settled.carried += 1
            except ValueError as exc:
                settled.refused.append(
                    f"{item.identity.qualified}: cannot carry automation: {exc}; "
                    "author a compatible successor"
                )
            continue
        # Automation reached through some OTHER changed dependency needs its
        # own authoring; never retire it merely because traversal reached it.
        if not any(pin.artifact_digest in replacements for pin in procedure_pins):
            settled.refused.append(
                f"{item.identity.qualified}: cannot carry non-Procedure dependency change; "
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
                final[trigger_path] = _retired_trigger(trigger_path, tree[trigger_path])
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
