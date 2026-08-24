"""Graph-format-v3 static analysis and domain-separated Merkle digests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedures.models import (
    TERMINAL_NODE_KINDS,
    TERMINAL_REQUIRED_RUNGS,
    CaptureEgressNodeV3,
    GuardNodeV3,
    InboxEgressNodeV3,
    MandateSettlementNodeV3,
    ProcedureDefinitionV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    ProviderNodeV3,
    RepeatNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
    iter_pin_bindings,
)

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class ProcedureGraphFormatError(PlaybillFormatError):
    """A graph-format-v3 definition fails a static law."""


@dataclass(frozen=True)
class ProcedureGraphV3:
    node_ids: tuple[str, ...]
    kinds: dict[str, str]
    edges: dict[str, dict[str, str]]
    successors: dict[str, tuple[str, ...]]
    predecessors: dict[str, tuple[str, ...]]
    available_aliases: dict[str, frozenset[str]]
    """Aliases produced on every path reaching each node (MUST)."""
    reachable_aliases: dict[str, frozenset[str]]
    """Aliases produced on at least one path reaching each node (MAY)."""
    produced_alias: dict[str, str | None]


@dataclass(frozen=True)
class ProcedureNodeDigestsV3:
    node_id: str
    kind: str
    local_digest: str
    subtree_digest: str


def _declared_edges(node: ProcedureNodeV3) -> dict[str, str]:
    if isinstance(node, GuardNodeV3):
        edges: dict[str, str] = {"on_false": node.on_false}
        if node.on_true is not None:
            edges["on_true"] = node.on_true
        return edges
    target = getattr(node, "next", None)
    return {} if target is None else {"next": str(target)}


def _node_alias(node: ProcedureNodeV3) -> str | None:
    value = getattr(node, "as_", None)
    return value if isinstance(value, str) else None


def _canonical_edges(edges: dict[str, str]) -> dict[str, str]:
    return {key: edges[key] for key in sorted(edges, key=lambda item: item.encode("utf-8"))}


def _successors(edges: dict[str, str]) -> tuple[str, ...]:
    return tuple(target for target in edges.values() if target != "$abort")


def _reference_templates(node: ProcedureNodeV3) -> Iterator[tuple[str, object]]:
    """Yield only fields whose values the v3 runtime resolves as references."""

    if isinstance(node, StateTapNodeV3):
        yield "parameters", node.parameters
    elif isinstance(node, SourceNodeV3):
        yield "request", node.request
    elif isinstance(node, ProviderNodeV3):
        yield "input", node.input
    elif isinstance(node, TransformNodeV3):
        yield "spec", node.spec
    elif isinstance(node, ProjectNodeV3):
        yield "fields", node.fields
    elif isinstance(node, RepeatNodeV3):
        return
    elif isinstance(node, CaptureEgressNodeV3 | InboxEgressNodeV3):
        yield "input", node.input
    elif isinstance(node, ProposeChangeSetNodeV3):
        yield "candidate_templates", node.candidate_templates
    elif isinstance(node, MandateSettlementNodeV3):
        yield "input", node.input


def _step_alias_references(value: object, *, location: str) -> Iterator[str]:
    """Extract structured ``$steps.<alias>`` references from canonical values."""

    if isinstance(value, str):
        if value == "$steps" or value.startswith("$steps."):
            if not value.startswith("$steps."):
                raise ProcedureGraphFormatError(
                    f"{location} contains malformed step reference {value!r}"
                )
            remainder = value[len("$steps.") :]
            alias = remainder.split(".", 1)[0]
            if not _ALIAS_RE.fullmatch(alias):
                raise ProcedureGraphFormatError(
                    f"{location} contains malformed step reference {value!r}"
                )
            yield alias
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _step_alias_references(item, location=location)
        return
    if isinstance(value, tuple | list):
        for item in value:
            yield from _step_alias_references(item, location=location)


def _alias_dataflow(
    definition: ProcedureDefinitionV3,
    *,
    node_ids: tuple[str, ...],
    predecessors: dict[str, tuple[str, ...]],
) -> tuple[
    dict[str, str | None],
    dict[str, frozenset[str]],
    dict[str, frozenset[str]],
]:
    """Compute distinct MUST-availability and MAY-reachability sets."""

    produced = {node.node_id: _node_alias(node) for node in definition.nodes}
    available: dict[str, frozenset[str]] = {}
    reachable: dict[str, frozenset[str]] = {}

    def after(node_id: str, aliases: frozenset[str]) -> frozenset[str]:
        alias = produced[node_id]
        return aliases if alias is None else aliases | frozenset({alias})

    for position, node_id in enumerate(node_ids):
        preds = predecessors[node_id]
        if position == 0 or not preds:
            available[node_id] = frozenset()
            reachable[node_id] = frozenset()
            continue
        must_contributions = [after(pred, available[pred]) for pred in preds]
        available[node_id] = frozenset.intersection(*must_contributions)
        reachable[node_id] = frozenset().union(*[after(pred, reachable[pred]) for pred in preds])
    return produced, available, reachable


def _validate_node_references(
    node: ProcedureNodeV3,
    *,
    available: frozenset[str],
) -> None:
    if isinstance(node, GuardNodeV3):
        missing = set(node.predicate.step_aliases()) - available
        if missing:
            raise ProcedureGraphFormatError(
                f"R15: guard {node.node_id!r} references aliases not produced on every "
                f"path reaching it: {sorted(missing)}"
            )

    for field_name, template in _reference_templates(node):
        location = f"Procedure node {node.node_id!r} field {field_name!r}"
        missing = set(_step_alias_references(template, location=location)) - available
        if missing:
            raise ProcedureGraphFormatError(
                f"R15: {location} references aliases not produced on every path "
                f"reaching it: {sorted(missing)}"
            )

    if not isinstance(node, RepeatNodeV3):
        return
    body_available = set(available)
    for body in node.body:
        location = f"Procedure repeat {node.node_id!r} body {body.node_id!r} field 'spec'"
        missing = set(_step_alias_references(body.spec, location=location)) - body_available
        if missing:
            raise ProcedureGraphFormatError(
                f"R15: {location} references unavailable aliases: {sorted(missing)}"
            )
        body_available.add(body.as_)
    body_aliases = {body.as_ for body in node.body}
    missing = set(node.until.step_aliases()) - body_aliases
    if missing:
        raise ProcedureGraphFormatError(
            f"repeat {node.node_id!r} predicate references aliases outside its body: "
            f"{sorted(missing)}"
        )


def analyze_procedure_v3(definition: ProcedureDefinitionV3) -> ProcedureGraphV3:
    """Enforce v3's forward-only, reachable, plane-typed graph."""

    node_ids = tuple(node.node_id for node in definition.nodes)
    position = {node_id: index for index, node_id in enumerate(node_ids)}
    kinds: dict[str, str] = {node.node_id: node.kind for node in definition.nodes}
    edges: dict[str, dict[str, str]] = {}
    declared_slots = {slot.slot_name for slot in definition.pin_slots}

    for index, node in enumerate(definition.nodes):
        declared = _declared_edges(node)
        fallthrough = node_ids[index + 1] if index + 1 < len(node_ids) else None
        if isinstance(node, GuardNodeV3) and "on_true" not in declared and fallthrough:
            declared["on_true"] = fallthrough
        elif (
            node.kind not in TERMINAL_NODE_KINDS
            and not isinstance(node, GuardNodeV3)
            and "next" not in declared
            and fallthrough
        ):
            declared["next"] = fallthrough
        if node.kind in TERMINAL_NODE_KINDS and declared:
            raise ProcedureGraphFormatError(
                f"R5: terminal Procedure node {node.node_id!r} cannot have successors"
            )
        required_rung = TERMINAL_REQUIRED_RUNGS.get(node.kind)
        if required_rung is not None and required_rung > definition.terminal_capability:
            raise ProcedureGraphFormatError(
                f"Procedure terminal {node.node_id!r} kind {node.kind!r} requires rung "
                f"{required_rung}, above declared terminal_capability "
                f"{definition.terminal_capability}"
            )
        for label, target in declared.items():
            if target == "$abort":
                continue
            if target not in position:
                raise ProcedureGraphFormatError(
                    f"R1: node {node.node_id!r} {label} names unknown target {target!r}"
                )
            if position[target] <= index:
                raise ProcedureGraphFormatError(
                    f"R2: node {node.node_id!r} {label} must target a later node"
                )
        for binding in iter_pin_bindings(node):
            if (
                isinstance(binding, ProcedurePinSlotRefV1)
                and binding.slot_name not in declared_slots
            ):
                raise ProcedureGraphFormatError(
                    f"Procedure node {node.node_id!r} references undeclared pin slot "
                    f"{binding.slot_name!r}"
                )
        edges[node.node_id] = _canonical_edges(declared)

    reachable: set[str] = set()
    pending = [node_ids[0]]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(target for target in edges[current].values() if target != "$abort")
    missing_nodes = tuple(node_id for node_id in node_ids if node_id not in reachable)
    if missing_nodes:
        raise ProcedureGraphFormatError(f"R3: unreachable Procedure nodes: {missing_nodes}")

    successors = {node_id: _successors(edges[node_id]) for node_id in node_ids}
    predecessor_lists: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node_id, targets in successors.items():
        for target in targets:
            predecessor_lists[target].append(node_id)
    predecessors = {node_id: tuple(values) for node_id, values in predecessor_lists.items()}
    produced, available, reachable_aliases = _alias_dataflow(
        definition,
        node_ids=node_ids,
        predecessors=predecessors,
    )

    for node in definition.nodes:
        _validate_node_references(node, available=available[node.node_id])

    for node in definition.nodes:
        if edges[node.node_id]:
            continue
        alias = _node_alias(node)
        if node.kind not in TERMINAL_NODE_KINDS and alias != definition.returns:
            raise ProcedureGraphFormatError(
                f"Procedure leaf {node.node_id!r} neither emits typed egress nor returns "
                f"the declared output alias {definition.returns!r}"
            )

    return ProcedureGraphV3(
        node_ids=node_ids,
        kinds=kinds,
        edges=edges,
        successors=successors,
        predecessors=predecessors,
        available_aliases=available,
        reachable_aliases=reachable_aliases,
        produced_alias=produced,
    )


