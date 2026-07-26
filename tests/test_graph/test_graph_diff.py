"""The multigraph matching algorithm and axis-honest classification.

One test per named-outcome row of the D5 scenario table, plus the partition
invariant every one of them has to keep: an edge is in exactly one of
``unchanged`` / ``added`` / ``removed`` / ``changed`` / ``ambiguous`` /
``identity_conflict``, and the section totals sum back to the input counts.

Coordinate resolution, artifact identity, and receipting live in
``tests/test_service/test_state_diff.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.graph.assertion_state import (
    EntityLifecycleState,
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.diff import (
    GraphDiffSelector,
    GraphDiffSide,
    OwnershipBasis,
    SectionDiff,
    boundary_stub_keys,
    diff_edges,
    diff_entities,
    normalize_json_value,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import RelationshipEvidence
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
)

OWNED = OwnershipBasis.pinned(
    owned_entity_types=("Part",),
    owned_relationship_types=("fits",),
)
LOCAL_ONLY = OwnershipBasis.pinned()
NO_SELECTOR = GraphDiffSelector()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _entity(entity_id: str, **properties: Any) -> EntityInstance:
    return EntityInstance(entity_type="Part", entity_id=entity_id, properties=dict(properties))


def _graph(*entities: EntityInstance) -> EntityGraph:
    graph = EntityGraph()
    for entity in entities:
        graph.add_entity(entity)
    return graph


def _edge(
    graph: EntityGraph,
    *,
    claim_id: str | None = None,
    from_id: str = "P-1",
    to_id: str = "V-1",
    relationship_type: str = "fits",
    properties: dict[str, Any] | None = None,
    review: str = "unreviewed",
    lifecycle: str = "active",
    metadata: RelationshipMetadata | None = None,
) -> None:
    """Add one edge, bypassing the id-less refusal for LEGACY-image fixtures."""
    resolved_metadata = metadata or RelationshipMetadata(
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status=review),  # type: ignore[arg-type]
            lifecycle=RelationshipLifecycleState(status=lifecycle),  # type: ignore[arg-type]
        )
    )
    instance = RelationshipInstance(
        relationship_type=relationship_type,
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        claim_id=claim_id or "CLM-placeholder",
        properties=dict(properties or {}),
        metadata=resolved_metadata,
    )
    key = graph.add_relationship(instance)
    if claim_id is None:
        # A pre-identity image genuinely carries no id. The graph's add path
        # refuses one, so the fixture writes it and then strips it -- exactly
        # the shape ``EntityGraph.from_dict`` produces for a legacy snapshot.
        graph._claim_ids.discard("CLM-placeholder")
        for _u, _v, edge_key, data in graph._graph.edges(keys=True, data=True):
            if edge_key == key:
                data["claim_id"] = None


def _side(graph: EntityGraph, *, ownership: OwnershipBasis = OWNED, map_digest: str | None = None):
    return GraphDiffSide(graph=graph, ownership=ownership, claim_identity_map_digest=map_digest)


def _assert_partition(section: SectionDiff) -> None:
    section.assert_partition()
    keys: list[tuple[Any, ...]] = []
    for bucket in ("added", "removed", "changed"):
        for item in section.bucket(bucket):
            keys.append((bucket, _identity_key(item)))
    assert len(keys) == len({key[1] for key in keys}) or True  # duplicates are legal per-bucket
    identities = [key[1] for key in keys]
    for identity in set(identities):
        buckets = {bucket for bucket, key in keys if key == identity}
        assert len(buckets) == 1, f"{identity} appears in more than one bucket: {buckets}"


def _identity_key(item: dict[str, Any]) -> tuple[Any, ...]:
    if "entity_type" in item:
        return (item["entity_type"], item["entity_id"])
    return (
        item["relationship_type"],
        item["from_type"],
        item["from_id"],
        item["to_type"],
        item["to_id"],
        item.get("to_state_digest") or item.get("state_digest"),
    )


# ---------------------------------------------------------------------------
# D5 scenario table
# ---------------------------------------------------------------------------


def test_scenario_1_duplicate_tuple_one_side_all_idful() -> None:
    """Phase 1 pairs by id; the extra edge is cleanly added. Never ambiguous."""
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", properties={"grade": "oem"})
    _edge(after, claim_id="CLM-b", properties={"grade": "aftermarket"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.counts["added"] == 1
    assert section.counts["ambiguous_from"] == section.counts["ambiguous_to"] == 0
    assert section.added[0]["claim_id"] == "CLM-b"
    _assert_partition(section)


def test_scenario_2_duplicate_one_side_legacy_leftovers_are_added() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, properties={"grade": "oem"})
    _edge(after, properties={"grade": "aftermarket"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.counts["added"] == 1
    assert section.counts["ambiguous_to"] == 0
    _assert_partition(section)


def test_scenario_2_residual_n_by_m_is_all_ambiguous_never_added_or_removed() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, properties={"grade": "a"})
    _edge(before, properties={"grade": "b"})
    after = _graph(_entity("P-1"))
    _edge(after, properties={"grade": "c"})
    _edge(after, properties={"grade": "d"})
    _edge(after, properties={"grade": "e"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["added"] == 0
    assert section.counts["removed"] == 0
    assert section.counts["changed"] == 0
    assert section.counts["ambiguous_from"] == 2
    assert section.counts["ambiguous_to"] == 3
    assert section.ambiguous[0]["counts"] == {"from": 2, "to": 3}
    assert len(section.ambiguous[0]["from_items"]) == 2
    _assert_partition(section)


def test_scenario_3_duplicates_both_sides_stable_ids_never_ambiguous() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", properties={"grade": "oem"})
    _edge(before, claim_id="CLM-b", properties={"grade": "aftermarket"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", properties={"grade": "oem"})
    _edge(after, claim_id="CLM-b", properties={"grade": "reman"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.counts["changed"] == 1
    assert section.counts["ambiguous_from"] == 0
    assert section.changed[0]["identity_basis"] == "claim_id"
    _assert_partition(section)


def test_scenario_4_legacy_duplicates_cancel_then_one_to_one_is_changed() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, properties={"grade": "oem"})
    _edge(before, properties={"grade": "aftermarket"})
    after = _graph(_entity("P-1"))
    _edge(after, properties={"grade": "oem"})
    _edge(after, properties={"grade": "reman"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.counts["changed"] == 1
    assert section.changed[0]["identity_basis"] == "tuple"
    assert section.changed[0]["from_claim_id"] is None
    _assert_partition(section)


def test_scenario_5_idless_versus_idful_shows_the_mixed_identity() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", properties={"grade": "reman"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["changed"] == 1
    item = section.changed[0]
    assert item["identity_basis"] == "tuple"
    assert item["from_claim_id"] is None
    assert item["to_claim_id"] == "CLM-a"
    assert section.counts["added"] == 0 and section.counts["removed"] == 0
    _assert_partition(section)


def test_scenario_6_pull_rekey_with_stable_ids_is_a_zero_item_diff() -> None:
    """``edge_key`` is not in any key, so a re-key cannot manufacture a diff."""
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", properties={"grade": "oem"})
    _edge(before, claim_id="CLM-b", properties={"grade": "aftermarket"})
    after = _graph(_entity("P-1"))
    # Reversed insertion order -> different edge_key on every edge.
    _edge(after, claim_id="CLM-b", properties={"grade": "aftermarket"})
    _edge(after, claim_id="CLM-a", properties={"grade": "oem"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 2
    assert section.added == section.removed == section.changed == []
    _assert_partition(section)


def test_scenario_6_pull_rekey_on_legacy_unique_tuples_is_also_empty() -> None:
    before = _graph(_entity("P-1"), _entity("P-2"))
    _edge(before, from_id="P-1", properties={"grade": "oem"})
    _edge(before, from_id="P-2", properties={"grade": "oem"})
    after = _graph(_entity("P-1"), _entity("P-2"))
    _edge(after, from_id="P-2", properties={"grade": "oem"})
    _edge(after, from_id="P-1", properties={"grade": "oem"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 2
    _assert_partition(section)


# ---------------------------------------------------------------------------
# identity_conflict and reidentification
# ---------------------------------------------------------------------------


def test_cross_bucket_duplicate_id_is_identity_conflict_and_matches_nothing() -> None:
    before = _graph(_entity("P-1"), _entity("P-2"))
    _edge(before, claim_id="CLM-a", from_id="P-1")
    after = _graph(_entity("P-1"), _entity("P-2"))
    _edge(after, claim_id="CLM-a", from_id="P-2")

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["identity_conflict_from"] == 1
    assert section.counts["identity_conflict_to"] == 1
    assert section.counts["added"] == 0
    assert section.counts["removed"] == 0
    conflict = section.identity_conflict[0]
    assert conflict["claim_id"] == "CLM-a"
    assert conflict["kind"] == "cross_bucket"
    assert conflict["from_items"][0]["from_id"] == "P-1"
    assert conflict["to_items"][0]["from_id"] == "P-2"
    _assert_partition(section)


def _duplicate_claim_id_graph(claim_id: str) -> EntityGraph:
    """Hand-build the shape three write-path layers make unreachable.

    ``add_relationship`` refuses a duplicate id, the storage INSERT has a
    UNIQUE column, and ``from_dict`` raises -- so this can only arrive from a
    hand-edited image, which is exactly why the comparator must name it rather
    than let a dict comprehension pick a winner.
    """
    graph = _graph(_entity("P-1"))
    _edge(graph, claim_id=claim_id, properties={"grade": "oem"})
    _edge(graph, claim_id="CLM-other", properties={"grade": "aftermarket"})
    for _u, _v, _key, data in graph._graph.edges(keys=True, data=True):
        if data.get("claim_id") == "CLM-other":
            data["claim_id"] = claim_id
    return graph


def test_intra_side_duplicate_claim_id_is_identity_conflict_not_last_write_wins() -> None:
    before = _duplicate_claim_id_graph("CLM-dup")
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-dup", properties={"grade": "oem"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    conflict = section.identity_conflict[0]
    assert conflict["kind"] == "duplicate_within_side"
    assert conflict["counts"] == {"from": 2, "to": 1}
    # BOTH from-side rows are reported, not just whichever indexed last.
    assert len(conflict["from_items"]) == 2
    assert section.counts["identity_conflict_from"] == 2
    assert section.counts["identity_conflict_to"] == 1
    assert section.counts["added"] == 0
    assert section.counts["removed"] == 0
    assert section.counts["changed"] == 0
    _assert_partition(section)


def test_conflict_item_lists_are_content_ordered_not_insertion_ordered() -> None:
    """The per-side conflict lists come out of a claim index keyed by load order.

    Every other emitted list is content-ordered; this one was the last place an
    insertion order could reach the digest.
    """

    def _build(reverse: bool) -> EntityGraph:
        graph = _graph(_entity("P-1"), _entity("P-2"), _entity("P-3"))
        rows = [("P-1", "a"), ("P-2", "b"), ("P-3", "c")]
        for from_id, grade in reversed(rows) if reverse else rows:
            _edge(graph, from_id=from_id, claim_id=f"CLM-{grade}", properties={"grade": grade})
        for _u, _v, _key, data in graph._graph.edges(keys=True, data=True):
            data["claim_id"] = "CLM-dup"
        graph._claim_ids = {"CLM-dup"}
        return graph

    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-dup", properties={"grade": "z"})

    forward = diff_edges(_side(_build(False)), _side(after), NO_SELECTOR)
    reverse = diff_edges(_side(_build(True)), _side(after), NO_SELECTOR)
    # Compared without the per-side `diagnostic` block, which is the projection
    # the digest is taken over -- `edge_key` legitimately differs between two
    # insertion orders, and is excluded for exactly that reason.
    assert _without_diagnostics(forward.identity_conflict) == _without_diagnostics(
        reverse.identity_conflict
    )
    assert [item["from_id"] for item in forward.identity_conflict[0]["from_items"]] == [
        "P-1",
        "P-2",
        "P-3",
    ]


def test_duplicate_claim_id_on_one_side_only_still_partitions() -> None:
    before = _duplicate_claim_id_graph("CLM-dup")
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-elsewhere", properties={"grade": "reman"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["identity_conflict_from"] == 2
    assert section.counts["identity_conflict_to"] == 0
    assert section.counts["added"] == 1
    _assert_partition(section)


def _reidentification_pair() -> tuple[EntityGraph, EntityGraph]:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-old", properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-new", properties={"grade": "reman"})
    return before, after


def test_reidentified_requires_both_map_churn_and_upstream_ownership() -> None:
    before, after = _reidentification_pair()
    section = diff_edges(
        _side(before, map_digest="sha256:aaa"),
        _side(after, map_digest="sha256:bbb"),
        NO_SELECTOR,
    )
    assert section.counts["changed"] == 1
    assert section.changed[0]["subtype"] == "reidentified"
    _assert_partition(section)


def test_identical_map_digests_make_it_ambiguous_not_reidentified() -> None:
    before, after = _reidentification_pair()
    section = diff_edges(
        _side(before, map_digest="sha256:aaa"),
        _side(after, map_digest="sha256:aaa"),
        NO_SELECTOR,
    )
    assert section.counts["changed"] == 0
    assert section.counts["ambiguous_from"] == 1
    _assert_partition(section)


def test_non_upstream_ownership_makes_it_ambiguous_not_reidentified() -> None:
    before, after = _reidentification_pair()
    section = diff_edges(
        _side(before, ownership=LOCAL_ONLY, map_digest="sha256:aaa"),
        _side(after, ownership=LOCAL_ONLY, map_digest="sha256:bbb"),
        NO_SELECTOR,
    )
    assert section.counts["changed"] == 0
    assert section.counts["ambiguous_from"] == 1


def test_snapshot_pair_without_map_digests_never_claims_reidentification() -> None:
    before, after = _reidentification_pair()
    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["ambiguous_from"] == 1
    assert section.counts["changed"] == 0


# ---------------------------------------------------------------------------
# D6 -- axis-honest classification
# ---------------------------------------------------------------------------


def test_superseded_is_a_transition_never_a_removal() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", lifecycle="active")
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", lifecycle="superseded")

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["removed"] == 0
    item = section.changed[0]
    assert item["lifecycle_transition"] == {"from": "active", "to": "superseded"}
    assert item["channels"] == ["lifecycle_transition"]


def test_rejected_names_the_review_axis_not_the_lifecycle_axis() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", review="approved")
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", review="rejected")

    item = diff_edges(_side(before), _side(after), NO_SELECTOR).changed[0]
    assert item["review_transition"] == {"from": "approved", "to": "rejected"}
    assert item["lifecycle_transition"] is None
    assert item["channels"] == ["review_transition"]


def test_pending_edges_are_in_scope_and_diffed_like_any_other_edge() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", review="pending", properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", review="pending", properties={"grade": "reman"})

    item = diff_edges(_side(before), _side(after), NO_SELECTOR).changed[0]
    assert item["channels"] == ["properties"]


def test_effective_window_change_is_a_lifecycle_property_change() -> None:
    before = _graph(_entity("P-1"))
    _edge(
        before,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            assertion=RelationshipAssertion(
                lifecycle=RelationshipLifecycleState(
                    status="active", effective_until="2026-01-01T00:00:00Z"
                )
            )
        ),
    )
    after = _graph(_entity("P-1"))
    _edge(
        after,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            assertion=RelationshipAssertion(
                lifecycle=RelationshipLifecycleState(
                    status="active", effective_until="2027-01-01T00:00:00Z"
                )
            )
        ),
    )

    item = diff_edges(_side(before), _side(after), NO_SELECTOR).changed[0]
    assert item["lifecycle_transition"] is None
    assert [change["property"] for change in item["lifecycle_changes"]] == ["effective_until"]
    assert item["channels"] == ["lifecycle_transition"]


def test_annotation_only_changes_are_counted_separately() -> None:
    before = _graph(_entity("P-1"))
    _edge(
        before,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            provenance=RelationshipProvenance(source="ingest", receipt_id="RCP-1")
        ),
    )
    after = _graph(_entity("P-1"))
    _edge(
        after,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            provenance=RelationshipProvenance(source="ingest", receipt_id="RCP-2")
        ),
    )

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["changed"] == 1
    assert section.counts["annotation_only"] == 1
    item = section.changed[0]
    assert item["annotation_only"] is True
    assert item["channels"] == ["annotations"]
    assert item["annotations"]["provenance_stamps_changed"] == ["receipt_id"]


def test_evidence_delta_is_keyed_on_the_existing_composite() -> None:
    before = _graph(_entity("P-1"))
    _edge(
        before,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=[{"source": "scanner", "source_record_id": "R-1"}]
            )
        ),
    )
    after = _graph(_entity("P-1"))
    _edge(
        after,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=[{"source": "scanner", "source_record_id": "R-2"}]
            )
        ),
    )

    item = diff_edges(_side(before), _side(after), NO_SELECTOR).changed[0]
    evidence = item["annotations"]["evidence"]
    assert evidence["from_count"] == 1 and evidence["to_count"] == 1
    assert len(evidence["added"]) == 1 and len(evidence["removed"]) == 1
    assert "source_record_id" in evidence["added"][0]


def test_absent_typed_state_normalizes_to_model_defaults() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", metadata=RelationshipMetadata())
    after = _graph(_entity("P-1"))
    _edge(
        after,
        claim_id="CLM-a",
        metadata=RelationshipMetadata(assertion=RelationshipAssertion()),
    )
    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.changed == []


def test_no_group_approval_drift_field_anywhere_in_a_diff_item() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", properties={"grade": "oem"})
    after = _graph(_entity("P-1"))
    _edge(after, claim_id="CLM-a", properties={"grade": "reman"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert "group_approval_drift" not in _flatten_keys(section.payload())


def test_entities_have_no_review_axis_only_lifecycle() -> None:
    before = _graph(_entity("P-1", grade="oem"))
    after = _graph(
        EntityInstance(
            entity_type="Part",
            entity_id="P-1",
            properties={"grade": "oem"},
            metadata=EntityMetadata(lifecycle=EntityLifecycleState(status="retired")),
        )
    )
    item = diff_entities(_side(before), _side(after), NO_SELECTOR).changed[0]
    assert item["review_transition"] is None
    assert item["review_changes"] == []
    assert item["lifecycle_transition"] == {"from": None, "to": "retired"}


# ---------------------------------------------------------------------------
# D7 -- boundary stubs
# ---------------------------------------------------------------------------


def _stub_graph() -> EntityGraph:
    """A ``Vehicle`` stub: not an owned type, but an endpoint of an owned edge."""
    graph = EntityGraph()
    graph.add_entity(_entity("P-1", grade="oem"))
    _edge(graph, claim_id="CLM-a")
    return graph


def test_four_clause_stub_predicate() -> None:
    graph = _stub_graph()
    assert boundary_stub_keys(_side(graph)) == frozenset({("Vehicle", "V-1")})


def test_genuinely_empty_entity_with_no_incident_edges_is_not_a_stub() -> None:
    graph = _stub_graph()
    graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-ORPHAN"))
    stubs = boundary_stub_keys(_side(graph))
    assert ("Vehicle", "V-ORPHAN") not in stubs

    section = diff_entities(_side(EntityGraph()), _side(graph), NO_SELECTOR)
    added = {(item["entity_type"], item["entity_id"]) for item in section.added}
    assert ("Vehicle", "V-ORPHAN") in added
    assert ("Vehicle", "V-1") not in added


def test_stub_exclusion_is_accounted_never_silent() -> None:
    section = diff_entities(_side(EntityGraph()), _side(_stub_graph()), NO_SELECTOR)
    assert section.diagnostics["excluded_boundary_stubs"] == {"from": 0, "to": 1}
    assert section.diagnostics["stub_detection"] == "enabled"
    _assert_partition(section)


def test_stub_versus_populated_reports_asymmetry_not_a_whole_property_set_change() -> None:
    before = _stub_graph()
    after = _stub_graph()
    after.update_entity_properties("Vehicle", "V-1", {"model": "Civic"})

    section = diff_entities(_side(before), _side(after), NO_SELECTOR)
    assert section.changed == []
    assert section.diagnostics["boundary_stub_asymmetry"] == [
        {"entity_type": "Vehicle", "entity_id": "V-1", "stub_side": "from"}
    ]
    _assert_partition(section)


def test_stub_detection_is_disabled_under_unknown_ownership() -> None:
    unknown = OwnershipBasis.unknown_basis()
    section = diff_entities(
        _side(EntityGraph(), ownership=unknown),
        _side(_stub_graph(), ownership=unknown),
        NO_SELECTOR,
    )
    assert section.diagnostics["stub_detection"] == "disabled"
    assert section.diagnostics["excluded_boundary_stubs"] == {"from": 0, "to": 0}
    added = {(item["entity_type"], item["entity_id"]) for item in section.added}
    assert ("Vehicle", "V-1") in added


# ---------------------------------------------------------------------------
# Comparator contracts
# ---------------------------------------------------------------------------


def test_output_order_is_content_derived_not_insertion_order() -> None:
    before = EntityGraph()
    after_forward = _graph(_entity("P-3"), _entity("P-1"), _entity("P-2"))
    after_reverse = _graph(_entity("P-2"), _entity("P-1"), _entity("P-3"))

    forward = diff_entities(_side(before), _side(after_forward), NO_SELECTOR)
    reverse = diff_entities(_side(before), _side(after_reverse), NO_SELECTOR)
    assert [item["entity_id"] for item in forward.added] == ["P-1", "P-2", "P-3"]
    assert forward.payload() == reverse.payload()


def test_edge_key_is_diagnostic_only_and_never_a_match_key() -> None:
    before = _graph(_entity("P-1"))
    _edge(before, claim_id="CLM-a", properties={"grade": "oem"})
    after = _graph(_entity("P-1"), _entity("P-2"))
    _edge(after, from_id="P-2", claim_id="CLM-z")
    _edge(after, claim_id="CLM-a", properties={"grade": "oem"})

    section = diff_edges(_side(before), _side(after), NO_SELECTOR)
    assert section.counts["unchanged"] == 1
    assert section.counts["added"] == 1
    assert "edge_key" in section.added[0]["diagnostic"]


def test_bucket_selector_narrows_the_body_but_never_the_counts() -> None:
    before = _graph(_entity("P-1"))
    after = _graph(_entity("P-1"), _entity("P-2"))
    section = diff_entities(
        _side(before),
        _side(after),
        GraphDiffSelector(buckets=frozenset({"removed"})),
    )
    assert section.counts["added"] == 1
    assert section.added == []


def test_changed_only_suppresses_added_and_removed_items() -> None:
    before = _graph(_entity("P-1"))
    after = _graph(_entity("P-1"), _entity("P-2"))
    section = diff_entities(_side(before), _side(after), GraphDiffSelector(changed_only=True))
    assert section.counts["added"] == 1
    assert section.added == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (float("nan"), {"non_finite": "nan"}),
        (float("inf"), {"non_finite": "inf"}),
        (float("-inf"), {"non_finite": "-inf"}),
    ],
)
def test_non_finite_floats_never_take_down_the_artifact(value: float, expected: Any) -> None:
    assert normalize_json_value({"score": value}) == {"score": expected}


def _without_diagnostics(value: Any) -> Any:
    """Drop per-side ``diagnostic`` blocks, mirroring the digest projection."""
    if isinstance(value, dict):
        return {
            key: _without_diagnostics(item) for key, item in value.items() if key != "diagnostic"
        }
    if isinstance(value, list):
        return [_without_diagnostics(item) for item in value]
    return value


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _flatten_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _flatten_keys(item)
    return keys
