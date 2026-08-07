"""Static analysis of a procedure's control graph.

A procedure definition is a typed DAG. The edges already existed: the four
assert kinds are predicates on the edge to the next step whose false branch is
unconditionally "abort". Giving those predicates a SECOND successor is the
whole feature, and everything here follows from one structural rule --
**forward-only edges**.

Forward-only makes acyclicity a syntactic O(V) check and keeps the step list a
valid topological order, which is what lets the availability fixpoint be a
single forward pass rather than an iteration to convergence.

**Linearity is not a special case.** A definition with no declared control
edges has control graph ``s0 -> ... -> sn``, and the successor walk visits
exactly the sequence the flat loop visits today. The degenerate case is the
same code path, not a branch around it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cruxible_core.config.schema import WorkflowStepSchema, workflow_step_kind
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import (
    ABORT_TARGET,
    ProcedureFlowStepSchema,
    ProcedureGuardStepSchema,
    ProcedureProjectStepSchema,
    ProcedureRepeatStepSchema,
    unwrap_procedure_step,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.procedure.types import ProcedureDefinition

TRUE_ARM = "on_true"
FALSE_ARM = "on_false"
FALLTHROUGH = "next"


def procedure_node_kind(node: Any) -> str:
    """Return the node kind of one procedure step, wrappers unwrapped.

    A flow wrapper is NOT a kind: it is a successor override on the node it
    wraps, so it reports the wrapped step's kind and the wrapped step's id.
    """
    if isinstance(node, ProcedureGuardStepSchema):
        return "guard"
    if isinstance(node, ProcedureProjectStepSchema):
        return "project"
    inner = unwrap_procedure_step(node)
    if isinstance(inner, ProcedureRepeatStepSchema):
        return "repeat"
    if isinstance(inner, WorkflowStepSchema):
        return str(workflow_step_kind(inner))
    raise ConfigError(f"procedure step '{getattr(node, 'id', '?')}' has no recognised node kind")


def declared_control_targets(node: Any) -> dict[str, str]:
    """Return the control edges DECLARED on one node, by edge label.

    Read from the wrapper, before unwrapping: the wrapper owns ``next``, a
    guard owns ``on_true``/``on_false``. An absent ``on_false`` is the abort
    sentinel, not a fallthrough -- a guard whose false arm says nothing is a
    guard that stops the run, which is exactly what the assert kinds it
    supersedes already do.
    """
    if isinstance(node, ProcedureGuardStepSchema):
        targets: dict[str, str] = {}
        if node.on_true is not None:
            targets[TRUE_ARM] = node.on_true
        targets[FALSE_ARM] = node.on_false or ABORT_TARGET
        return targets
    if isinstance(node, ProcedureFlowStepSchema):
        return {FALLTHROUGH: node.next}
    return {}


@dataclass(frozen=True)
class ProcedureGraph:
    """The resolved control graph of one definition."""

    node_ids: tuple[str, ...]
    kinds: dict[str, str]
    edges: dict[str, dict[str, str]]
    """Per node, ``{edge label: target}``, with fallthrough resolved and
    ``"$abort"`` present as a target where it applies."""
    successors: dict[str, tuple[str, ...]]
    """Per node, the real successor node ids -- ``"$abort"`` excluded, since it
    terminates rather than continuing."""
    predecessors: dict[str, tuple[str, ...]]
    available_aliases: dict[str, frozenset[str]]
    """Per node, the aliases available on EVERY control path reaching it."""
    produced_alias: dict[str, str | None]

    @property
    def entry_id(self) -> str:
        return self.node_ids[0]

    def successors_of(self, node_id: str) -> tuple[str, ...]:
        return self.successors[node_id]


def build_procedure_graph(
    definition: ProcedureDefinition,
    *,
    initial_aliases: frozenset[str] = frozenset(),
) -> ProcedureGraph:
    """Resolve, verify and return the control graph. Refuses R1, R2, R3, R15."""
    steps = list(definition.steps)
    node_ids = tuple(str(step.id) for step in steps)
    index_of = {node_id: index for index, node_id in enumerate(node_ids)}
    kinds = {str(step.id): procedure_node_kind(step) for step in steps}

    edges: dict[str, dict[str, str]] = {}
    for position, step in enumerate(steps):
        node_id = node_ids[position]
        declared = declared_control_targets(step)
        _refuse_unknown_or_backward_targets(
            node_id=node_id,
            declared=declared,
            index_of=index_of,
            position=position,
        )
        resolved = dict(declared)
        fallthrough = node_ids[position + 1] if position + 1 < len(node_ids) else None
        if isinstance(step, ProcedureGuardStepSchema):
            # A guard with no `on_true` falls through, exactly as an assert
            # does on success. With no next step there is nothing to fall
            # through TO, so the true arm simply ends the walk.
            if TRUE_ARM not in resolved and fallthrough is not None:
                resolved[TRUE_ARM] = fallthrough
        elif FALLTHROUGH not in resolved and fallthrough is not None:
            resolved[FALLTHROUGH] = fallthrough
        # Canonical label order, so successor tuples and the digest's successor
        # map are stable regardless of which edges were declared.
        edges[node_id] = {
            label: resolved[label]
            for label in (TRUE_ARM, FALSE_ARM, FALLTHROUGH)
            if label in resolved
        }

    successors = {
        node_id: tuple(
            dict.fromkeys(target for target in targets.values() if target != ABORT_TARGET)
        )
        for node_id, targets in edges.items()
    }
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node_id, targets in successors.items():
        for target in targets:
            predecessors[target].append(node_id)

    _refuse_unreachable_nodes(node_ids, successors)

    produced_alias, available = _resolve_alias_availability(
        definition,
        node_ids=node_ids,
        predecessors=predecessors,
        initial_aliases=initial_aliases,
    )

    return ProcedureGraph(
        node_ids=node_ids,
        kinds=kinds,
        edges=edges,
        successors=successors,
        predecessors={node_id: tuple(values) for node_id, values in predecessors.items()},
        available_aliases=available,
        produced_alias=produced_alias,
    )


def _refuse_unknown_or_backward_targets(
    *,
    node_id: str,
    declared: dict[str, str],
    index_of: dict[str, int],
    position: int,
) -> None:
    for label, target in declared.items():
        if target == ABORT_TARGET:
            continue
        if target not in index_of:  # R1
            raise ConfigError(
                f"procedure step '{node_id}' control edge '{label}' targets "
                f"'{target}', which is not a step in this definition "
                f"(and is not the '{ABORT_TARGET}' sentinel)"
            )
        if index_of[target] <= position:  # R2
            raise ConfigError(
                f"procedure step '{node_id}' control edge '{label}' targets "
                f"'{target}', which is at or before it in the step list. Control "
                "edges are forward-only: that is what makes acyclicity a "
                "syntactic check and keeps the step list a topological order."
            )


def _refuse_unreachable_nodes(
    node_ids: tuple[str, ...],
    successors: dict[str, tuple[str, ...]],
) -> None:
    if not node_ids:
        return
    seen = {node_ids[0]}
    queue = deque([node_ids[0]])
    while queue:
        for target in successors[queue.popleft()]:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    unreachable = [node_id for node_id in node_ids if node_id not in seen]
    if unreachable:  # R3
        raise ConfigError(
            f"procedure steps {unreachable} are unreachable from the entry step "
            f"'{node_ids[0]}'. A step no path reaches is either a mistake or a "
            "misdirected control edge; either way it never runs."
        )


def _resolve_alias_availability(
    definition: ProcedureDefinition,
    *,
    node_ids: tuple[str, ...],
    predecessors: dict[str, list[str]],
    initial_aliases: frozenset[str],
) -> tuple[dict[str, str | None], dict[str, frozenset[str]]]:
    """Forward MUST-dataflow: ``avail(n) = intersection over preds of avail|produced``.

    Intersection, not union, is the whole point: an alias produced on ONE arm
    is not available to a node both arms reach, and reading it there is a
    runtime failure that no per-step check can see.

    Under linearity this reduces to "every earlier step's alias", which is
    byte-identical to the compiler's existing prior-alias walk.
    """
    produced: dict[str, str | None] = {}
    for step in definition.steps:
        inner = unwrap_procedure_step(step)
        produced[str(step.id)] = getattr(inner, "as_", None)

    refuse_shadowing = definition.graph_format == 2
    available: dict[str, frozenset[str]] = {}
    for position, node_id in enumerate(node_ids):
        preds = predecessors[node_id]
        if position == 0 or not preds:
            available[node_id] = initial_aliases
        else:
            sets = [available[pred] | _produced_set(produced[pred]) for pred in preds]
            available[node_id] = frozenset.intersection(*sets)
        alias = produced[node_id]
        if refuse_shadowing and alias is not None and alias in available[node_id]:
            raise ConfigError(  # R15
                f"procedure step '{node_id}' produces alias '{alias}', which is "
                "already produced on every path reaching it. Two producers of one "
                "alias on the same path make every downstream reference ambiguous."
            )
    return produced, available


def _produced_set(alias: str | None) -> frozenset[str]:
    return frozenset() if alias is None else frozenset({alias})


__all__ = [
    "FALLTHROUGH",
    "FALSE_ARM",
    "TRUE_ARM",
    "ProcedureGraph",
    "build_procedure_graph",
    "declared_control_targets",
    "procedure_node_kind",
]
