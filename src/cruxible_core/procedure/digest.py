"""Definition and node digests.

Two identities, kept apart on purpose:

* the **definition digest** commits AUTHORED content only -- no pins. It is
  computed at proposal, before compilation, by a function with no instance and
  no lock, and the store recomputes it the same way. Folding acceptance-resolved
  pins into it would make it uncomputable at both of those sites.
* **per-node digests** come in two flavours. The *local* digest is one node's
  own authored content -- no successors, and NO CONTROL TARGETS. The *subtree*
  digest folds in the successors' subtree digests.

The control-target exclusion is what makes the arm-history guarantee true. If
`on_true`/`on_false` were part of a guard's local preimage, swapping the arms
would change the guard's identity, and every reading bound to that decision
point would detach from a node that still asks exactly the same question. The
two flavours answer two different questions cleanly: *what does this node ask?*
versus *what does this node ask and everything it can lead to?*
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cruxible_client.contracts.primitives import canonical_json
from cruxible_core.procedure.analysis import build_procedure_graph, procedure_node_kind
from cruxible_core.procedure.graph_format import (
    DEFINITION_FORMAT_V1,
    DEFINITION_FORMAT_V2,
    definition_format_version,
)
from cruxible_core.procedure.types import (
    ProcedureRepeatStepSchema,
    unwrap_procedure_step,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.procedure.types import ProcedureDefinition

_CONTROL_TARGET_FIELDS = frozenset({"on_true", "on_false", "next"})
"""Fields naming a SUCCESSOR.

They are topology, not this node's own content, and they are excluded from the
local preimage so that retargeting an edge does not change the identity of the
decision point it leaves.
"""

_ENVELOPE_EXEMPT_FIELDS = frozenset({"steps"})
"""Definition fields deliberately outside the virtual root's own content.

Exactly one entry: ``steps``, which the node digests already commit through the
root's successor.
"""


def _sha256(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Node digests
# --------------------------------------------------------------------------


def node_local_content(node: Any, *, body_root: str | None = None) -> dict[str, Any]:
    """Return the AUTHORED content of one node.

    No pins, no successors, no control targets. A flow wrapper contributes
    nothing of its own: its only fields are the wrapped step and the edge, so
    the node's content IS the wrapped step's content -- which is what makes a
    step identical before and after an edge is added to it.
    """
    inner = unwrap_procedure_step(node)
    dumped = inner.model_dump(mode="json", by_alias=True, exclude_none=True)
    spec = {key: value for key, value in dumped.items() if key not in _CONTROL_TARGET_FIELDS}
    if body_root is not None:
        # A repeat body is what the node DOES, not where control goes
        # afterwards, so it belongs in local content. R17 keeps that
        # unambiguous by forbidding graph nodes inside a body.
        spec["body_root"] = body_root
    return {"id": str(node.id), "kind": procedure_node_kind(node), "spec": spec}


def node_local_digest(node: Any, *, body_root: str | None = None) -> str:
    return _sha256(node_local_content(node, body_root=body_root))


def node_structural_digest(node: Any, *, body_root: str | None = None) -> str:
    """The local digest minus ``id``. RETRIEVAL ONLY -- never a subject.

    Two nodes that ask the same question under different names share this
    digest, which is what makes near-duplicate retrieval possible. It is not a
    provenance subject precisely because it cannot tell them apart.
    """
    content = node_local_content(node, body_root=body_root)
    spec = {key: value for key, value in content["spec"].items() if key != "id"}
    return _sha256({"kind": content["kind"], "spec": spec})


def node_subtree_digest(local: str, successors: dict[str, str]) -> str:
    """Fold successor subtree digests into one node's identity.

    ``successors`` is a MAP KEYED BY EDGE LABEL, not a list, so canonical JSON's
    key sort keeps it deterministic while an ``on_true``/``on_false`` swap stays
    visible.
    """
    return _sha256({"local": local, "successors": successors})


@dataclass(frozen=True)
class NodeDigests:
    """The three digests recorded for one node."""

    node_id: str
    kind: str
    local_digest: str
    subtree_digest: str
    structural_digest: str


def compute_node_digests(definition: ProcedureDefinition) -> dict[str, NodeDigests]:
    """Compute every node's digests in ONE reverse-topological pass, O(V+E).

    The step list is already a valid topological order because control edges
    are forward-only, so reversing it is the whole scheduling story.
    """
    graph = build_procedure_graph(definition)
    steps_by_id = {str(step.id): step for step in definition.steps}
    digests: dict[str, NodeDigests] = {}
    for node_id in reversed(graph.node_ids):
        step = steps_by_id[node_id]
        body_root = None
        inner = unwrap_procedure_step(step)
        if isinstance(inner, ProcedureRepeatStepSchema):
            nested = _nested_body_digests(node_id, inner)
            digests.update(nested)
            body_root = nested[f"{node_id}/{inner.repeat.steps[0].id}"].subtree_digest
        local = node_local_digest(step, body_root=body_root)
        successors = {
            label: (target if target == "$abort" else digests[target].subtree_digest)
            for label, target in graph.edges[node_id].items()
        }
        digests[node_id] = NodeDigests(
            node_id=node_id,
            kind=graph.kinds[node_id],
            local_digest=local,
            subtree_digest=node_subtree_digest(local, successors),
            structural_digest=node_structural_digest(step, body_root=body_root),
        )
    return digests


def _nested_body_digests(
    repeat_id: str,
    repeat_step: ProcedureRepeatStepSchema,
) -> dict[str, NodeDigests]:
    """Digest a repeat body as a linear sub-DAG under namespaced ids.

    Namespacing is what lets a reading bind to a node INSIDE a loop body
    without colliding with a same-named node outside it.
    """
    nested = list(repeat_step.repeat.steps)
    digests: dict[str, NodeDigests] = {}
    successor_digest: str | None = None
    for step in reversed(nested):
        namespaced = f"{repeat_id}/{step.id}"
        local = node_local_digest(step)
        successors = {} if successor_digest is None else {"next": successor_digest}
        subtree = node_subtree_digest(local, successors)
        digests[namespaced] = NodeDigests(
            node_id=namespaced,
            kind=procedure_node_kind(step),
            local_digest=local,
            subtree_digest=subtree,
            structural_digest=node_structural_digest(step),
        )
        successor_digest = subtree
    return digests


# --------------------------------------------------------------------------
# The virtual root
# --------------------------------------------------------------------------

_ENVELOPE_EXTENSIONS: list[tuple[str, Callable[[ProcedureDefinition], Any]]] = []


def register_envelope_field(key: str, accessor: Callable[[ProcedureDefinition], Any]) -> None:
    """Commit one more definition field to the virtual root.

    Registering a field CHANGES the v2 digest of any definition that sets it,
    which is correct only because registration happens in the same commit that
    declares the field -- no definition can have set it beforehand. Adding a
    field in one commit and registering it later would silently produce two
    digests for one definition.
    """
    _ENVELOPE_EXTENSIONS.append((key, accessor))


def registered_envelope_fields() -> tuple[str, ...]:
    return tuple(key for key, _accessor in _ENVELOPE_EXTENSIONS)


BASE_ENVELOPE_FIELDS: tuple[str, ...] = (
    "graph_format",
    "name",
    "description",
    "contract_in",
    "contract_out",
    "returns",
    "precondition",
    "budget",
    "declared_tier",
    "evidence_outputs",
)
"""Every definition field the virtual root commits directly.

