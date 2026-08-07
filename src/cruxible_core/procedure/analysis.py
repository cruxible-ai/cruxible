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
from collections.abc import Collection, Sequence
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


def resolve_control_edges(steps: Sequence[Any]) -> dict[str, dict[str, str]]:
    """Resolve every node's outgoing edges, fallthrough included. NO refusals.

    Split out of :func:`build_procedure_graph` because the budget DP (§3.3)
    needs the same resolution on a definition that is being VALIDATED, where
    the structural refusals do not belong: R1/R2/R3/R15 are compile-time
    refusals with their own messages and their own call site, and firing them
    from a pydantic validator would move them to parse time and change what a
    v1 definition does today.
    """
    node_ids = [str(step.id) for step in steps]
    edges: dict[str, dict[str, str]] = {}
    for position, step in enumerate(steps):
        resolved = dict(declared_control_targets(step))
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
        edges[node_ids[position]] = {
            label: resolved[label]
            for label in (TRUE_ARM, FALSE_ARM, FALLTHROUGH)
            if label in resolved
        }
    return edges


def control_successors(edges: dict[str, dict[str, str]]) -> dict[str, tuple[str, ...]]:
    """Return the real successor ids per node -- ``"$abort"`` excluded.

    Abort terminates rather than continuing, so it is not a successor: no
    availability flows through it, no path continues past it, and no budget
    accumulates beyond it.
    """
    return {
        node_id: tuple(
            dict.fromkeys(target for target in targets.values() if target != ABORT_TARGET)
        )
        for node_id, targets in edges.items()
    }


