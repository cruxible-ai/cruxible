"""Typed structural comparison of two ``EntityGraph`` images.

This is the graph-shape half of ``cruxible state diff`` (the coordinate
resolution, ownership basis, and artifact assembly half lives in
``service/state_diff.py``). It is deliberately NEWLY WRITTEN rather than
promoted from ``tests/support/state_cross_section.py``: the harness differ
tokenizes digests and timestamps, truncates before comparing, and keys edges on
the per-load ``edge_key`` counter -- all correct for a golden, all disqualifying
for a plan artifact.

Four contracts this module is written to:

* **Duplicates are never collapsed.** The graph is a ``MultiDiGraph``; a
  5-tuple may carry several parallel edges. Every phase consumes ordered
  buckets and cancels 1:1. Last-write-wins on a duplicate key is a defect class
  here, not an implementation detail.
* **Ordering is content-derived.** Output order is ``(item key, canonical state
  digest)``. Insertion order and ``edge_key`` never affect it.
* **No pre-comparison truncation.** Bounds apply to the returned view, never to
  what the comparator sees.
* **``edge_key`` is never identity.** Not because it is ephemeral -- it is
  DURABLE: ``graph_relationships.edge_key`` is a NOT NULL UNIQUE column, loads
  are ordered by it, and ``to_dict``/``from_dict`` round-trip it through
  node-link ``links[].key``. It is excluded because it is semantically
  MEANINGLESS ACROSS RE-SERIALIZATION: every path that re-materializes a graph
  (``add_relationship``'s counter, ``extract_owned_subgraph``, ``merge_graphs``,
  a clone, a pull) re-assigns keys, so two byte-identical semantic states can
  carry different keys and the same key can name different claims in two
  images. It is emitted as a per-side diagnostic only, is kept OUT of the digest
  preimage, and never enters a key, a bucket order, or a match.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from cruxible_core.graph.assertion_state import RelationshipAssertion
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import _evidence_ref_key
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipMetadata,
    make_node_id,
)
from cruxible_core.primitives import canonical_json
from cruxible_core.temporal import format_datetime

OwnershipLabel = Literal["upstream", "local", "cross_boundary", "unknown"]
IdentityBasis = Literal["claim_id", "tuple", "entity_key"]
DiffBucketName = Literal["added", "removed", "changed", "ambiguous", "identity_conflict"]

DIFF_BUCKET_NAMES: tuple[DiffBucketName, ...] = (
    "added",
    "removed",
    "changed",
    "ambiguous",
    "identity_conflict",
)

CHANGE_CHANNELS: tuple[str, ...] = (
    "properties",
    "review_transition",
    "lifecycle_transition",
    "annotations",
)

EdgeTupleKey = tuple[str, str, str, str, str]
"""``(relationship_type, from_type, from_id, to_type, to_id)`` -- never ``edge_key``."""


# ---------------------------------------------------------------------------
# Canonical value normalization
# ---------------------------------------------------------------------------


def normalize_json_value(value: Any) -> Any:
    """Return a JSON-native, canonically-serializable form of ``value``.

    ``primitives.canonical_json`` is called with NO ``default=`` fallback, so
    every value reaching it must already be JSON-native. Two cases are handled
    here rather than left to explode:

    * datetimes normalize through the shared UTC formatter, so a naive and an
      aware stamp of the same instant compare equal;
    * non-finite floats become ``{"non_finite": ...}``. ``allow_nan=False``
      would otherwise fail the WHOLE artifact over one bad ingested property.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"non_finite": "nan"}
        if math.isinf(value):
            return {"non_finite": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, dict):
        return {str(key): normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json_value(item) for item in value]
    return str(value)


def state_digest(state: Any) -> str:
    """Content digest of a normalized comparison state (ordering + cancellation)."""
    payload = canonical_json(state).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


# ---------------------------------------------------------------------------
# Ownership basis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnershipBasis:
    """One coordinate's pinned upstream ownership boundary, or ``unknown``.

    ``unknown`` is not "no types owned": it is "this coordinate never recorded
    what upstream owned at the time". Annotating a snapshot taken before
    ``upstream.json`` existed with today's boundary would be a lie, so the
    label propagates instead of being recomputed.
    """

    basis: Literal["pinned", "unknown"]
    owned_entity_types: frozenset[str] = frozenset()
    owned_relationship_types: frozenset[str] = frozenset()
    reason: str | None = None
    """Why the basis is unknown, so ``stub_detection: disabled`` is explicable.

    An unexplained ``unknown`` reads as a bug in the diff; the honest cases --
    an image written before the basis was pinned, a member that will not parse,
    and propagation from the other coordinate -- are different situations with
    different fixes, and the artifact says which one fired.
    """

    @classmethod
    def unknown_basis(cls, reason: str | None = None) -> OwnershipBasis:
        return cls(basis="unknown", reason=reason)

    @classmethod
    def pinned(
        cls,
        *,
        owned_entity_types: Any = (),
        owned_relationship_types: Any = (),
    ) -> OwnershipBasis:
        return cls(
            basis="pinned",
            owned_entity_types=frozenset(owned_entity_types),
            owned_relationship_types=frozenset(owned_relationship_types),
        )

    @property
    def is_known(self) -> bool:
        return self.basis == "pinned"

    def payload(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "reason": self.reason,
            "owned_entity_types": sorted(self.owned_entity_types),
            "owned_relationship_types": sorted(self.owned_relationship_types),
        }


