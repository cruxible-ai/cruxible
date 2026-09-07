"""Bounded per-process memo for Procedure node digest vectors.

A definition's node-local and subtree digests are a pure function of the
definition bytes, which the definition digest already names. Readings,
activations, and grain checks each asked the graph to recompute that vector
for every call; at one reading per run that is a full graph walk per credit.
This memo keys the vector on the definition digest, so the first caller pays
and every later reading of the same accepted revision reuses it.

It is a cache and nothing else: cold after a restart, bounded, and keyed on
the exact digest so a different revision can never be served another's
vector. Callers that hold no digest, or hold a definition whose digest they
cannot vouch for, keep calling the graph directly.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping

from cruxible_client.contracts.procedures.graph import (
    ProcedureNodeDigestsV3,
    compute_procedure_node_digests_v3,
    compute_procedure_node_digests_v4,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinitionV3,
    ProcedureDefinitionV4,
)

NODE_DIGEST_MEMO_CAPACITY = 256

_memo: OrderedDict[str, Mapping[str, ProcedureNodeDigestsV3]] = OrderedDict()


def compute_node_digests(
    definition: ProcedureDefinitionV3 | ProcedureDefinitionV4,
) -> Mapping[str, ProcedureNodeDigestsV3]:
    """Compute the vector directly, exactly as the graph law does."""

    if definition.graph_format == 3:
        return compute_procedure_node_digests_v3(definition)
    return compute_procedure_node_digests_v4(definition)


def cached_node_digests(
    definition: ProcedureDefinitionV3 | ProcedureDefinitionV4,
    *,
    definition_digest: str,
) -> Mapping[str, ProcedureNodeDigestsV3]:
    """Return the node digest vector for one exact accepted definition digest."""

    cached = _memo.get(definition_digest)
    if cached is not None:
        _memo.move_to_end(definition_digest)
        return cached
    computed = compute_node_digests(definition)
    _memo[definition_digest] = computed
    while len(_memo) > NODE_DIGEST_MEMO_CAPACITY:
        _memo.popitem(last=False)
    return computed


def clear_node_digest_memo() -> None:
    _memo.clear()


__all__ = [
    "NODE_DIGEST_MEMO_CAPACITY",
    "cached_node_digests",
    "clear_node_digest_memo",
    "compute_node_digests",
]
