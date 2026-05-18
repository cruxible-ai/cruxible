"""Tests for the evaluate module."""

from __future__ import annotations

from cruxible_core.config.schema import (
    BoundsQualityCheck,
    CardinalityQualityCheck,
    ConstraintSchema,
    CoreConfig,
    EntityTypeSchema,
    JsonContentQualityCheck,
    PropertyQualityCheck,
    PropertySchema,
    ProposalPolicySchema,
    RelationshipSchema,
    SignalPolicySchema,
    UniquenessQualityCheck,
)
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.group.store import GroupStore
from cruxible_core.group.types import CandidateGroup, CandidateMember, CandidateSignal
from cruxible_core.query.evaluate import evaluate_graph


def _review_metadata(status: str, source: str = "human") -> RelationshipMetadata:
    return RelationshipMetadata(
        assertion=RelationshipAssertion(
            review=RelationshipReviewState(status=status, source=source)
        )
    )


def _group_metadata(group_id: str = "GRP-test") -> RelationshipMetadata:
    return RelationshipMetadata(
        provenance=RelationshipProvenance(source_ref=f"group:{group_id}")
    )


def _minimal_config(**overrides) -> CoreConfig:
    """Build a minimal CoreConfig with overrides."""
    defaults = {
        "name": "test",
        "entity_types": {
            "Part": EntityTypeSchema(
                properties={
                    "part_id": PropertySchema(type="string", primary_key=True),
                    "category": PropertySchema(type="string"),
                    "priority": PropertySchema(type="int", optional=True),
                }
            ),
            "Vehicle": EntityTypeSchema(
                properties={
                    "vehicle_id": PropertySchema(type="string", primary_key=True),
                    "make": PropertySchema(type="string"),
                }
            ),
        },
        "relationships": [
            RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle"),
            RelationshipSchema(
                name="replaces",
                from_entity="Part",
                to_entity="Part",
                properties={"confidence": PropertySchema(type="float")},
            ),
        ],
    }
    defaults.update(overrides)
    return CoreConfig(**defaults)