def relationship_ownership(
    basis: OwnershipBasis,
    *,
    relationship_type: str,
    from_type: str,
    to_type: str,
) -> OwnershipLabel:
    """Ownership of one edge under a coordinate's pinned boundary."""
    if not basis.is_known:
        return "unknown"
    if relationship_type in basis.owned_relationship_types:
        return "upstream"
    if from_type in basis.owned_entity_types or to_type in basis.owned_entity_types:
        return "cross_boundary"
    return "local"


def entity_ownership(
    basis: OwnershipBasis,
    *,
    entity_type: str,
    incident_relationship_types: frozenset[str],
) -> OwnershipLabel:
    """Ownership of one entity under a coordinate's pinned boundary.

    An entity's own type places it on one side of the boundary. It becomes
    ``cross_boundary`` when it also carries an incident edge from the OTHER
    side -- which is exactly the shape ``extract_owned_subgraph`` produces when
    it auto-creates a stub for a non-owned endpoint of an owned relationship
    (``EntityGraph.add_relationship``, "Creates stub entities if needed"). The
    stub predicate in :func:`boundary_stub_keys` needs that distinction, and no
    entity-only ownership rule can express it.
    """
    if not basis.is_known:
        return "unknown"
    owned_side = entity_type in basis.owned_entity_types
    for relationship_type in incident_relationship_types:
        if (relationship_type in basis.owned_relationship_types) != owned_side:
            return "cross_boundary"
    return "upstream" if owned_side else "local"


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphDiffSelector:
    """The caller's SEMANTIC narrowing of the comparison.

    Semantic filters are part of the diff's definition, not of its rendering:
    they are serialized into the artifact header and are inside the digest
    preimage, so a filtered diff can never be mistaken for the unfiltered
    whole.
    """

    entity_types: frozenset[str] | None = None
    relationship_types: frozenset[str] | None = None
    buckets: frozenset[str] | None = None
    changed_only: bool = False

    def wants_entity_type(self, entity_type: str) -> bool:
        return self.entity_types is None or entity_type in self.entity_types

    def wants_relationship_type(self, relationship_type: str) -> bool:
        return self.relationship_types is None or relationship_type in self.relationship_types

    def wants_bucket(self, bucket: str) -> bool:
        if self.changed_only and bucket in {"added", "removed"}:
            return False
        return self.buckets is None or bucket in self.buckets


# ---------------------------------------------------------------------------
# Side inputs and section results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphDiffSide:
    """One resolved side of a graph comparison."""

    graph: EntityGraph
    ownership: OwnershipBasis
    claim_identity_map_digest: str | None = None


@dataclass
class SectionDiff:
    """One comparable section's complete, unelided logical diff."""

    counts: dict[str, int] = field(default_factory=dict)
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    identity_conflict: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def bucket(self, name: str) -> list[dict[str, Any]]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "ambiguous": self.ambiguous,
            "identity_conflict": self.identity_conflict,
        }[name]

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"counts": dict(self.counts), "diagnostics": self.diagnostics}
        for name in DIFF_BUCKET_NAMES:
            body[name] = self.bucket(name)
        return body

    def assert_partition(self) -> None:
        """The buckets are a PARTITION; nothing is counted twice or dropped.

        An ``ambiguous`` residual is never also ``added`` or ``removed``.
        ``identity_conflict`` is counted PER SIDE rather than per pair: the
        ordinary cross-bucket case consumes one item from each side, but an
        intra-side duplicated id can consume several from one side and none
        from the other, and a per-pair count could not express that.
        Asserted here rather than only in tests because a silent violation
        would make a plan artifact's counts non-reconstructible.
        """
        counts = self.counts
        from_total = (
            counts["unchanged"]
            + counts["removed"]
            + counts["changed"]
            + counts["ambiguous_from"]
            + counts["identity_conflict_from"]
            + counts["excluded_from"]
        )
        to_total = (
            counts["unchanged"]
            + counts["added"]
            + counts["changed"]
            + counts["ambiguous_to"]
            + counts["identity_conflict_to"]
            + counts["excluded_to"]
        )
        if from_total != counts["from_total"] or to_total != counts["to_total"]:
            raise AssertionError(
                "State diff bucket partition broken: "
                f"from {from_total} != {counts['from_total']}, to {to_total} != "
                f"{counts['to_total']} ({counts})"
            )