def _node_local_payload(node: ProcedureNodeV3) -> dict[str, object]:
    payload = node.model_dump(mode="json", by_alias=True)
    payload.pop("node_id")
    payload.pop("next", None)
    payload.pop("on_true", None)
    payload.pop("on_false", None)
    return {"kind": node.kind, "spec": payload}


def compute_procedure_node_digests_v3(
    definition: ProcedureDefinitionV3,
) -> dict[str, ProcedureNodeDigestsV3]:
    graph = analyze_procedure_v3(definition)
    nodes = {node.node_id: node for node in definition.nodes}
    result: dict[str, ProcedureNodeDigestsV3] = {}
    for node_id in reversed(graph.node_ids):
        node = nodes[node_id]
        local = typed_digest(
            ArtifactDigest,
            "playbill-procedure-node-local-v3",
            _node_local_payload(node),
        ).tagged
        successor_digests = {
            label: (target if target == "$abort" else result[target].subtree_digest)
            for label, target in graph.edges[node_id].items()
        }
        subtree = typed_digest(
            ArtifactDigest,
            "playbill-procedure-node-subtree-v3",
            {"local_digest": local, "successors": successor_digests},
        ).tagged
        result[node_id] = ProcedureNodeDigestsV3(
            node_id=node_id,
            kind=node.kind,
            local_digest=local,
            subtree_digest=subtree,
        )
    return result


def compute_procedure_definition_digest_v3(definition: ProcedureDefinitionV3) -> ArtifactDigest:
    """Commit the envelope and entry subtree without altering v1/v2 functions."""

    node_digests = compute_procedure_node_digests_v3(definition)
    payload = definition.model_dump(mode="json", by_alias=True)
    payload.pop("nodes")
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-definition-v3",
        {
            "definition": payload,
            "entry_node_id": definition.nodes[0].node_id,
            "entry_subtree_digest": node_digests[definition.nodes[0].node_id].subtree_digest,
        },
    )


__all__ = [
    "ProcedureGraphFormatError",
    "ProcedureGraphV3",
    "ProcedureNodeDigestsV3",
    "analyze_procedure_v3",
    "compute_procedure_definition_digest_v3",
    "compute_procedure_node_digests_v3",
]
