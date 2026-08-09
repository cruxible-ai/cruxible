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

import math
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

from cruxible_core.config.schema import WorkflowStepSchema, workflow_step_kind
from cruxible_core.errors import ConfigError
from cruxible_core.predicate import (
    ComparisonOp,
    PredicateCoercionError,
    PredicateValueType,
    coerce_predicate_value,
    comparison_symbol,
    evaluate_typed_comparison,
    normalize_comparison_op,
)
from cruxible_core.procedure.branch_targets import BRANCH_TARGETABLE_KINDS
from cruxible_core.procedure.guards import GuardSpec, PredicateOperand, parse_predicate_operand
from cruxible_core.procedure.types import (
    ABORT_TARGET,
    MAX_PROCEDURE_ENUMERATED_PATHS,
    ProcedureBridgeStepSchema,
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
    if isinstance(node, ProcedureBridgeStepSchema):
        return "propose_group_from"
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
    """Resolve and verify the graph. Refuses R1–R3, R5, R6, and R15."""
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

    for step in steps:
        source_id = str(step.id)
        for label, target in declared_control_targets(step).items():
            if target == ABORT_TARGET:
                continue
            target_kind = kinds[target]
            if target_kind not in BRANCH_TARGETABLE_KINDS:  # R6
                raise ConfigError(
                    f"R6: procedure step '{source_id}' control edge '{label}' targets "
                    f"step '{target}' of non-targetable kind '{target_kind}'"
                )

    edges = resolve_control_edges(steps)
    for node_id, kind in kinds.items():
        if kind == "propose_group_from" and edges[node_id]:  # R5
            raise ConfigError(
                f"R5: procedure bridge step '{node_id}' must be terminal on its path "
                f"and declare no successor; resolved edges are {edges[node_id]}"
            )
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
# Analysis 7 -- path enumeration, DISPLAY ONLY (§3.1)
# ---------------------------------------------------------------------------


def enumerate_control_paths(
    graph: ProcedureGraph,
    *,
    cap: int = MAX_PROCEDURE_ENUMERATED_PATHS,
) -> tuple[tuple[tuple[str, ...], ...], bool]:
    """Return ``(paths, truncated)`` for the reviewer surfaces.

    The one analysis here that is exponential, and the one no correctness
    check may consult. Everything a refusal depends on runs in ``O(V+E)`` over
    the graph; this walks the paths themselves because a reviewer asked to
    authorise a branching definition is being asked about its BEHAVIOURS, and
    a topology is not a list of behaviours.

    Two independent bounds keep it finite: R11 caps branch nodes at twelve, and
    the cap here stops the walk. Truncation is REPORTED rather than silently
    absorbed -- a reviewer who is seeing part of the picture has to be told,
    and a surface that quietly showed the first 64 of 300 paths would be worse
    than one that showed none.

    Paths are enumerated in canonical successor order, so two calls on one
    definition return the same list in the same order.
    """
    if not graph.node_ids:
        return (), False
    paths: list[tuple[str, ...]] = []
    truncated = False
    stack: list[tuple[str, tuple[str, ...]]] = [(graph.entry_id, (graph.entry_id,))]
    while stack:
        node_id, prefix = stack.pop()
        successors = graph.successors_of(node_id)
        if not successors:
            if len(paths) >= cap:
                truncated = True
                break
            paths.append(prefix)
            continue
        # Reversed, because a LIFO stack pops last-pushed first: this makes the
        # walk visit `on_true` before `on_false` before `next`.
        for target in reversed(successors):
            stack.append((target, (*prefix, target)))
    return tuple(paths), truncated


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


# ---------------------------------------------------------------------------
# Analysis 5 -- vacuity / unsatisfiability, R9 (§3.5)
# ---------------------------------------------------------------------------
#
# CONNECTIVE-AWARE AND DELIBERATELY SHALLOW. The fragment narrows a domain only
# through CONJUNCTIVE dominating comparisons: a plain comparison, or a member
# of an `all_of`. `any_of` contributes nothing -- a disjunction narrows nothing
# -- and `not_of` contributes nothing, because negating a range over an open
# domain is not a range. No SMT. Anything the fragment cannot decide is NOT
# refused: fail-open on the analysis, fail-closed on the semantics. v1's
# "intersect dominating comparisons" was insufficient for exactly these two
# connectives and would have hard-refused valid definitions.


@dataclass(frozen=True)
class GuardConstraint:
    """One conjunctive comparison, normalised to ``<reference> <op> <literal>``."""

    key: str
    op: ComparisonOp
    value: Any
    source_node_id: str

    def rendered(self) -> str:
        return (
            f"{self.key} {comparison_symbol(self.op)} {self.value!r} (at '{self.source_node_id}')"
        )


@dataclass
class _Domain:
    """What the conjunction admits for one reference, as far as it can tell."""

    equals: set[Any] | None = None
    excluded: set[Any] = field(default_factory=set)
    lower: tuple[Any, bool] | None = None
    upper: tuple[Any, bool] | None = None
    granularities: set[str | None] = field(default_factory=set)
    """Per constraint: ``"int"``, ``"date"``, or ``None`` for continuous.

    Discreteness is CONTAGIOUS, not unanimous. A value satisfying an
    ``int``-declared comparison must survive integer coercion, so the whole
    conjunction's satisfying set is a subset of the integers as soon as ONE
    constraint declares it -- untyped bounds then apply within that set. A
    domain with no discrete constraint at all stays continuous, because
    untyped the payload may carry ``0.5``.
    """
    undecidable: bool = False
    """Set when two constraints on this domain could not be compared at all.

    The fragment then reports nothing about it. §3.5: what the analysis cannot
    decide is not refused.
    """
    witnesses: list[GuardConstraint] = field(default_factory=list)


@dataclass
class _EnumeratedDomain:
    """One reference whose contract declares a finite vocabulary.

    Keyed by REFERENCE ALONE, never by comparison class. The vocabulary is a
    property of the field, so every conjunctive comparison on it narrows the
    same set: a member that survives only the `int` comparison and one that
    survives only the `string` comparison leave nothing that survives both.
    Partitioning the candidates by class hid that and accepted a dead arm.

    This is also why the class partition remains right for OPEN references --
    there the fragment genuinely cannot say what the payload will carry, and
    an enumerated domain is exactly the case where it can.
    """

    candidates: set[Any]
    witnesses: list[GuardConstraint] = field(default_factory=list)


def conjunctive_comparisons(spec: GuardSpec) -> list[GuardSpec]:
    """Return the comparisons a predicate asserts UNCONDITIONALLY.

    ``all_of`` distributes, because every member must hold. ``any_of`` and
    ``not_of`` return nothing at all -- not "their children", nothing. A
    disjunct is not asserted, and a negated range is not a range.
    """
    if spec.all_of is not None:
        return [
            comparison for child in spec.all_of for comparison in conjunctive_comparisons(child)
        ]
    if spec.any_of is not None or spec.not_of is not None:
        return []
    return [spec] if spec.op is not None else []


def true_arm_dominators(graph: ProcedureGraph) -> dict[str, tuple[str, ...]]:
    """Per node, the guards whose TRUE arm every entry-to-node path traverses.

    Only the true arm contributes. The false arm asserts a NEGATION, and the
    fragment takes nothing from negations -- the same rule that excludes
    ``not_of``, applied to the same reasoning about open domains.

    A guard whose two arms converge on one node contributes nothing to that
    node either: control reaches it under the predicate and under its negation,
    so the predicate is not implied there.
    """
    dominators: dict[str, list[str]] = {node_id: [] for node_id in graph.node_ids}
    for guard_id in graph.node_ids:
        if graph.kinds[guard_id] != "guard":
            continue
        targets = graph.edges[guard_id]
        true_target = targets.get(TRUE_ARM)
        if true_target is None or true_target == ABORT_TARGET:
            continue
        if targets.get(FALSE_ARM) == true_target:
            continue
        reachable = _reachable_without_arm(graph, guard_id, TRUE_ARM)
        for node_id in graph.node_ids:
            if node_id not in reachable:
                dominators[node_id].append(guard_id)
    return {node_id: tuple(guards) for node_id, guards in dominators.items()}


def _reachable_without_arm(graph: ProcedureGraph, guard_id: str, arm: str) -> set[str]:
    """Nodes reachable from the entry when one labelled edge is cut."""
    seen = {graph.entry_id}
    queue = deque([graph.entry_id])
    while queue:
        node_id = queue.popleft()
        for label, target in graph.edges[node_id].items():
            if target == ABORT_TARGET or (node_id == guard_id and label == arm):
                continue
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def refuse_unsatisfiable_guards(
    graph: ProcedureGraph,
    definition: ProcedureDefinition,
    *,
    declared_input_enums: Mapping[str, frozenset[Any]] | None = None,
) -> None:
    """Refuse a guard no execution can satisfy (R9).

    A guard whose predicate cannot hold is a dead arm wearing the costume of a
    decision: the reviewer reads a three-way triage and the instance runs a
    two-way one, forever, with nothing anywhere saying so.
    """
    dominators = true_arm_dominators(graph)
    guards = {
        str(step.id): step
        for step in definition.steps
        if isinstance(step, ProcedureGuardStepSchema)
    }
    for node_id, guard in guards.items():
        asserted: list[tuple[str, GuardSpec]] = [
            (dominator, guards[dominator].guard)
            for dominator in dominators[node_id]
            if dominator in guards
        ]
        asserted.append((node_id, guard.guard))
        _refuse_empty_domain(
            node_id,
            asserted,
            declared_input_enums=declared_input_enums or {},
        )


def _refuse_empty_domain(
    node_id: str,
    asserted: Sequence[tuple[str, GuardSpec]],
    *,
    declared_input_enums: Mapping[str, frozenset[Any]],
) -> None:
    domains: dict[tuple[str, str], _Domain] = {}
    enumerated: dict[str, _EnumeratedDomain] = {}
    for source_node_id, spec in asserted:
        for comparison in conjunctive_comparisons(spec):
            _apply_comparison(
                comparison,
                source_node_id=source_node_id,
                node_id=node_id,
                domains=domains,
                enumerated=enumerated,
                declared_input_enums=declared_input_enums,
            )
    # ENUMERATED references first: their verdict is exact, and it is the one a
    # reviewer can check by hand against the vocabulary.
    for key, enum_domain in sorted(enumerated.items()):
        if enum_domain.candidates:
            continue
        _refuse_empty(node_id, key, enum_domain.witnesses)
    for (key, _value_class), domain in sorted(domains.items()):
        if _domain_is_empty(domain):
            _refuse_empty(node_id, key, domain.witnesses)


def _refuse_empty(node_id: str, key: str, witnesses: Sequence[GuardConstraint]) -> NoReturn:
    cited = "; ".join(constraint.rendered() for constraint in witnesses)
    raise ConfigError(  # R9
        f"procedure guard step '{node_id}' is statically unsatisfiable: no value of "
        f"{key} satisfies every comparison that must hold on the paths reaching it "
        f"[{cited}]. A predicate that cannot hold is a branch that never runs."
    )


def _apply_comparison(
    comparison: GuardSpec,
    *,
    source_node_id: str,
    node_id: str,
    domains: dict[tuple[str, str], _Domain],
    enumerated: dict[str, _EnumeratedDomain],
    declared_input_enums: Mapping[str, frozenset[Any]],
) -> None:
    left = parse_predicate_operand(comparison.left)
    right = parse_predicate_operand(comparison.right)
    if left.form == "param" or right.form == "param":
        # A governed parameter's value is not known here. Narrowing on an
        # unknown would refuse definitions whose values are perfectly
        # satisfiable, so the comparison contributes nothing.
        return
    assert comparison.op is not None
    op = normalize_comparison_op(comparison.op)

    if left.form == "literal" and right.form == "literal":
        _refuse_constant_false(comparison, op, node_id=node_id, source_node_id=source_node_id)
        return

    key = _operand_key(left)
    literal = right.literal if right.form == "literal" else None
    if key is None or right.form != "literal":
        key = _operand_key(right)
        literal = left.literal if left.form == "literal" else None
        if key is None or left.form != "literal":
            # Reference against reference: two unknowns, nothing to intersect.
            return
        op = _flip(op)

    value = _coerced(literal, comparison.value_type)
    if value is _UNCOERCIBLE:
        return

    constraint = GuardConstraint(key=key, op=op, value=value, source_node_id=source_node_id)

    # AN ENUMERATED REFERENCE HAS ONE CANDIDATE SET, NOT ONE PER CLASS. The
    # vocabulary is a property of the reference, so every conjunctive
    # comparison on it narrows the SAME set whatever class its literal is --
    # a member surviving only the int comparison and a member surviving only
    # the string one leave nothing that survives both, and partitioning the
    # candidates by class hid exactly that.
    declared = _seeded_enum(key, declared_input_enums)
    if declared is not None:
        enum_domain = enumerated.get(key)
        if enum_domain is None:
            enum_domain = _EnumeratedDomain(candidates=declared)
            enumerated[key] = enum_domain
        enum_domain.witnesses.append(constraint)
        enum_domain.candidates = _surviving_members(
            enum_domain.candidates, op, literal, comparison.value_type
        )

    # ONE OPEN DOMAIN PER (reference, comparison class). With no declared
    # vocabulary the fragment cannot say what the payload carries, so two
    # comparisons whose literals are of different classes -- `eq "1"` and
    # `eq 1` -- narrow NOTHING about each other and intersecting them would
    # invent a contradiction. Keeping them apart is the §3.5 fail-open rule
    # applied to types, and it is also what keeps a `date` bound from ever
    # being compared against a `datetime` one.
    domain_key = (key, _comparison_class(value))
    domain = domains.get(domain_key)
    if domain is None:
        domain = _Domain()
        domains[domain_key] = domain
    domain.witnesses.append(constraint)
    domain.granularities.add(_granularity(comparison.value_type, value))
    _narrow(domain, op, value)


def _surviving_members(
    candidates: set[Any],
    op: ComparisonOp,
    literal: Any,
    value_type: PredicateValueType | None,
) -> set[Any]:
    """Narrow a declared vocabulary by RUNNING the comparison over it.

    The enum is the finite runtime domain, so this is not an approximation of
    the predicate -- it is the predicate, evaluated by the same function the
    executor uses, over every value a caller is permitted to send. That makes
    a cross-class comparison DECIDABLE rather than undecidable: a numeric test
    against a string vocabulary is false for `low`, false for `medium` and
    false for `high`, and a guard false on every admissible input is exactly
    what R9 exists to refuse.
    """
    surviving: set[Any] = set()
    for member in candidates:
        try:
            satisfied = evaluate_typed_comparison(member, op, literal, value_type=value_type)
        except (TypeError, ValueError):
            # Undecidable for this member: keep it. §3.5 fail-open.
            surviving.add(member)
            continue
        if satisfied:
            surviving.add(member)
    return surviving


def _refuse_constant_false(
    comparison: GuardSpec,
    op: ComparisonOp,
    *,
    node_id: str,
    source_node_id: str,
) -> None:
    """A comparison of two literals is a constant, and half of them are false.

    The degenerate empty domain: no reference to narrow, and the answer is
    already known. It sits inside R9 rather than beside it because the defect
    is identical -- a branch whose predicate no execution can satisfy.
    """
    if evaluate_typed_comparison(
        comparison.left,
        op,
        comparison.right,
        value_type=comparison.value_type,
    ):
        return
    raise ConfigError(  # R9
        f"procedure guard step '{node_id}' is statically unsatisfiable: the comparison "
        f"{comparison.left!r} {comparison_symbol(op)} {comparison.right!r} (at "
        f"'{source_node_id}') compares two literals and is constantly false."
    )


_UNCOERCIBLE = object()


def _coerced(value: Any, value_type: PredicateValueType | None) -> Any:
    if value_type is None:
        return value
    try:
        return coerce_predicate_value(value, value_type)
    except PredicateCoercionError:
        # A literal the declared type rejects is the typed-comparison layer's
        # business, not this one's.
        return _UNCOERCIBLE


def _comparison_class(value: Any) -> str:
    """The class within which two literals are meaningfully comparable.

    ``bool`` is its own class despite being an ``int`` subclass, and ``date``
    is kept apart from ``datetime`` -- Python refuses to order the two, which
    is how an unguarded bound comparison became a TypeError escaping the
    analysis rather than a verdict.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


def _granularity(value_type: PredicateValueType | None, value: Any) -> str | None:
    """The step size the DECLARED type imposes, or ``None`` for continuous.

    Read off the declaration, not the literal: ``n > 0`` with no declared type
    admits ``0.5``, so its bound is continuous even though ``0`` is an int.
    Only an explicit ``int``/``integer``/``date`` narrows the domain to
    representable steps.
    """
    if value_type in {"int", "integer"} and isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if value_type == "date" and isinstance(value, date) and not isinstance(value, datetime):
        return "date"
    return None


def _seeded_enum(
    key: str,
    declared_input_enums: Mapping[str, frozenset[Any]],
) -> set[Any] | None:
    """Seed a domain with the vocabulary the CONTRACT declares, if any.

    This is the "declared enum sets" half of §3.5: a guard demanding a value
    the contract's own enumeration cannot carry can never hold, whatever the
    caller sends.

    Seeded WHOLE, every member regardless of class. Narrowing is
    :func:`_filter_enum_candidates`'s job and it does it by evaluation, which
    is exact over a finite domain -- class filtering here would throw away
    members before anyone asked whether they satisfy the comparison.
    """
    declared = declared_input_enums.get(key)
    return None if declared is None else set(declared)


def _narrow(domain: _Domain, op: ComparisonOp, value: Any) -> None:
    if op == "eq":
        domain.equals = {value} if domain.equals is None else domain.equals & {value}
        return
    if op == "ne":
        domain.excluded.add(value)
        return
    if not _is_orderable(value):
        # Only numeric and temporal intervals are intersected (§3.5). A
        # lexicographic bound on a string is not what an author writing
        # `tier > "gold"` means, and refusing on one would be a guess.
        return
    if op in {"gt", "gte"}:
        candidate = (value, op == "gte")
        tighter = _tighter_lower(candidate, domain.lower, domain)
        if tighter:
            domain.lower = candidate
        return
    candidate = (value, op == "lte")
    if _tighter_upper(candidate, domain.upper, domain):
        domain.upper = candidate


def _tighter_lower(
    candidate: tuple[Any, bool],
    current: tuple[Any, bool] | None,
    domain: _Domain,
) -> bool:
    if current is None:
        return True
    try:
        if candidate[0] != current[0]:
            return bool(candidate[0] > current[0])
    except TypeError:
        # Two bounds that cannot be ordered against each other. The fragment
        # has nothing to say about this domain, and saying it anyway is how a
        # comparison error escapes an advisory analysis as a crash.
        domain.undecidable = True
        return False
    return not candidate[1] and current[1]


def _tighter_upper(
    candidate: tuple[Any, bool],
    current: tuple[Any, bool] | None,
    domain: _Domain,
) -> bool:
    if current is None:
        return True
    try:
        if candidate[0] != current[0]:
            return bool(candidate[0] < current[0])
    except TypeError:
        domain.undecidable = True
        return False
    return not candidate[1] and current[1]


def _is_orderable(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float | date | datetime)


def _discrete_step(domain: _Domain) -> str | None:
    """The domain's step size. Discreteness is CONTAGIOUS, not unanimous.

    A value satisfying an ``int``-declared comparison has to survive integer
    coercion -- a non-integral payload makes that comparison false, not true
    -- so ONE such constraint confines the whole conjunction's satisfying set
    to the integers, and every other bound on the domain, declared or not,
    then applies within that set. Requiring unanimity accepted
    ``0 < n (int)`` with ``n < 1`` (untyped): 0.5 fails the first, every
    positive integer fails the second, and no runtime witness exists.

    A domain with no discrete constraint at all stays continuous.
    """
    if "int" in domain.granularities:
        return "int"
    if "date" in domain.granularities:
        return "date"
    return None


def _closed_bounds(domain: _Domain) -> tuple[Any, Any] | None:
    """Normalise the interval to INCLUSIVE bounds, granularity respected.

    On a discrete domain every bound has a nearest representable neighbour, so
    ``0 < n < 1`` over the integers closes to ``[1, 0]`` -- and an interval
    whose lower bound exceeds its upper is empty. Reasoning continuously
    accepted it, and the arm was dead in every run.

    Bounds are floored and ceiled rather than nudged by one, because contagion
    admits a NON-INTEGRAL bound into an integer domain: ``0.5 < n (float)``
    with ``n < 5 (int)`` is satisfied by 1 through 4, and ``0.5 + 1`` would
    have moved the lower bound to 1.5 and lost the smallest witness.
    """
    if domain.lower is None or domain.upper is None:
        return None
    low, low_inclusive = domain.lower
    high, high_inclusive = domain.upper
    step = _discrete_step(domain)
    try:
        if step == "int":
            return (
                math.ceil(low) if low_inclusive else math.floor(low) + 1,
                math.floor(high) if high_inclusive else math.ceil(high) - 1,
            )
        if step == "date":
            day = timedelta(days=1)
            return (low if low_inclusive else low + day, high if high_inclusive else high - day)
    except (TypeError, ValueError, OverflowError):
        # A bound the step size cannot be applied to says nothing decidable.
        return None
    return None


def _domain_is_empty(domain: _Domain) -> bool:
    if domain.undecidable:
        return False
    if domain.equals is not None:
        return not any(
            candidate not in domain.excluded and _within_bounds(candidate, domain)
            for candidate in domain.equals
        )
    closed = _closed_bounds(domain)
    if closed is not None:
        try:
            return bool(closed[0] > closed[1])
        except TypeError:
            return False
    if domain.lower is None or domain.upper is None:
        return False
    low, low_inclusive = domain.lower
    high, high_inclusive = domain.upper
    try:
        if low > high:
            return True
        return bool(low == high and not (low_inclusive and high_inclusive))
    except TypeError:
        return False


def _within_bounds(candidate: Any, domain: _Domain) -> bool:
    """Fail OPEN on anything incomparable: an unknown is not a contradiction."""
    for bound, is_lower in ((domain.lower, True), (domain.upper, False)):
        if bound is None:
            continue
        value, inclusive = bound
        if not _is_orderable(candidate) or not _is_orderable(value):
            continue
        try:
            if is_lower and not (candidate >= value if inclusive else candidate > value):
                return False
            if not is_lower and not (candidate <= value if inclusive else candidate < value):
                return False
        except TypeError:
            continue
    return True


def _flip(op: ComparisonOp) -> ComparisonOp:
    """Mirror a comparison so the reference is always on the left."""
    mirrored: dict[ComparisonOp, ComparisonOp] = {
        "gt": "lt",
        "gte": "lte",
        "lt": "gt",
        "lte": "gte",
        "eq": "eq",
        "ne": "ne",
    }
    return mirrored[op]


def _operand_key(operand: PredicateOperand) -> str | None:
    """Canonical name for the value an operand reads, or ``None`` for a literal.

    Two comparisons narrow ONE domain only when they name the same thing, so
    the key has to be the operand's whole reference -- ``$steps.rows.score``
    and ``$steps.rows.count`` are different values and intersecting them would
    invent a contradiction.
    """
    if operand.form == "input_path":
        return "$input" if operand.path is None else f"$input.{operand.path}"
    if operand.form == "steps_path":
        tail = f".{operand.path}" if operand.path else ""
        return f"$steps.{operand.alias}{tail}"
    if operand.form == "count":
        return f"count({operand.alias}, {operand.selector})"
    if operand.form == "truncated":
        return f"truncated({operand.alias})"
    if operand.form == "exists":
        return f"exists({operand.ref})"
    return None


__all__ = [
    "FALLTHROUGH",
    "FALSE_ARM",
    "TRUE_ARM",
    "GuardConstraint",
    "ProcedureGraph",
    "WorstCaseExpansion",
    "WorstCasePath",
    "build_procedure_graph",
    "control_successors",
    "control_targets_are_forward_only",
    "declared_control_targets",
    "enumerate_control_paths",
    "format_witness_path",
    "conjunctive_comparisons",
    "has_path_avoiding",
    "longest_weighted_path",
    "node_provider_weight",
    "node_step_weight",
    "procedure_node_kind",
    "refuse_unsatisfiable_guards",
    "resolve_control_edges",
    "true_arm_dominators",
    "worst_case_expansion",
]