def _empty_counts() -> dict[str, int]:
    return {
        "from_total": 0,
        "to_total": 0,
        "unchanged": 0,
        "added": 0,
        "removed": 0,
        "changed": 0,
        "annotation_only": 0,
        "ambiguous_from": 0,
        "ambiguous_to": 0,
        "identity_conflict_from": 0,
        "identity_conflict_to": 0,
        "excluded_from": 0,
        "excluded_to": 0,
    }


# ---------------------------------------------------------------------------
# Typed comparison state
# ---------------------------------------------------------------------------


def relationship_comparison_state(
    properties: dict[str, Any],
    metadata: RelationshipMetadata,
) -> dict[str, Any]:
    """Full canonical comparison state of one edge.

    Absent typed state normalizes to MODEL DEFAULTS (``RelationshipAssertion()``)
    before comparison, so a legacy edge carrying no assertion block does not
    read as changed against a defaulted one.
    """
    assertion = metadata.assertion or RelationshipAssertion()
    provenance = (
        metadata.provenance.model_dump(mode="json", exclude_none=True)
        if metadata.provenance is not None
        else None
    )
    evidence_refs = list(metadata.evidence.evidence_refs) if metadata.evidence is not None else []
    return {
        "properties": normalize_json_value(properties),
        "review": normalize_json_value(assertion.review.model_dump(mode="json", exclude_none=True)),
        "lifecycle": normalize_json_value(
            assertion.lifecycle.model_dump(mode="json", exclude_none=True)
        ),
        "annotations": {
            "group_override": bool(assertion.group_override),
            "provenance": normalize_json_value(provenance),
            "evidence_ref_keys": sorted(_evidence_ref_key(ref) for ref in evidence_refs),
        },
    }


def entity_comparison_state(entity: EntityInstance) -> dict[str, Any]:
    """Full canonical comparison state of one entity, INCLUDING metadata.

    The harness differ omits entity metadata entirely, which makes entity
    lifecycle transitions inexpressible. They are expressible here.
    """
    metadata = entity.metadata if isinstance(entity.metadata, EntityMetadata) else EntityMetadata()
    lifecycle = metadata.lifecycle
    return {
        "properties": normalize_json_value(entity.properties),
        "lifecycle": normalize_json_value(
            lifecycle.model_dump(mode="json", exclude_none=True) if lifecycle is not None else None
        ),
        "annotations": {
            "actor_context": normalize_json_value(
                metadata.actor_context.model_dump(mode="json", exclude_none=True)
                if metadata.actor_context is not None
                else None
            ),
            "extra": normalize_json_value(metadata.extra),
        },
    }


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EdgeRow:
    tuple_key: EdgeTupleKey
    claim_id: str | None
    edge_key: Any
    ownership: OwnershipLabel
    state: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class _EntityRow:
    key: tuple[str, str]
    ownership: OwnershipLabel
    state: dict[str, Any]
    digest: str
    is_stub: bool


def _edge_rows(side: GraphDiffSide, selector: GraphDiffSelector) -> list[_EdgeRow]:
    rows: list[_EdgeRow] = []
    for (
        from_type,
        from_id,
        to_type,
        to_id,
        relationship_type,
        edge_key,
        claim_id,
        properties,
        metadata,
    ) in side.graph._iter_edges_raw():
        if not selector.wants_relationship_type(relationship_type):
            continue
        state = relationship_comparison_state(dict(properties), metadata)
        rows.append(
            _EdgeRow(
                tuple_key=(relationship_type, from_type, from_id, to_type, to_id),
                claim_id=claim_id if isinstance(claim_id, str) and claim_id else None,
                edge_key=edge_key,
                ownership=relationship_ownership(
                    side.ownership,
                    relationship_type=relationship_type,
                    from_type=from_type,
                    to_type=to_type,
                ),
                state=state,
                digest=state_digest(state),
            )
        )
    return rows


def _incident_relationship_types(graph: EntityGraph) -> dict[str, frozenset[str]]:
    incident: dict[str, set[str]] = defaultdict(set)
    for from_type, from_id, to_type, to_id, relationship_type, *_rest in graph._iter_edges_raw():
        incident[make_node_id(from_type, from_id)].add(relationship_type)
        incident[make_node_id(to_type, to_id)].add(relationship_type)
    return {node_id: frozenset(types) for node_id, types in incident.items()}


