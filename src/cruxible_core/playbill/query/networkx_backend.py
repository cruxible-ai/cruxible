"""The local NetworkX query backend, materialized from accepted projection facts.

The graph is a cache and nothing else. It is built only from accepted Subject
and Claim facts at one accepted coordinate, it can be discarded and rebuilt at
any time without touching the ledger, and its logical export is byte-identical
across rebuilds. Verdicts are not computed here: the backend calls the one
shared verdict path, so it differs from the reference index in storage and
traversal only.

Nodes are ``("subject", ledger_path)`` and ``("claim", ledger_path)``. A
relation Claim contributes one ``asserts`` edge from its Subject and one
``object`` edge to the Subject it names, so both traversal directions are
adjacency reads rather than scans. A relation target that is absent at the
coordinate is still materialized as an unresolved Subject node: dropping the
edge would silently narrow a traversal that must refuse instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

import networkx as nx

from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.providers import ProviderV1
from cruxible_core.playbill.query.backends import (
    ClaimFactRowV1,
    ClaimQueryBackendError,
    ClaimQueryFactsV1,
    ClaimViewRowV1,
    SubjectQueryViewV1,
    SubjectViewRowV1,
    VisibleClaimRow,
    adjacency_rows,
    claim_row_visibility,
    claim_view_row,
    subject_view_row,
)
from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.subjects import AcceptedSubject

_SUBJECT: Literal["subject"] = "subject"
_CLAIM: Literal["claim"] = "claim"
_ASSERTS = "asserts"
_OBJECT = "object"

QueryGraphNodeV1 = tuple[Literal["subject", "claim"], str]


def _subject_node(artifact_path: str) -> QueryGraphNodeV1:
    return (_SUBJECT, artifact_path)


def _claim_node(claim_path: str) -> QueryGraphNodeV1:
    return (_CLAIM, claim_path)


class NetworkXClaimQueryBackend:
    """A disposable NetworkX materialization of accepted Subject/Claim facts."""

    def __init__(
        self,
        facts: ClaimQueryFactsV1,
        *,
        definition: QueryDefinitionV1,
        evaluation_time: datetime,
    ) -> None:
        self._definition = definition
        self._evaluation_time = evaluation_time
        self._graph: nx.MultiDiGraph[QueryGraphNodeV1] | None = None
        self.rebuild(facts)

    # -- materialization --------------------------------------------------

    def rebuild(self, facts: ClaimQueryFactsV1) -> None:
        """Materialize the graph from accepted facts, replacing any earlier build.

        The accepted facts are supplied again on every rebuild: a backend can
        never reconstruct state it was not handed.
        """

        graph: nx.MultiDiGraph[QueryGraphNodeV1] = nx.MultiDiGraph()
        graph.graph["coordinate"] = facts.coordinate
        graph.graph["providers"] = {
            provider.identity.qualified: provider for provider in facts.providers
        }
        for subject in facts.subjects:
            graph.add_node(
                _subject_node(subject.path),
                subject=subject,
                view=subject_view_row(subject),
            )
        for row in facts.claims:
            source = _subject_node(row.subject_path)
            if not graph.has_node(source):
                continue
            predicate = row.accepted.claim.statement.predicate
            claim = _claim_node(row.accepted.path)
            graph.add_node(
                claim,
                fact=row,
                view=claim_view_row(row),
                visible=claim_row_visibility(
                    row,
                    subject=cast(AcceptedSubject, graph.nodes[source]["subject"]),
                    providers=self._providers(graph),
                    policy=self._definition.evaluation_policy,
                    evaluation_time=self._evaluation_time,
                ),
            )
            graph.add_edge(source, claim, key=row.accepted.path, role=_ASSERTS, predicate=predicate)
            target = row.object_subject_path
            if target is None:
                continue
            related = _subject_node(target)
            if not graph.has_node(related):
                graph.add_node(related, subject=None, view=None)
            graph.add_edge(claim, related, key=row.accepted.path, role=_OBJECT, predicate=predicate)
        self._graph = graph

    def discard(self) -> None:
        """Delete the materialization; the ledger and the accepted facts are untouched."""

        self._graph = None

    @property
    def materialized(self) -> bool:
        """Return whether the graph is currently built."""

        return self._graph is not None

    def _built(self) -> nx.MultiDiGraph[QueryGraphNodeV1]:
        if self._graph is None:
            raise ClaimQueryBackendError("the query graph was discarded and has not been rebuilt")
        return self._graph

    @staticmethod
    def _providers(graph: nx.MultiDiGraph[QueryGraphNodeV1]) -> dict[str, ProviderV1]:
        return cast("dict[str, ProviderV1]", graph.graph["providers"])

    # -- the backend primitives ------------------------------------------

    @property
    def coordinate(self) -> AcceptedProjectionCoordinate:
        """Return the accepted coordinate this graph was materialized at."""

        return cast(AcceptedProjectionCoordinate, self._built().graph["coordinate"])

    def subjects(self, kinds: tuple[str, ...], *, subject_id: str | None = None) -> tuple[str, ...]:
        """Return the canonically ordered Subject paths of the declared kinds."""

        admitted = set(kinds)
        graph = self._built()
        found: list[str] = []
        for node in graph.nodes:
            if node[0] != _SUBJECT:
                continue
            subject = cast("AcceptedSubject | None", graph.nodes[node]["subject"])
            if subject is None or subject.shell.subject_kind not in admitted:
                continue
            if subject_id is not None and subject.shell.subject_id != subject_id:
                continue
            found.append(subject.path)
        return byte_sorted(tuple(found))

    def subject(self, artifact_path: str) -> AcceptedSubject | None:
        """Return one accepted Subject row by its exact ledger path."""

        graph = self._built()
        node = _subject_node(artifact_path)
        if not graph.has_node(node):
            return None
        return cast("AcceptedSubject | None", graph.nodes[node]["subject"])

    def claims_on(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose statement subject is that Subject."""

        return self._visible(self._incident(artifact_path, predicate, role=_ASSERTS))

    def claims_to(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        """Return the visible Claims whose Subject-typed object is that Subject."""

        return self._visible(self._incident(artifact_path, predicate, role=_OBJECT))

    def visibility(self, row: ClaimFactRowV1) -> VisibleClaimRow | None:
        """Return the Claim row's visibility, or None when the policy hides it."""

        return claim_row_visibility(
            row,
            subject=self.subject(row.subject_path),
            providers=self._providers(self._built()),
            policy=self._definition.evaluation_policy,
            evaluation_time=self._evaluation_time,
        )

    def _incident(self, artifact_path: str, predicate: str, *, role: str) -> tuple[str, ...]:
        graph = self._built()
        node = _subject_node(artifact_path)
        if not graph.has_node(node):
            return ()
        edges: Any = (
            graph.out_edges(node, keys=True, data=True)
            if role == _ASSERTS
            else graph.in_edges(node, keys=True, data=True)
        )
        return byte_sorted(
            tuple(
                str(key)
                for _, _, key, data in edges
                if data["role"] == role and data["predicate"] == predicate
            )
        )

    def _visible(self, claim_paths: tuple[str, ...]) -> tuple[VisibleClaimRow, ...]:
        graph = self._built()
        rows = (
            cast("VisibleClaimRow | None", graph.nodes[_claim_node(path)]["visible"])
            for path in claim_paths
        )
        return tuple(row for row in rows if row is not None)

    # -- the logical export ----------------------------------------------

    def export(self) -> SubjectQueryViewV1:
        """Export the materialized Subject/relation-Claim structure of this graph."""

        graph = self._built()
        subjects: list[SubjectViewRowV1] = []
        claims: list[ClaimViewRowV1] = []
        asserted: dict[tuple[str, str], list[str]] = {}
        incident: dict[tuple[str, str], list[str]] = {}
        for node in graph.nodes:
            view = graph.nodes[node]["view"]
            if node[0] == _SUBJECT:
                if view is not None:
                    subjects.append(cast(SubjectViewRowV1, view))
                continue
            row = cast(ClaimViewRowV1, view)
            claims.append(row)
            asserted.setdefault((row.subject_path, row.predicate), []).append(row.claim_path)
            if row.object_subject_path is not None:
                incident.setdefault((row.object_subject_path, row.predicate), []).append(
                    row.claim_path
                )
        return SubjectQueryViewV1(
            coordinate=self.coordinate,
            subjects=tuple(sorted(subjects, key=lambda item: item.path.encode("utf-8"))),
            claims=tuple(sorted(claims, key=lambda item: item.claim_path.encode("utf-8"))),
            adjacency=adjacency_rows(asserted, incident),
        )


__all__ = [
    "NetworkXClaimQueryBackend",
    "QueryGraphNodeV1",
]
