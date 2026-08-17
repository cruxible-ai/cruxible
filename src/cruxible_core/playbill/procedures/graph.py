"""Graph-format-v3 static analysis and domain-separated Merkle digests."""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_core.playbill.canonical import ArtifactDigest, typed_digest
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.procedures.models import (
    TERMINAL_NODE_KINDS,
    GuardNodeV3,
    ProcedureDefinitionV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    RepeatNodeV3,
    iter_pin_bindings,
)


class ProcedureGraphFormatError(PlaybillFormatError):
    """A graph-format-v3 definition fails a static law."""


@dataclass(frozen=True)
class ProcedureGraphV3:
    node_ids: tuple[str, ...]
    kinds: dict[str, str]
    edges: dict[str, dict[str, str]]


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


def analyze_procedure_v3(definition: ProcedureDefinitionV3) -> ProcedureGraphV3:
    """Enforce v3's forward-only, reachable, plane-typed graph."""

    node_ids = tuple(node.node_id for node in definition.nodes)
    position = {node_id: index for index, node_id in enumerate(node_ids)}
    kinds: dict[str, str] = {node.node_id: node.kind for node in definition.nodes}
    edges: dict[str, dict[str, str]] = {}
    available_aliases: set[str] = set()
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
        if isinstance(node, GuardNodeV3):
            missing = set(node.predicate.step_aliases()) - available_aliases
            if missing:
                raise ProcedureGraphFormatError(
                    f"R15: guard {node.node_id!r} references unavailable aliases {sorted(missing)}"
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
        if isinstance(node, RepeatNodeV3):
            body_aliases = {body.as_ for body in node.body}
            missing = set(node.until.step_aliases()) - body_aliases
            if missing:
                raise ProcedureGraphFormatError(
                    f"repeat {node.node_id!r} predicate references aliases outside its body: "
                    f"{sorted(missing)}"
                )
        alias = _node_alias(node)
        if alias is not None:
            available_aliases.add(alias)
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

    for node in definition.nodes:
        if edges[node.node_id]:
            continue
        alias = _node_alias(node)
        if node.kind not in TERMINAL_NODE_KINDS and alias != definition.returns:
            raise ProcedureGraphFormatError(
                f"Procedure leaf {node.node_id!r} neither emits typed egress nor returns "
                f"the declared output alias {definition.returns!r}"
            )

    return ProcedureGraphV3(node_ids=node_ids, kinds=kinds, edges=edges)


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