def boundary_stub_keys(side: GraphDiffSide) -> frozenset[tuple[str, str]]:
    """Entities that are materialization artifacts, not state.

    FOUR clauses, all required: empty properties, empty metadata, at least one
    incident edge, and ownership resolving ``cross_boundary``. A genuinely
    empty entity with NO incident edges is not a stub and is diffed normally;
    a predicate whose fourth clause is unknowable must not run on three, so
    detection is disabled entirely when the ownership basis is ``unknown``.
    """
    if not side.ownership.is_known:
        return frozenset()
    incident = _incident_relationship_types(side.graph)
    stubs: set[tuple[str, str]] = set()
    for entity in side.graph.iter_all_entities():
        node_id = make_node_id(entity.entity_type, entity.entity_id)
        incident_types = incident.get(node_id, frozenset())
        if entity.properties:
            continue
        metadata = (
            entity.metadata if isinstance(entity.metadata, EntityMetadata) else EntityMetadata()
        )
        if metadata.to_metadata_dict():
            continue
        if not incident_types:
            continue
        if (
            entity_ownership(
                side.ownership,
                entity_type=entity.entity_type,
                incident_relationship_types=incident_types,
            )
            != "cross_boundary"
        ):
            continue
        stubs.add((entity.entity_type, entity.entity_id))
    return frozenset(stubs)


def _entity_rows(
    side: GraphDiffSide,
    selector: GraphDiffSelector,
    stubs: frozenset[tuple[str, str]],
) -> dict[tuple[str, str], _EntityRow]:
    incident = _incident_relationship_types(side.graph)
    rows: dict[tuple[str, str], _EntityRow] = {}
    for entity in side.graph.iter_all_entities():
        if not selector.wants_entity_type(entity.entity_type):
            continue
        key = (entity.entity_type, entity.entity_id)
        state = entity_comparison_state(entity)
        rows[key] = _EntityRow(
            key=key,
            ownership=entity_ownership(
                side.ownership,
                entity_type=entity.entity_type,
                incident_relationship_types=incident.get(
                    make_node_id(*key),
                    frozenset(),
                ),
            ),
            state=state,
            digest=state_digest(state),
            is_stub=key in stubs,
        )
    return rows


# ---------------------------------------------------------------------------
# Channel comparison
# ---------------------------------------------------------------------------


def _property_channel(
    from_properties: dict[str, Any],
    to_properties: dict[str, Any],
) -> dict[str, Any] | None:
    """Key- and value-level property delta through the shared typed helpers.

    Imported lazily to keep the graph diff module's import cost bounded.
    """
    from cruxible_core.graph.property_diffs import property_delta, property_value_changes

    delta = property_delta(to_properties, from_properties)
    if not (delta.added or delta.removed or delta.changed):
        return None
    changes = property_value_changes(
        to_properties,
        from_properties,
        include_added=True,
        include_removed=True,
    )
    return {
        "delta": {
            "added": list(delta.added),
            "removed": list(delta.removed),
            "changed": list(delta.changed),
        },
        "changes": [
            {
                "property": change.property,
                "from_value": normalize_json_value(change.from_value),
                "to_value": normalize_json_value(change.to_value),
            }
            for change in changes
        ],
    }


def _scalar_changes(
    from_state: dict[str, Any] | None,
    to_state: dict[str, Any] | None,
    *,
    skip: frozenset[str],
) -> list[dict[str, Any]]:
    """Field-level changes on a typed sub-state, excluding the axis status."""
    from_state = from_state or {}
    to_state = to_state or {}
    names = sorted((set(from_state) | set(to_state)) - skip)
    return [
        {
            "property": name,
            "from_value": from_state.get(name),
            "to_value": to_state.get(name),
        }
        for name in names
        if from_state.get(name) != to_state.get(name)
    ]