class TestOrphanEntities:
    def test_detects_orphan(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        # P1 has no edges -> orphan
        report = evaluate_graph(config, graph)
        orphans = [f for f in report.findings if f.category == "orphan_entity"]
        assert len(orphans) == 1
        assert "P1" in orphans[0].message
        assert orphans[0].severity == "warning"

    def test_exclude_orphan_types(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        # Both orphans, but exclude Vehicle
        report = evaluate_graph(config, graph, exclude_orphan_types=["Vehicle"])
        orphans = [f for f in report.findings if f.category == "orphan_entity"]
        assert len(orphans) == 1
        assert "Part" in orphans[0].message

    def test_exclude_multiple_types(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        report = evaluate_graph(config, graph, exclude_orphan_types=["Part", "Vehicle"])
        orphans = [f for f in report.findings if f.category == "orphan_entity"]
        assert len(orphans) == 0

    def test_exclude_none_same_as_default(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        report_default = evaluate_graph(config, graph)
        report_none = evaluate_graph(config, graph, exclude_orphan_types=None)
        orphans_default = [f for f in report_default.findings if f.category == "orphan_entity"]
        orphans_none = [f for f in report_none.findings if f.category == "orphan_entity"]
        assert len(orphans_default) == len(orphans_none)

    def test_no_orphan_when_connected(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        orphans = [f for f in report.findings if f.category == "orphan_entity"]
        assert len(orphans) == 0


class TestCoverageGaps:
    def test_detects_missing_entity_type(self):
        config = _minimal_config()
        graph = EntityGraph()
        # Only add Part, not Vehicle
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        gaps = [f for f in report.findings if f.category == "coverage_gap"]
        entity_gaps = [g for g in gaps if g.detail.get("type") == "entity_type"]
        assert any("Vehicle" in g.message for g in entity_gaps)

    def test_detects_missing_relationship_type(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V1", properties={}))
        # Only add fits, not replaces
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        gaps = [f for f in report.findings if f.category == "coverage_gap"]
        rel_gaps = [g for g in gaps if g.detail.get("type") == "relationship_type"]
        assert any("replaces" in g.message for g in rel_gaps)

    def test_no_gap_when_fully_covered(self):
        config = _minimal_config(
            entity_types={
                "Part": EntityTypeSchema(
                    properties={"part_id": PropertySchema(type="string", primary_key=True)}
                ),
            },
            relationships=[
                RelationshipSchema(name="replaces", from_entity="Part", to_entity="Part"),
            ],
        )
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        gaps = [f for f in report.findings if f.category == "coverage_gap"]
        assert len(gaps) == 0


class TestConstraintViolations:
    def test_detects_violation(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(
                    name="same_category",
                    rule="replaces.FROM.category == replaces.TO.category",
                    severity="error",
                ),
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P1", properties={"category": "brake"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P2", properties={"category": "engine"})
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        violations = [f for f in report.findings if f.category == "constraint_violation"]
        assert len(violations) == 1
        assert violations[0].severity == "error"
        assert "same_category" in violations[0].message

    def test_no_violation_when_matching(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(
                    name="same_category",
                    rule="replaces.FROM.category == replaces.TO.category",
                ),
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P1", properties={"category": "brake"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P2", properties={"category": "brake"})
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        violations = [f for f in report.findings if f.category == "constraint_violation"]
        assert len(violations) == 0

    def test_skips_unparseable_rule(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(name="complex", rule="some_complex_expression(x, y)"),
            ]
        )
        graph = EntityGraph()
        report = evaluate_graph(config, graph)
        violations = [f for f in report.findings if f.category == "constraint_violation"]
        assert len(violations) == 0

    def test_detects_not_equal_violation(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(
                    name="different_category",
                    rule="replaces.FROM.category != replaces.TO.category",
                    severity="warning",
                ),
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P1", properties={"category": "brake"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P2", properties={"category": "brake"})
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        violations = [f for f in report.findings if f.category == "constraint_violation"]
        assert len(violations) == 1
        assert (
            "expected Part:P1.category ('brake') != Part:P2.category ('brake')"
            in violations[0].message
        )

    def test_detects_ordered_constraint_violation(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(
                    name="replacement_priority_descends",
                    rule="replaces.FROM.priority > replaces.TO.priority",
                    severity="error",
                ),
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="P1",
                properties={"category": "brake", "priority": 1},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id="P2",
                properties={"category": "brake", "priority": 5},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        violations = [f for f in report.findings if f.category == "constraint_violation"]
        assert len(violations) == 1
        assert violations[0].severity == "error"


def _governed_replaces_config(*, blocking: bool = False) -> CoreConfig:
    role = "blocking" if blocking else "required"
    return _minimal_config(
        relationships=[
            RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle"),
            RelationshipSchema(
                name="replaces",
                from_entity="Part",
                to_entity="Part",
                proposal_policy=ProposalPolicySchema(
                    signals={
                        "detector": SignalPolicySchema(role=role),
                        "reviewer": SignalPolicySchema(role="required"),
                    }
                ),
            ),
        ],
    )


def _store_with_member(
    signals: list[CandidateSignal],
    *,
    group_id: str = "GRP-test",
) -> GroupStore:
    store = GroupStore(":memory:")
    group = CandidateGroup(
        group_id=group_id,
        relationship_type="replaces",
        signature="sig",
        status="resolved",
        member_count=1,
        signal_sources_used=[signal.signal_source for signal in signals],
    )
    member = CandidateMember(
        relationship_type="replaces",
        from_type="Part",
        from_id="P1",
        to_type="Part",
        to_id="P2",
        signals=signals,
    )
    store.save_group(group)
    store.save_members(group_id, [member])
    store.commit()
    return store


class TestGovernedSupportRelationships:
    def test_detects_missing_group_trail(self):
        config = _governed_replaces_config()
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        store = GroupStore(":memory:")
        report = evaluate_graph(config, graph, group_store=store)
        findings = [
            f for f in report.findings if f.category == "governed_support_relationship"
        ]
        assert len(findings) == 1
        assert findings[0].detail["reason"] == "missing_group_trail"

    def test_detects_pending_review(self):
        config = _governed_replaces_config()
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                metadata=_review_metadata("pending", source="system"),
            )
        )
        report = evaluate_graph(config, graph)
        findings = [
            f for f in report.findings if f.category == "governed_support_relationship"
        ]
        assert len(findings) == 1
        assert "Pending review" in findings[0].message
        assert findings[0].detail["reason"] == "pending_review"

    def test_detects_missing_required_signal(self):
        config = _governed_replaces_config()
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                metadata=_group_metadata(),
            )
        )
        store = _store_with_member(
            [CandidateSignal(signal_source="detector", signal="support")]
        )
        report = evaluate_graph(config, graph, group_store=store)
        findings = [
            f for f in report.findings if f.category == "governed_support_relationship"
        ]
        assert len(findings) == 1
        assert findings[0].detail["reason"] == "missing_required_signal"
        assert findings[0].detail["signal_sources"] == ["reviewer"]

    def test_detects_required_unsure_signal(self):
        config = _governed_replaces_config()
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                metadata=_group_metadata(),
            )
        )
        store = _store_with_member(
            [
                CandidateSignal(signal_source="detector", signal="unsure"),
                CandidateSignal(signal_source="reviewer", signal="support"),
            ]
        )
        report = evaluate_graph(config, graph, group_store=store)
        findings = [
            f for f in report.findings if f.category == "governed_support_relationship"
        ]
        assert len(findings) == 1
        assert findings[0].detail["reason"] == "required_unsure"

    def test_detects_blocking_contradict_signal(self):
        config = _governed_replaces_config(blocking=True)
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                metadata=_group_metadata(),
            )
        )
        store = _store_with_member(
            [
                CandidateSignal(signal_source="detector", signal="contradict"),
                CandidateSignal(signal_source="reviewer", signal="support"),
            ]
        )
        report = evaluate_graph(config, graph, group_store=store)
        findings = [
            f for f in report.findings if f.category == "governed_support_relationship"
        ]
        assert len(findings) == 1
        assert findings[0].detail["reason"] == "blocking_contradict"