v1's root was the entry NODE's digest, so `name`, the contracts, `returns`,
`precondition`, `budget`, `declared_tier` and `evidence_outputs` were all
outside the digest -- meaning a definition could change without its identity
changing, which is disqualifying for a settlement substrate.
"""


def definition_envelope(definition: ProcedureDefinition) -> dict[str, Any]:
    """Return the virtual root's own content."""
    envelope: dict[str, Any] = {
        "graph_format": definition.graph_format,
        "name": definition.name,
        "description": definition.description,
        "contract_in": _contract_ref_or_inline(definition.contract_in),
        "contract_out": _contract_ref_or_inline(definition.contract_out),
        "returns": definition.returns,
        "precondition": definition.precondition.model_dump(mode="json", exclude_none=True),
        "budget": definition.budget.model_dump(mode="json"),
        "declared_tier": definition.declared_tier,
        "evidence_outputs": definition.evidence_outputs,
    }
    for key, accessor in _ENVELOPE_EXTENSIONS:
        envelope[key] = accessor(definition)
    return envelope


def _contract_ref_or_inline(reference: Any) -> Any:
    if reference is None or isinstance(reference, str):
        return reference
    return reference.model_dump(mode="json", by_alias=True, exclude_none=True)


# --------------------------------------------------------------------------
# The versioned digest functions and their dispatcher
# --------------------------------------------------------------------------


def _compute_definition_digest_v1(definition: ProcedureDefinition) -> str:
    """FROZEN, byte for byte, forever.

    Its body may not change for any reason including refactors. It is archival
    infrastructure, not legacy debt: receipts outlive procedures, and a
    historical receipt's ``definition_digest`` must still resolve to a
    verifiable definition after the last v1 procedure is gone.
    """
    payload = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _compute_definition_digest_v2(definition: ProcedureDefinition) -> str:
    """The virtual root's subtree digest.

    The root is an ordinary node under the same construction as every other,
    so there is genuinely one machinery rather than a special case bolted on
    top of the node walk.
    """
    digests = compute_node_digests(definition)
    root_local = _sha256(
        {"id": "$root", "kind": "definition", "spec": definition_envelope(definition)}
    )
    entry_id = str(definition.steps[0].id)
    return node_subtree_digest(root_local, {"entry": digests[entry_id].subtree_digest})


DIGEST_FUNCTIONS: dict[int, Callable[[ProcedureDefinition], str]] = {
    DEFINITION_FORMAT_V1: _compute_definition_digest_v1,
    DEFINITION_FORMAT_V2: _compute_definition_digest_v2,
}
"""The reader-dispatch registry, keyed by format version.

The ``dict[int, ...]`` annotation is LOAD-BEARING, not documentation: it is
what makes mypy reject a tuple used as a key, which is exactly the mistake that
would otherwise turn every digest computation -- at five call sites, for both
formats -- into a KeyError.
"""


def compute_definition_digest(definition: ProcedureDefinition) -> str:
    """Return the stable content digest of one validated definition."""
    version, _warnings = definition_format_version(definition)
    return DIGEST_FUNCTIONS[version](definition)


__all__ = [
    "BASE_ENVELOPE_FIELDS",
    "DIGEST_FUNCTIONS",
    "NodeDigests",
    "compute_definition_digest",
    "compute_node_digests",
    "definition_envelope",
    "node_local_content",
    "node_local_digest",
    "node_structural_digest",
    "node_subtree_digest",
    "register_envelope_field",
    "registered_envelope_fields",
]
