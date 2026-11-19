"""One keyset page of the Subject graph, read from the bound projection's indexes.

Nodes are the live Subjects, walked kind by kind through
``subjects_by_kind_lifecycle_path`` (one covering seek per kind the page
touches). Edges are the live Claims whose object is a Subject, walked through
the partial object index ``claims_by_object_subject`` by (target path, Claim
identity). A page costs its own rows, one more to detect truncation, and the
per-kind Subject counters (vocabulary-sized): never a scan of the whole
population, a blob, a body or the history.

A Claim's contested or conflicted status is not on the edge: it is not in the
projection tables, only in the ``next`` facts' resolution statuses, which a
graph page does not read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.orient import (
    OrientGraph,
    OrientGraphEdge,
    OrientGraphNode,
    OrientGraphTotals,
)

#: Each live Subject kind with its live count, from the counters Subject writes maintain.
GRAPH_KINDS_SQL = (
    "SELECT subject_kind, amount FROM query_subject_counts "
    "WHERE lifecycle='live' ORDER BY subject_kind"
)
#: One kind's live Subjects after a path: a covering seek of the kind/lifecycle index.
GRAPH_NODES_SQL = (
    "SELECT path FROM subjects INDEXED BY subjects_by_kind_lifecycle_path "
    "WHERE subject_kind=? AND lifecycle='live' AND path>? ORDER BY path LIMIT ?"
)
#: Live Subject-valued Claims after a (target path, Claim) key: a seek of the object index.
GRAPH_EDGES_SQL = (
    "SELECT identity, subject_path, predicate, qualifier, object_path FROM claims "
    "INDEXED BY claims_by_object_subject "
    "WHERE object_kind='subject' AND (object_path, identity)>(?, ?) AND lifecycle='live' "
    "ORDER BY object_path, identity LIMIT ?"
)

GraphPhase = Literal["node", "edge"]


@dataclass(frozen=True)
class GraphKey:
    """Where a page stopped: the last node's (kind, path) or the last edge's (target, Claim)."""

    phase: GraphPhase
    first: str
    second: str

    def encode(self) -> list[str]:
        return [self.phase, self.first, self.second]

    @classmethod
    def decode(cls, last_key: tuple[str, ...]) -> GraphKey | None:
        if len(last_key) != 3 or last_key[0] not in ("node", "edge"):
            return None
        phase: GraphPhase = "node" if last_key[0] == "node" else "edge"
        return cls(phase, last_key[1], last_key[2])


def subject_ref(path: str) -> str:
    """``subjects/<kind>/<id>.json`` as the ``kind/id`` ref ``get`` reads."""

    if path.startswith("subjects/") and path.endswith(".json"):
        return path[len("subjects/") : -len(".json")]
    return path


def graph_page(
    connection: sqlite3.Connection,
    *,
    limit: int,
    after: GraphKey | None,
) -> tuple[OrientGraph, GraphKey | None]:
    """Up to ``limit`` rows after ``after``: nodes, then edges; and the key to continue."""

    want = limit + 1
    kinds = [(str(kind), int(amount)) for kind, amount in connection.execute(GRAPH_KINDS_SQL)]
    nodes: list[tuple[str, str]] = []
    edge_after = ("", "")
    if after is None or after.phase == "node":
        for kind, _amount in kinds:
            if after is not None and kind < after.first:
                continue
            start = after.second if after is not None and kind == after.first else ""
            nodes.extend(
                (kind, str(path))
                for (path,) in connection.execute(GRAPH_NODES_SQL, (kind, start, want - len(nodes)))
            )
            if len(nodes) >= want:
                break
    else:
        edge_after = (after.first, after.second)
    edges: list[tuple[str, str, str, str | None, str]] = []
    if len(nodes) < want:
        edges = [
            (str(identity), str(source), str(predicate), qualifier, str(target))
            for identity, source, predicate, qualifier, target in connection.execute(
                GRAPH_EDGES_SQL, (*edge_after, want - len(nodes))
            )
        ]
    stop: GraphKey | None = None
    if len(nodes) + len(edges) > limit:
        if edges:
            edges.pop()
        else:
            nodes.pop()
        stop = (
            GraphKey("edge", edges[-1][4], edges[-1][0]) if edges else GraphKey("node", *nodes[-1])
        )
    graph = OrientGraph(
        nodes=tuple(OrientGraphNode(subject=subject_ref(path), kind=kind) for kind, path in nodes),
        edges=tuple(
            OrientGraphEdge(
                claim=identity.removeprefix("Claim:"),
                source=subject_ref(source),
                predicate=predicate,
                qualifier=None if qualifier is None else str(qualifier),
                target=subject_ref(target),
            )
            for identity, source, predicate, qualifier, target in edges
        ),
        totals=OrientGraphTotals(subjects=sum(amount for _kind, amount in kinds)),
    )
    return graph, stop


__all__ = [
    "GRAPH_EDGES_SQL",
    "GRAPH_KINDS_SQL",
    "GRAPH_NODES_SQL",
    "GraphKey",
    "graph_page",
    "subject_ref",
]