class TestReportStructure:
    def test_max_findings_truncates(self):
        config = _minimal_config()
        graph = EntityGraph()
        # Create 5 orphan entities
        for i in range(5):
            graph.add_entity(EntityInstance(entity_type="Part", entity_id=f"P{i}", properties={}))
        report = evaluate_graph(config, graph, max_findings=3)
        assert len(report.findings) == 3
        # Summary counts all findings, not just truncated
        assert report.summary["orphan_entity"] == 5

    def test_summary_counts(self):
        config = _minimal_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P2",
                to_type="Part",
                to_id="P3",
                properties={"confidence": 0.1},
            )
        )
        report = evaluate_graph(config, graph)
        assert report.entity_count > 0
        assert report.edge_count > 0
        # Should have at least orphan and coverage findings
        assert "orphan_entity" in report.summary
        assert "coverage_gap" in report.summary

    def test_empty_graph(self):
        config = _minimal_config()
        graph = EntityGraph()
        report = evaluate_graph(config, graph)
        assert report.entity_count == 0
        assert report.edge_count == 0
        # Should still have coverage gaps
        gaps = [f for f in report.findings if f.category == "coverage_gap"]
        assert len(gaps) > 0


def _cross_ref_config(**overrides) -> CoreConfig:
    """Config with SDN, Officer, Company types and xref + works_at relationships."""
    defaults = {
        "name": "test_xref",
        "entity_types": {
            "SDN": EntityTypeSchema(
                properties={"sdn_id": PropertySchema(type="string", primary_key=True)}
            ),
            "Officer": EntityTypeSchema(
                properties={"officer_id": PropertySchema(type="string", primary_key=True)}
            ),
            "Company": EntityTypeSchema(
                properties={"company_id": PropertySchema(type="string", primary_key=True)}
            ),
        },
        "relationships": [
            RelationshipSchema(name="xref", from_entity="SDN", to_entity="Officer"),
            RelationshipSchema(name="works_at", from_entity="Officer", to_entity="Company"),
        ],
    }
    defaults.update(overrides)
    return CoreConfig(**defaults)


class TestUnreviewedCoMembers:
    def test_detects_unreviewed_co_member(self):
        """Officer2 shares Company1 with matched Officer1 but has no xref → flagged."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        # SDN1 → Officer1 via xref
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        # Officer1 → Company1 via works_at
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        # Officer2 → Company1 via works_at
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 1
        assert co_members[0].detail["entity_type"] == "Officer"
        assert co_members[0].detail["entity_id"] == "O2"
        assert co_members[0].detail["matched_sibling"] == "Officer:O1"
        assert co_members[0].detail["shared_via"] == "works_at"
        assert co_members[0].detail["shared_entity"] == "Company:C1"
        assert co_members[0].detail["missing_relationship"] == "xref"

    def test_no_finding_when_co_member_also_matched(self):
        """Officer2 also has incoming xref → not flagged."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S2",
                to_type="Officer",
                to_id="O2",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0