def _transition(
    from_state: dict[str, Any] | None,
    to_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    from_status = (from_state or {}).get("status")
    to_status = (to_state or {}).get("status")
    if from_status == to_status:
        return None
    return {"from": from_status, "to": to_status}


def _annotation_channel(
    from_annotations: dict[str, Any],
    to_annotations: dict[str, Any],
) -> dict[str, Any] | None:
    """Summarize the annotation channel; never descend into it.

    Provenance stamps are volatile ACROSS COORDINATES by construction -- the
    materialization paths null and restamp them deliberately
    (``relabel_clone_receipts``). Descending would make this channel dominate
    every cross-lineage diff with differences that are properties of the
    comparison, not of the state.
    """
    from_provenance = from_annotations.get("provenance") or {}
    to_provenance = to_annotations.get("provenance") or {}
    stamps_changed = sorted(
        name
        for name in set(from_provenance) | set(to_provenance)
        if from_provenance.get(name) != to_provenance.get(name)
    )
    from_evidence = set(from_annotations.get("evidence_ref_keys") or ())
    to_evidence = set(to_annotations.get("evidence_ref_keys") or ())
    evidence_added = sorted(to_evidence - from_evidence)
    evidence_removed = sorted(from_evidence - to_evidence)
    override_from = from_annotations.get("group_override")
    override_to = to_annotations.get("group_override")
    extra_changes = _scalar_changes(
        {k: v for k, v in from_annotations.items() if k not in {"provenance", "evidence_ref_keys"}},
        {k: v for k, v in to_annotations.items() if k not in {"provenance", "evidence_ref_keys"}},
        skip=frozenset({"group_override"}),
    )
    if not (
        stamps_changed
        or evidence_added
        or evidence_removed
        or override_from != override_to
        or extra_changes
    ):
        return None
    return {
        "provenance_stamps_changed": stamps_changed,
        "evidence": {
            "added": evidence_added,
            "removed": evidence_removed,
            "from_count": len(from_evidence),
            "to_count": len(to_evidence),
        },
        "group_override": (
            None if override_from == override_to else {"from": override_from, "to": override_to}
        ),
        "other_changes": extra_changes,
    }


def _changed_channels(
    from_state: dict[str, Any],
    to_state: dict[str, Any],
    *,
    review_axis: bool,
) -> dict[str, Any]:
    """Axis-honest channel decomposition of a matched pair.

    A claim that went ``active -> superseded`` is an ADJUDICATION EVENT, never
    a removal; an edge that left the live view because review went
    ``approved -> rejected`` names the review axis. The ``not-live`` visibility
    bucket collapses both axes and cannot express either.
    """
    body: dict[str, Any] = {}
    channels: list[str] = []

    properties = _property_channel(
        dict(from_state.get("properties") or {}),
        dict(to_state.get("properties") or {}),
    )
    body["properties"] = properties
    if properties is not None:
        channels.append("properties")

    if review_axis:
        review_transition = _transition(from_state.get("review"), to_state.get("review"))
        review_changes = _scalar_changes(
            from_state.get("review"),
            to_state.get("review"),
            skip=frozenset({"status"}),
        )
        body["review_transition"] = review_transition
        body["review_changes"] = review_changes
        if review_transition is not None or review_changes:
            channels.append("review_transition")
    else:
        body["review_transition"] = None
        body["review_changes"] = []

    lifecycle_transition = _transition(from_state.get("lifecycle"), to_state.get("lifecycle"))
    # Effective windows are ordinary lifecycle PROPERTY changes. Evaluating
    # live-view membership instead would make the artifact digest depend on the
    # clock: two runs over byte-identical stored state would disagree.
    lifecycle_changes = _scalar_changes(
        from_state.get("lifecycle"),
        to_state.get("lifecycle"),
        skip=frozenset({"status"}),
    )
    body["lifecycle_transition"] = lifecycle_transition
    body["lifecycle_changes"] = lifecycle_changes
    if lifecycle_transition is not None or lifecycle_changes:
        channels.append("lifecycle_transition")

    annotations = _annotation_channel(
        dict(from_state.get("annotations") or {}),
        dict(to_state.get("annotations") or {}),
    )
    body["annotations"] = annotations
    if annotations is not None:
        channels.append("annotations")

    body["channels"] = [name for name in CHANGE_CHANNELS if name in channels]
    body["annotation_only"] = body["channels"] == ["annotations"]
    return body


# ---------------------------------------------------------------------------
# Item payloads
# ---------------------------------------------------------------------------


def _edge_identity(row: _EdgeRow) -> dict[str, Any]:
    relationship_type, from_type, from_id, to_type, to_id = row.tuple_key
    return {
        "relationship_type": relationship_type,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
    }


def _edge_side_summary(row: _EdgeRow) -> dict[str, Any]:
    return {
        "claim_id": row.claim_id,
        "ownership": row.ownership,
        "state": row.state,
        "state_digest": row.digest,
        # DIAGNOSTIC ONLY. edge_key is minted per load; it can be looked at,
        # never matched on.
        "diagnostic": {"edge_key": row.edge_key},
    }


def _edge_present_item(row: _EdgeRow) -> dict[str, Any]:
    return {**_edge_identity(row), **_edge_side_summary(row)}


def _entity_identity(row: _EntityRow) -> dict[str, Any]:
    return {"entity_type": row.key[0], "entity_id": row.key[1]}


def _entity_present_item(row: _EntityRow) -> dict[str, Any]:
    return {
        **_entity_identity(row),
        "ownership": row.ownership,
        "state": row.state,
        "state_digest": row.digest,
    }


# ---------------------------------------------------------------------------
# Edge matching
# ---------------------------------------------------------------------------


def _reidentification_permitted(
    from_side: GraphDiffSide,
    to_side: GraphDiffSide,
    from_row: _EdgeRow,
    to_row: _EdgeRow,
) -> bool:
    """Both conditions of the reconcile-evidence rule, or nothing.

    A residual 1:1 pair carrying two DIFFERENT claim ids is ``reidentified``
    only when the two coordinates' recorded reconcile-map digests differ (the
    map itself churned, which is the signal ``legacy_identity_map_digest``
    exists to make visible) AND the edge's ownership resolves ``upstream`` (the
    map governs only upstream-origin legacy tuples). Otherwise: ``ambiguous``.
    """
    if from_side.claim_identity_map_digest is None or to_side.claim_identity_map_digest is None:
        return False
    if from_side.claim_identity_map_digest == to_side.claim_identity_map_digest:
        return False
    return from_row.ownership == "upstream" and to_row.ownership == "upstream"


def _content_ordered(rows: list[_EdgeRow]) -> list[_EdgeRow]:
    """Total order over one side's rows: tuple identity, then state digest."""
    return sorted(rows, key=lambda row: (row.tuple_key, row.digest))


def _index_by_claim_id(rows: list[_EdgeRow]) -> dict[str, list[_EdgeRow]]:
    """Group rows by claim id, PRESERVING duplicates rather than overwriting."""
    index: dict[str, list[_EdgeRow]] = defaultdict(list)
    for row in rows:
        if row.claim_id is not None:
            index[row.claim_id].append(row)
    return dict(index)


def diff_edges(
    from_side: GraphDiffSide,
    to_side: GraphDiffSide,
    selector: GraphDiffSelector,
) -> SectionDiff:
    """Multigraph edge comparison: claim ids first, then exact cancellation."""
    result = SectionDiff(counts=_empty_counts())
    from_rows = _edge_rows(from_side, selector)
    to_rows = _edge_rows(to_side, selector)
    result.counts["from_total"] = len(from_rows)
    result.counts["to_total"] = len(to_rows)

    consumed_from: set[int] = set()
    consumed_to: set[int] = set()

    # PHASE 0 -- index. Grouped by id rather than dict-assigned, because a dict
    # comprehension here is LAST-WRITE-WINS on a duplicated id, which is the
    # defect class this module's contracts forbid. The three write-path layers
    # (the in-memory index, the storage INSERT, and the merge guard) make an
    # intra-side duplicate unreachable through the public API, so its
    # appearance means a hand-edited image -- exactly the case that must be
    # named rather than silently resolved to whichever row happened to be last.
    from_by_claim = _index_by_claim_id(from_rows)
    to_by_claim = _index_by_claim_id(to_rows)
    duplicated_ids = {
        claim_id
        for index in (from_by_claim, to_by_claim)
        for claim_id, rows in index.items()
        if len(rows) > 1
    }

    matched: list[tuple[_EdgeRow, _EdgeRow]] = []
    # PHASE 1 -- claim-id matching plus the global sweep for the two shapes no
    # write path can produce: the same id under two different tuples, and the
    # same id twice on one side. Both mean a hand-edited image or two instances
    # that minted colliding ids -- a case where NAMING beats guessing, so every
    # row carrying such an id is reported and matched by nothing.
    from_index = {id(row): position for position, row in enumerate(from_rows)}
    to_index = {id(row): position for position, row in enumerate(to_rows)}
    for claim_id in sorted(duplicated_ids | (set(from_by_claim) & set(to_by_claim))):
        conflict_from = from_by_claim.get(claim_id, [])
        conflict_to = to_by_claim.get(claim_id, [])
        if claim_id not in duplicated_ids:
            from_row, to_row = conflict_from[0], conflict_to[0]
            consumed_from.add(from_index[id(from_row)])
            consumed_to.add(to_index[id(to_row)])
            if from_row.tuple_key == to_row.tuple_key:
                matched.append((from_row, to_row))
                continue
        else:
            for row in conflict_from:
                consumed_from.add(from_index[id(row)])
            for row in conflict_to:
                consumed_to.add(to_index[id(row)])
        result.counts["identity_conflict_from"] += len(conflict_from)
        result.counts["identity_conflict_to"] += len(conflict_to)
        result.identity_conflict.append(
            {
                "claim_id": claim_id,
                "kind": ("duplicate_within_side" if claim_id in duplicated_ids else "cross_bucket"),
                "counts": {"from": len(conflict_from), "to": len(conflict_to)},
                # CONTENT-ORDERED, like every other emitted list. These two
                # come out of the per-side claim index, whose order is graph
                # iteration order -- the one place in this module where an
                # emitted list was still insertion-ordered, which would have
                # made the digest depend on load order.
                "from_items": [
                    {**_edge_identity(row), **_edge_side_summary(row)}
                    for row in _content_ordered(conflict_from)
                ],
                "to_items": [
                    {**_edge_identity(row), **_edge_side_summary(row)}
                    for row in _content_ordered(conflict_to)
                ],
            }
        )

    for from_row, to_row in matched:
        if from_row.digest == to_row.digest:
            result.counts["unchanged"] += 1
            continue
        _append_changed_edge(
            result,
            from_row,
            to_row,
            identity_basis="claim_id",
            subtype=None,
        )

    # PHASE 2/3 -- bucket by 5-tuple, cancel identical states, then resolve the
    # residual by the named-outcome table.
    from_buckets: dict[EdgeTupleKey, list[_EdgeRow]] = defaultdict(list)
    to_buckets: dict[EdgeTupleKey, list[_EdgeRow]] = defaultdict(list)
    for position, row in enumerate(from_rows):
        if position not in consumed_from:
            from_buckets[row.tuple_key].append(row)
    for position, row in enumerate(to_rows):
        if position not in consumed_to:
            to_buckets[row.tuple_key].append(row)

    for tuple_key in sorted(set(from_buckets) | set(to_buckets)):
        # Bucket order is by CANONICAL STATE DIGEST, so ordering is
        # content-derived; edges that tie are byte-identical in comparison
        # state and therefore interchangeable.
        from_bucket = sorted(from_buckets.get(tuple_key, ()), key=lambda row: row.digest)
        to_bucket = sorted(to_buckets.get(tuple_key, ()), key=lambda row: row.digest)

        by_digest: dict[str, int] = defaultdict(int)
        for row in to_bucket:
            by_digest[row.digest] += 1
        residual_from: list[_EdgeRow] = []
        for row in from_bucket:
            if by_digest[row.digest] > 0:
                by_digest[row.digest] -= 1
                result.counts["unchanged"] += 1
            else:
                residual_from.append(row)
        remaining = dict(by_digest)
        residual_to: list[_EdgeRow] = []
        for row in to_bucket:
            if remaining.get(row.digest, 0) > 0:
                remaining[row.digest] -= 1
                residual_to.append(row)

        _resolve_edge_residual(result, from_side, to_side, residual_from, residual_to)

    for bucket in (result.added, result.removed, result.changed, result.ambiguous):
        bucket.sort(key=_item_sort_key)
    result.identity_conflict.sort(key=lambda item: str(item.get("claim_id")))
    result.diagnostics = {"stub_detection": "not_applicable"}
    result.assert_partition()
    apply_selector_to_section(result, selector)
    return result


def _resolve_edge_residual(
    result: SectionDiff,
    from_side: GraphDiffSide,
    to_side: GraphDiffSide,
    residual_from: list[_EdgeRow],
    residual_to: list[_EdgeRow],
) -> None:
    n, m = len(residual_from), len(residual_to)
    if n == 0 and m == 0:
        return
    if m == 0:
        result.counts["removed"] += n
        result.removed.extend(_edge_present_item(row) for row in residual_from)
        return
    if n == 0:
        result.counts["added"] += m
        result.added.extend(_edge_present_item(row) for row in residual_to)
        return
    if n == 1 and m == 1:
        from_row, to_row = residual_from[0], residual_to[0]
        both_identified = from_row.claim_id is not None and to_row.claim_id is not None
        if both_identified and not _reidentification_permitted(
            from_side, to_side, from_row, to_row
        ):
            _append_ambiguous(result, residual_from, residual_to)
            return
        _append_changed_edge(
            result,
            from_row,
            to_row,
            identity_basis="tuple",
            subtype="reidentified" if both_identified else None,
        )
        return
    _append_ambiguous(result, residual_from, residual_to)


def _append_ambiguous(
    result: SectionDiff,
    residual_from: list[_EdgeRow],
    residual_to: list[_EdgeRow],
) -> None:
    """Decline to guess at BUCKET granularity, and account for every residual.

    This is the precedent ``_member_review_state`` already sets by returning an
    empty delta on multi-edge, applied to a bucket instead of one item. Equal
    counts alone never establish 1:1 identity -- nothing is matched on count.
    """
    result.counts["ambiguous_from"] += len(residual_from)
    result.counts["ambiguous_to"] += len(residual_to)
    reference = residual_from[0] if residual_from else residual_to[0]
    result.ambiguous.append(
        {
            **_edge_identity(reference),
            "counts": {"from": len(residual_from), "to": len(residual_to)},
            "from_items": [_edge_side_summary(row) for row in residual_from],
            "to_items": [_edge_side_summary(row) for row in residual_to],
        }
    )


def _append_changed_edge(
    result: SectionDiff,
    from_row: _EdgeRow,
    to_row: _EdgeRow,
    *,
    identity_basis: IdentityBasis,
    subtype: str | None,
) -> None:
    channels = _changed_channels(from_row.state, to_row.state, review_axis=True)
    result.counts["changed"] += 1
    if channels["annotation_only"]:
        result.counts["annotation_only"] += 1
    result.changed.append(
        {
            **_edge_identity(to_row),
            "identity_basis": identity_basis,
            "subtype": subtype,
            "from_claim_id": from_row.claim_id,
            "to_claim_id": to_row.claim_id,
            "from_ownership": from_row.ownership,
            "to_ownership": to_row.ownership,
            "from_state_digest": from_row.digest,
            "to_state_digest": to_row.digest,
            "diagnostic": {
                "from_edge_key": from_row.edge_key,
                "to_edge_key": to_row.edge_key,
            },
            **channels,
        }
    )


# ---------------------------------------------------------------------------
# Entity matching
# ---------------------------------------------------------------------------


def diff_entities(
    from_side: GraphDiffSide,
    to_side: GraphDiffSide,
    selector: GraphDiffSelector,
) -> SectionDiff:
    """Entity comparison, with boundary-stub exclusion ACCOUNTED, never silent."""
    result = SectionDiff(counts=_empty_counts())
    detection_enabled = from_side.ownership.is_known and to_side.ownership.is_known
    from_stubs = boundary_stub_keys(from_side) if detection_enabled else frozenset()
    to_stubs = boundary_stub_keys(to_side) if detection_enabled else frozenset()
    from_rows = _entity_rows(from_side, selector, from_stubs)
    to_rows = _entity_rows(to_side, selector, to_stubs)
    result.counts["from_total"] = len(from_rows)
    result.counts["to_total"] = len(to_rows)

    asymmetry: list[dict[str, Any]] = []
    for key in sorted(set(from_rows) | set(to_rows)):
        from_row = from_rows.get(key)
        to_row = to_rows.get(key)
        from_stub = from_row is not None and from_row.is_stub
        to_stub = to_row is not None and to_row.is_stub
        if from_stub and to_stub:
            result.counts["excluded_from"] += 1
            result.counts["excluded_to"] += 1
            continue
        if from_stub != to_stub and from_row is not None and to_row is not None:
            # A stub matched against a populated entity is a MATERIALIZATION
            # artifact, not a state change: reporting it as `changed` would
            # emit the entire local property set as "added".
            result.counts["excluded_from"] += 1
            result.counts["excluded_to"] += 1
            asymmetry.append(
                {
                    "entity_type": key[0],
                    "entity_id": key[1],
                    "stub_side": "from" if from_stub else "to",
                }
            )
            continue
        if from_stub:
            result.counts["excluded_from"] += 1
            continue
        if to_stub:
            result.counts["excluded_to"] += 1
            continue
        if from_row is None and to_row is not None:
            result.counts["added"] += 1
            result.added.append(_entity_present_item(to_row))
            continue
        if to_row is None and from_row is not None:
            result.counts["removed"] += 1
            result.removed.append(_entity_present_item(from_row))
            continue
        assert from_row is not None and to_row is not None
        if from_row.digest == to_row.digest:
            result.counts["unchanged"] += 1
            continue
        # Entities are REFERENTS, not assertions: there is no review axis.
        channels = _changed_channels(from_row.state, to_row.state, review_axis=False)
        result.counts["changed"] += 1
        if channels["annotation_only"]:
            result.counts["annotation_only"] += 1
        result.changed.append(
            {
                **_entity_identity(to_row),
                "identity_basis": "entity_key",
                "subtype": None,
                "from_ownership": from_row.ownership,
                "to_ownership": to_row.ownership,
                "from_state_digest": from_row.digest,
                "to_state_digest": to_row.digest,
                **channels,
            }
        )

    for bucket in (result.added, result.removed, result.changed):
        bucket.sort(key=_item_sort_key)
    result.diagnostics = {
        "stub_detection": "enabled" if detection_enabled else "disabled",
        "excluded_boundary_stubs": {
            "from": result.counts["excluded_from"],
            "to": result.counts["excluded_to"],
        },
        "boundary_stub_asymmetry": sorted(
            asymmetry,
            key=lambda item: (item["entity_type"], item["entity_id"]),
        ),
    }
    result.assert_partition()
    apply_selector_to_section(result, selector)
    return result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _item_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    """Total order derived from CONTENT: item key, then canonical state digest."""
    identity = (
        str(item.get("relationship_type", "")),
        str(item.get("from_type", "")),
        str(item.get("from_id", "")),
        str(item.get("to_type", "")),
        str(item.get("to_id", "")),
        str(item.get("entity_type", "")),
        str(item.get("entity_id", "")),
    )
    digest = str(
        item.get("state_digest") or item.get("to_state_digest") or item.get("from_state_digest", "")
    )
    return (*identity, digest)


def apply_selector_to_section(section: SectionDiff, selector: GraphDiffSelector) -> None:
    """Drop deselected buckets from the BODY; counts stay whole.

    The bucket selector narrows what is reported, never what was compared: the
    counts remain the honest totals so a filtered artifact still says how much
    it declined to list.
    """
    for name in DIFF_BUCKET_NAMES:
        if not selector.wants_bucket(name):
            section.bucket(name).clear()


__all__ = [
    "CHANGE_CHANNELS",
    "DIFF_BUCKET_NAMES",
    "DiffBucketName",
    "EdgeTupleKey",
    "GraphDiffSelector",
    "GraphDiffSide",
    "IdentityBasis",
    "OwnershipBasis",
    "OwnershipLabel",
    "SectionDiff",
    "apply_selector_to_section",
    "boundary_stub_keys",
    "diff_edges",
    "diff_entities",
    "entity_comparison_state",
    "entity_ownership",
    "normalize_json_value",
    "relationship_comparison_state",
    "relationship_ownership",
    "state_digest",
]