def control_targets_are_forward_only(steps: Sequence[Any]) -> bool:
    """Report whether every declared target is a known, strictly later step.

    The precondition of every analysis here. When it does not hold the
    definition is structurally broken and the compiler refuses it (R1/R2); the
    budget DP consults this so it can fall back to a sound over-approximation
    instead of walking a graph that is not a DAG.
    """
    node_ids = [str(step.id) for step in steps]
    index_of = {node_id: index for index, node_id in enumerate(node_ids)}
    for position, step in enumerate(steps):
        for target in declared_control_targets(step).values():
            if target == ABORT_TARGET:
                continue
            if target not in index_of or index_of[target] <= position:
                return False
    return True


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
    """Per node, the aliases available on EVERY control path reaching it.

    MUST-availability: the intersection over predecessors. This is the right
    question for REFERENCE VALIDITY -- an alias produced on one arm only is
    genuinely unavailable to a node both arms reach."""
    reachable_aliases: dict[str, frozenset[str]]
    """Per node, the aliases produced on AT LEAST ONE control path reaching it.

    MAY-reachability: the union over predecessors, and a different question
    from the one above. It is the right question for DUPLICATE PRODUCTION,
    because "two producers of one alias on the same path" is satisfied by ONE
    such path -- the intersection would have dropped the alias at the join and
    called the collision legal."""
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

    for position, step in enumerate(steps):
        _refuse_unknown_or_backward_targets(
            node_id=node_ids[position],
            declared=declared_control_targets(step),
            index_of=index_of,
            position=position,
        )

    edges = resolve_control_edges(steps)
    successors = control_successors(edges)
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node_id, targets in successors.items():
        for target in targets:
            predecessors[target].append(node_id)

    _refuse_unreachable_nodes(node_ids, successors)

    produced_alias, available, reachable = _resolve_alias_availability(
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
        reachable_aliases=reachable,
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
) -> tuple[dict[str, str | None], dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Compute BOTH dataflow directions, because two rules need two questions.

    ``avail(n)`` is MUST: the intersection over predecessors. An alias produced
    on ONE arm is not available to a node both arms reach, and reading it there
    is a runtime failure that no per-step check can see. Under linearity this
    reduces to "every earlier step's alias", which is byte-identical to the
    compiler's existing prior-alias walk.

    ``reach(n)`` is MAY: the union. Duplicate production needs it, because the
    hazard is satisfied by ONE path carrying two producers. Guard arms that
    produce different aliases make the intersection at the join drop BOTH, so a
    must-only check declares a genuine same-path collision legal and lets the
    second producer silently overwrite the first.
    """
    produced: dict[str, str | None] = {}
    for step in definition.steps:
        inner = unwrap_procedure_step(step)
        produced[str(step.id)] = getattr(inner, "as_", None)

    refuse_shadowing = definition.graph_format == 2
    available: dict[str, frozenset[str]] = {}
    reachable: dict[str, frozenset[str]] = {}
    for position, node_id in enumerate(node_ids):
        preds = predecessors[node_id]
        if position == 0 or not preds:
            available[node_id] = initial_aliases
            reachable[node_id] = initial_aliases
        else:
            contributions = [
                (available[pred] | _produced_set(produced[pred]), pred) for pred in preds
            ]
            available[node_id] = frozenset.intersection(
                *[contribution for contribution, _pred in contributions]
            )
            reachable[node_id] = frozenset().union(
                *[reachable[pred] | _produced_set(produced[pred]) for pred in preds]
            )
        alias = produced[node_id]
        if refuse_shadowing and alias is not None and alias in reachable[node_id]:
            raise ConfigError(  # R15
                f"procedure step '{node_id}' produces alias '{alias}', which is "
                "already produced on at least one path reaching it. Two producers of "
                "one alias on the same path make every downstream reference "
                "ambiguous, and the second silently overwrites the first."
            )
    return produced, available, reachable


def _produced_set(alias: str | None) -> frozenset[str]:
    return frozenset() if alias is None else frozenset({alias})


# ---------------------------------------------------------------------------
# Analysis 4 -- per-path contract checking (§3.5)
# ---------------------------------------------------------------------------


def has_path_avoiding(graph: ProcedureGraph, avoided: Collection[str]) -> bool:
    """Report whether some entry-to-exit path touches NONE of ``avoided``.

    The question behind ``contract_field_path_conditional``: an input consumed
    only on the escalation arm is one a caller can be asked for and never have
    used, and nothing says so today -- the field IS consumed, so the unconsumed
    warning stays silent and the caller learns the difference at run time or
    never.

    An EXIT is a node with no successors. ``"$abort"`` is deliberately not one:
    a guard's false arm ending the run is a refusal, not an execution that
    completed without the field. Counting abort arms would make nearly every
    input downstream of a guard "path conditional", and a warning that fires
    on nearly everything is not a warning.
    """
    blocked = set(avoided)
    if not graph.node_ids or graph.entry_id in blocked:
        return False
    seen = {graph.entry_id}
    queue = deque([graph.entry_id])
    while queue:
        node_id = queue.popleft()
        successors = graph.successors_of(node_id)
        if not successors:
            return True
        for target in successors:
            if target in blocked or target in seen:
                continue
            seen.add(target)
            queue.append(target)
    return False


# ---------------------------------------------------------------------------
# Analysis 3 -- worst-case budget (§3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorstCasePath:
    """One worst-case weighted count and the path that realises it."""

    count: int
    path: tuple[str, ...]


@dataclass(frozen=True)
class WorstCaseExpansion:
    """The two path-maximal execution counts of one definition.

    ``total_steps`` is deliberately absent: it counts STORED definitions, not
    executed ones, so it stays a sum over the whole body and lives where it
    always did.
    """

    expanded_steps: WorstCasePath
    expanded_provider_calls: WorstCasePath


def node_step_weight(node: Any) -> int:
    """Steps one node contributes to a single execution, repeat expanded."""
    inner = unwrap_procedure_step(node)
    if isinstance(inner, ProcedureRepeatStepSchema):
        return 1 + inner.repeat.max_attempts * len(inner.repeat.steps)
    return 1


def node_provider_weight(node: Any) -> int:
    """Provider calls one node contributes to a single execution.

    A guard is zero: it decides where control goes, it does not call out.
    """
    inner = unwrap_procedure_step(node)
    if isinstance(inner, ProcedureRepeatStepSchema):
        nested = sum(workflow_step_kind(step) == "provider" for step in inner.repeat.steps)
        return inner.repeat.max_attempts * nested
    if isinstance(inner, WorkflowStepSchema) and workflow_step_kind(inner) == "provider":
        return 1
    return 0


def longest_weighted_path(
    node_ids: Sequence[str],
    successors: dict[str, tuple[str, ...]],
    weights: dict[str, int],
) -> WorstCasePath:
    """Return the heaviest entry-to-exit path and its weight. ``O(V+E)``.

    Forward-only edges (R2) make the step list a topological order, so ONE
    reverse pass computes the maximum -- no iteration to a fixpoint, no
    enumeration of paths. Ties break on the first successor in canonical edge
    order, which is what makes the reported witness deterministic rather than
    dictionary-order noise.
    """
    if not node_ids:
        return WorstCasePath(count=0, path=())
    best: dict[str, int] = {}
    via: dict[str, str | None] = {}
    for node_id in reversed(node_ids):
        chosen: str | None = None
        downstream = 0
        for target in successors.get(node_id, ()):
            if chosen is None or best[target] > downstream:
                chosen = target
                downstream = best[target]
        best[node_id] = weights[node_id] + downstream
        via[node_id] = chosen
    entry = node_ids[0]
    path: list[str] = []
    current: str | None = entry
    while current is not None:
        path.append(current)
        current = via[current]
    return WorstCasePath(count=best[entry], path=tuple(path))


def worst_case_expansion(steps: Sequence[Any]) -> WorstCaseExpansion:
    """Return the longest-path step and provider-call counts, with witnesses.

    §3.3's ``longest_path_provider_calls``, run once per weight function over
    one resolution of the control graph. The two maxima are computed
    INDEPENDENTLY because they are maxima of different weightings: the path
    that runs the most steps need not be the path that makes the most provider
    calls, and reporting one path's count beside the other path's node list
    would be a number nothing realises.

    Under linearity every node is on the one path and each maximum equals the
    sum the pre-graph implementation computed -- the regression obligation of
    §3.2, asserted over the corpus by T4 rather than promised here.

    A definition whose declared targets are unknown or backward has no
    topological order to maximise over. It is refused by the compiler a moment
    later (R1/R2), and until then the SUM is returned: it is an
    over-approximation of any path's weight, so the ceilings can only refuse
    more, never less, and the fallback cannot admit something the graph
    analysis would have caught.
    """
    node_ids = [str(step.id) for step in steps]
    step_weights = {node_id: node_step_weight(step) for node_id, step in zip(node_ids, steps)}
    provider_weights = {
        node_id: node_provider_weight(step) for node_id, step in zip(node_ids, steps)
    }
    if not control_targets_are_forward_only(steps):
        return WorstCaseExpansion(
            expanded_steps=WorstCasePath(count=sum(step_weights.values()), path=tuple(node_ids)),
            expanded_provider_calls=WorstCasePath(
                count=sum(provider_weights.values()), path=tuple(node_ids)
            ),
        )
    successors = control_successors(resolve_control_edges(steps))
    return WorstCaseExpansion(
        expanded_steps=longest_weighted_path(node_ids, successors, step_weights),
        expanded_provider_calls=longest_weighted_path(node_ids, successors, provider_weights),
    )


def format_witness_path(path: Sequence[str]) -> str:
    """Render a witness path for a refusal message."""
    return " -> ".join(path) if path else "(empty)"


__all__ = [
    "FALLTHROUGH",
    "FALSE_ARM",
    "TRUE_ARM",
    "ProcedureGraph",
    "WorstCaseExpansion",
    "WorstCasePath",
    "build_procedure_graph",
    "control_successors",
    "control_targets_are_forward_only",
    "declared_control_targets",
    "format_witness_path",
    "has_path_avoiding",
    "longest_weighted_path",
    "node_provider_weight",
    "node_step_weight",
    "procedure_node_kind",
    "resolve_control_edges",
    "worst_case_expansion",
]