class TestQualityChecks:
    def test_property_check_reports_non_empty_failures_and_zero_counts(self):
        config = _minimal_config(
            quality_checks=[
                PropertyQualityCheck(
                    name="part_category_non_empty",
                    target="entity",
                    entity_type="Part",
                    property="category",
                    rule="non_empty",
                    severity="error",
                ),
                BoundsQualityCheck(
                    name="vehicle_count_ok",
                    target="entity_count",
                    entity_type="Vehicle",
                    min_count=0,
                    max_count=10,
                ),
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P1", properties={"category": ""})
        )

        report = evaluate_graph(config, graph)

        quality = [f for f in report.findings if f.category == "quality_check_failed"]
        assert len(quality) == 1
        assert quality[0].severity == "error"
        assert quality[0].detail["check_name"] == "part_category_non_empty"
        assert report.quality_summary["part_category_non_empty"] == 1
        assert report.quality_summary["vehicle_count_ok"] == 0

    def test_json_content_check_flags_empty_objects(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(
                    name="affects",
                    from_entity="Part",
                    to_entity="Vehicle",
                    properties={"affected_versions": PropertySchema(type="json", optional=True)},
                ),
            ],
            quality_checks=[
                JsonContentQualityCheck(
                    name="no_empty_ranges",
                    target="relationship",
                    relationship_type="affects",
                    property="affected_versions",
                    rule="no_empty_objects_in_array",
                )
            ],
        )
        graph = EntityGraph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="affects",
                from_type="Part",
                from_id="P1",
                to_type="Vehicle",
                to_id="V1",
                properties={"affected_versions": [{}]},
            )
        )

        report = evaluate_graph(config, graph)

        quality = [f for f in report.findings if f.category == "quality_check_failed"]
        assert len(quality) == 1
        assert quality[0].detail["check_kind"] == "json_content"
        assert quality[0].detail["reason"] == "empty_object"
        assert report.quality_summary["no_empty_ranges"] == 1

    def test_uniqueness_check_groups_collisions(self):
        config = _minimal_config(
            quality_checks=[
                UniquenessQualityCheck(
                    name="part_categories_unique",
                    entity_type="Part",
                    properties=["category"],
                )
            ]
        )
        graph = EntityGraph()
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P1", properties={"category": "brakes"})
        )
        graph.add_entity(
            EntityInstance(entity_type="Part", entity_id="P2", properties={"category": "brakes"})
        )

        report = evaluate_graph(config, graph)

        quality = [f for f in report.findings if f.category == "quality_check_failed"]
        assert len(quality) == 1
        assert quality[0].detail["entity_ids"] == ["P1", "P2"]
        assert report.quality_summary["part_categories_unique"] == 1

    def test_bounds_check_reports_count_out_of_range(self):
        config = _minimal_config(
            quality_checks=[
                BoundsQualityCheck(
                    name="fits_edge_bounds",
                    target="relationship_count",
                    relationship_type="fits",
                    min_count=1,
                    max_count=1,
                )
            ]
        )
        graph = EntityGraph()

        report = evaluate_graph(config, graph)

        quality = [f for f in report.findings if f.category == "quality_check_failed"]
        assert len(quality) == 1
        assert quality[0].detail["relationship_type"] == "fits"
        assert report.quality_summary["fits_edge_bounds"] == 1

    def test_cardinality_check_reports_per_entity_failures(self):
        config = _minimal_config(
            quality_checks=[
                CardinalityQualityCheck(
                    name="parts_need_fitment",
                    entity_type="Part",
                    relationship_type="fits",
                    direction="outgoing",
                    min_count=1,
                )
            ]
        )
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))

        report = evaluate_graph(config, graph)

        quality = [f for f in report.findings if f.category == "quality_check_failed"]
        assert len(quality) == 1
        assert quality[0].detail["entity_id"] == "P1"
        assert report.quality_summary["parts_need_fitment"] == 1

    def test_no_findings_for_self_referential(self):
        """Config with only Part→Part replaces → no co-member findings."""
        config = _minimal_config(
            entity_types={
                "Part": EntityTypeSchema(
                    properties={"part_id": PropertySchema(type="string", primary_key=True)}
                ),
            },
            relationships=[
                RelationshipSchema(name="replaces", from_entity="Part", to_entity="Part"),
            ],
        )
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Part", entity_id="P2", properties={}))
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="replaces",
                from_type="Part",
                from_id="P1",
                to_type="Part",
                to_id="P2",
                properties={},
            )
        )
        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0

    def test_deduplication_across_intermediaries(self):
        """Officer2 shares Company1 AND Company2 with Officer1 → only 1 finding."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C2", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        # Officer1 works at both C1 and C2
        for c_id in ["C1", "C2"]:
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="works_at",
                    from_type="Officer",
                    from_id="O1",
                    to_type="Company",
                    to_id=c_id,
                    properties={},
                )
            )
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="works_at",
                    from_type="Officer",
                    from_id="O2",
                    to_type="Company",
                    to_id=c_id,
                    properties={},
                )
            )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 1
        assert co_members[0].detail["entity_id"] == "O2"

    def test_skips_high_degree_intermediary(self):
        """Company with >200 incoming works_at edges → zero co-member findings."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        # Add 201 more officers at Company1 (total incoming = 202 > 200)
        for i in range(201):
            oid = f"OX{i}"
            graph.add_entity(EntityInstance(entity_type="Officer", entity_id=oid, properties={}))
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="works_at",
                    from_type="Officer",
                    from_id=oid,
                    to_type="Company",
                    to_id="C1",
                    properties={},
                )
            )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0

    def test_does_not_skip_low_degree_intermediary(self):
        """Same structure with fewer officers → Officer2 IS flagged."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 1
        assert co_members[0].detail["entity_id"] == "O2"

    def test_skips_rejected_seed_edge(self):
        """Rejected xref seed doesn't populate matched_set → no finding."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                metadata=_review_metadata("rejected"),
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0

    def test_skips_rejected_outgoing_membership_edge(self):
        """Rejected outgoing works_at from Officer1 → Company1 not reachable."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                metadata=_review_metadata("rejected"),
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0

    def test_skips_rejected_incoming_membership_edge(self):
        """Rejected incoming works_at for Officer2 → not reachable as co-member."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O2", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O2",
                to_type="Company",
                to_id="C1",
                metadata=_review_metadata("rejected"),
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0

    def test_summary_counts_all_before_truncation(self):
        """Summary counts reflect true totals even when findings are truncated."""
        config = _cross_ref_config()
        graph = EntityGraph()

        # Create SDN and matched Officer
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))

        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        # Create >10 unmatched officers at the same company
        for i in range(12):
            oid = f"UO{i}"
            graph.add_entity(EntityInstance(entity_type="Officer", entity_id=oid, properties={}))
            graph.add_relationship(
                RelationshipInstance(
                    relationship_type="works_at",
                    from_type="Officer",
                    from_id=oid,
                    to_type="Company",
                    to_id="C1",
                    properties={},
                )
            )

        report = evaluate_graph(config, graph, max_findings=5)
        assert len(report.findings) == 5
        assert report.summary["unreviewed_co_member"] > 5

    def test_skips_malformed_edge_wrong_co_member_type(self):
        """Malformed works_at edge from Company to Company is ignored."""
        config = _cross_ref_config()
        graph = EntityGraph()
        graph.add_entity(EntityInstance(entity_type="SDN", entity_id="S1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Officer", entity_id="O1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C1", properties={}))
        graph.add_entity(EntityInstance(entity_type="Company", entity_id="C2", properties={}))

        # SDN1 → Officer1 via xref (seeds matched_set)
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="xref",
                from_type="SDN",
                from_id="S1",
                to_type="Officer",
                to_id="O1",
                properties={},
            )
        )
        # Officer1 → Company1 via works_at
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Officer",
                from_id="O1",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )
        # Malformed: Company2 → Company1 via works_at (wrong from_entity type)
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="works_at",
                from_type="Company",
                from_id="C2",
                to_type="Company",
                to_id="C1",
                properties={},
            )
        )

        report = evaluate_graph(config, graph)
        co_members = [f for f in report.findings if f.category == "unreviewed_co_member"]
        assert len(co_members) == 0
