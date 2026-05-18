"""Tests for service_propose_group and derive_review_priority."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import (
    DecisionPolicyMatch,
    DecisionPolicySchema,
    ProposalPolicySchema,
    SignalPolicySchema,
)
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.store import GroupStore
from cruxible_core.group.types import CandidateMember, CandidateSignal, GroupResolution
from cruxible_core.service import (
    GroupMemberInput,
    GroupSignalInput,
    ProposeGroupResult,
    derive_review_priority,
    service_propose_group,
    service_propose_group_inputs,
    service_resolve_group,
)


def _fake_resolution(
    trust_status: Literal["trusted", "watch", "invalidated"] = "watch",
) -> GroupResolution:
    """Build a minimal resolution for derive_review_priority tests."""
    return GroupResolution(
        resolution_id="RES-test",
        relationship_type="fits",
        group_signature="sig",
        action="approve",
        trust_status=trust_status,
        resolved_at=datetime.now(timezone.utc),
    )

# ---------------------------------------------------------------------------
# Config YAML with proposal_policy section for group testing
# ---------------------------------------------------------------------------

MATCHING_CONFIG_YAML = """\
version: "1.0"
name: car_parts_matching
description: Vehicle-to-part fitment with proposal policy config

contracts:
  bolt_pattern_io:
    fields:
      lookup_table:
        type: string
  year_range_io:
    fields:
      field:
        type: string
      tolerance:
        type: int
  llm_classify_io:
    fields:
      model_ref:
        type: string
  keyword_match_io:
    fields:
      field:
        type: string
      method:
        type: string

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]
      price:
        type: float
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
      raw_score:
        type: float
        optional: true
    proposal_policy:
      signals:
        bolt_pattern_check:
          role: blocking
          note: "Authoritative — physical compatibility"
        year_range_check:
          role: blocking
        description_fit_v1:
          role: required
          always_review_on_unsure: true
        style_tags_v1:
          role: advisory
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only
      max_group_size: 200
  - name: replaces
    from: Part
    to: Part
    properties:
      direction:
        type: string
        enum: [upgrade, downgrade, equivalent]
      confidence:
        type: float

constraints: []
"""

NO_MATCHING_CONFIG_YAML = """\
version: "1.0"
name: car_parts_no_matching
description: No proposal policy section

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]
      price:
        type: float
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
  - name: replaces
    from: Part
    to: Part
    properties:
      direction:
        type: string
        enum: [upgrade, downgrade, equivalent]
      confidence:
        type: float

constraints: []
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def matching_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance with proposal policy config."""
    (tmp_path / "config.yaml").write_text(MATCHING_CONFIG_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


@pytest.fixture
def tuple_identity_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance where fits dedupes pending proposals by relationship tuple."""
    config = MATCHING_CONFIG_YAML.replace(
        "    proposal_policy:\n      signals:",
        "    proposal_identity: relationship_tuple\n    proposal_policy:\n      signals:",
        1,
    )
    (tmp_path / "config.yaml").write_text(config)
    return CruxibleInstance.init(tmp_path, "config.yaml")


@pytest.fixture
def no_matching_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance without proposal policy config."""
    (tmp_path / "config.yaml").write_text(NO_MATCHING_CONFIG_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _member(
    from_id: str = "BP-1001",
    to_id: str = "V-2024-CIVIC",
    signals: list[CandidateSignal] | None = None,
    properties: dict[str, Any] | None = None,
) -> CandidateMember:
    """Helper to create a CandidateMember for fits relationship."""
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=signals or [],
        properties=properties or {},
    )


def _all_support_signals() -> list[CandidateSignal]:
    """All blocking + required signals as support."""
    return [
        CandidateSignal(signal_source="bolt_pattern_check", signal="support", evidence="match"),
        CandidateSignal(signal_source="year_range_check", signal="support", evidence="in range"),
        CandidateSignal(signal_source="description_fit_v1", signal="support", evidence="fits"),
    ]


def _all_support_with_advisory() -> list[CandidateSignal]:
    """All blocking + required + advisory signals as support."""
    return _all_support_signals() + [
        CandidateSignal(signal_source="style_tags_v1", signal="support", evidence="tags match"),
    ]


def _seed_policy_graph(instance: CruxibleInstance) -> None:
    """Seed entity state so workflow policy matching can inspect FROM/TO selectors."""
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1001",
            properties={
                "part_number": "BP-1001",
                "name": "Ceramic Brake Pads",
                "category": "brakes",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2024-CIVIC",
            properties={
                "vehicle_id": "V-2024-CIVIC",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        )
    )
    instance.save_graph(graph)


def _seed_fitment_entities(instance: CruxibleInstance) -> None:
    """Seed entity state for resolution and override tests."""
    graph = instance.load_graph()
    for part_id, vehicle_id in [
        ("BP-1001", "V-2024-CIVIC"),
        ("BP-1002", "V-2024-ACCORD"),
    ]:
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id=part_id,
                properties={
                    "part_number": part_id,
                    "name": f"Part {part_id}",
                    "category": "brakes",
                },
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id=vehicle_id,
                properties={
                    "vehicle_id": vehicle_id,
                    "year": 2024,
                    "make": "Honda",
                    "model": vehicle_id.split("-")[-1].title(),
                },
            )
        )
    instance.save_graph(graph)


def _approve_live_fit_edge(
    instance: CruxibleInstance,
    *,
    assertion_status: str | None = None,
) -> None:
    graph = instance.load_graph()
    properties = {"verified": True}
    metadata = RelationshipMetadata()
    if assertion_status == "pending":
        metadata = RelationshipMetadata(
            assertion=RelationshipAssertion(
                review=RelationshipReviewState(status="pending")
            )
        )
    elif assertion_status == "rejected":
        metadata = RelationshipMetadata(
            assertion=RelationshipAssertion(
                review=RelationshipReviewState(status="rejected", source="human")
            )
        )
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC",
            properties=properties,
            metadata=metadata,
        )
    )
    instance.save_graph(graph)


# ---------------------------------------------------------------------------
# Basic proposal tests
# ---------------------------------------------------------------------------


class TestBasicProposal:
    def test_basic_proposal_pending(self, matching_instance: CruxibleInstance) -> None:
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_text="test thesis",
            thesis_facts={"style": "casual"},
        )
        assert isinstance(result, ProposeGroupResult)
        assert result.group_id.startswith("GRP-")
        assert result.status == "pending_review"
        assert result.member_count == 1
        assert result.prior_resolution is None

    def test_members_stored_with_signals(self, matching_instance: CruxibleInstance) -> None:
        sigs = _all_support_signals()
        members = [_member(signals=sigs)]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts={"k": "v"})
        # Load from store to verify
        store = matching_instance.get_group_store()
        try:
            stored = store.get_members(result.group_id)
            assert len(stored) == 1
            assert len(stored[0].signals) == 3
            assert stored[0].signals[0].signal_source == "bolt_pattern_check"
        finally:
            store.close()

    def test_input_wrapper_normalizes_members_and_signals(
        self,
        matching_instance: CruxibleInstance,
    ) -> None:
        result = service_propose_group_inputs(
            matching_instance,
            "fits",
            [
                GroupMemberInput(
                    from_type="Part",
                    from_id="BP-1001",
                    to_type="Vehicle",
                    to_id="V-2024-CIVIC",
                    relationship_type="fits",
                    signals=[
                        GroupSignalInput(
                            signal_source="bolt_pattern_check",
                            signal="support",
                            evidence="match",
                            basis={
                                "mode": "enum",
                                "path": "verdict",
                                "value": "match",
                                "matched": "support",
                            },
                        ),
                        GroupSignalInput(
                            signal_source="year_range_check",
                            signal="support",
                            evidence="in range",
                        ),
                        GroupSignalInput(
                            signal_source="description_fit_v1",
                            signal="support",
                            evidence="fits",
                        ),
                    ],
                    properties={"verified": True},
                )
            ],
            thesis_facts={"k": "v"},
        )

        store = matching_instance.get_group_store()
        try:
            assert result.group_id is not None
            stored = store.get_members(result.group_id)
            assert len(stored) == 1
            assert stored[0].properties == {"verified": True}
            assert stored[0].signals[0].basis is not None
            assert stored[0].signals[0].basis.mode == "enum"
        finally:
            store.close()

    def test_multiple_members(self, matching_instance: CruxibleInstance) -> None:
        members = [
            _member("BP-1001", "V-1", signals=_all_support_signals()),
            _member("BP-1002", "V-2", signals=_all_support_signals()),
        ]
        result = service_propose_group(
            matching_instance, "fits", members, thesis_facts={"batch": True}
        )
        assert result.member_count == 2

    def test_no_matching_section_open_mode(self, no_matching_instance: CruxibleInstance) -> None:
        """No proposal policy section → guardrails skipped, signals not required."""
        members = [_member()]  # no signals
        result = service_propose_group(
            no_matching_instance, "fits", members, thesis_facts={"open": True}
        )
        assert result.status == "pending_review"
        assert result.review_priority == "review"  # no prior → review


class TestRelationshipTupleProposalIdentity:
    def test_same_signature_rerun_rewrites_without_self_suppression(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        members = [_member(signals=_all_support_signals())]
        first = service_propose_group(
            tuple_identity_instance,
            "fits",
            members,
            thesis_facts={"bucket": "same"},
        )

        second = service_propose_group(
            tuple_identity_instance,
            "fits",
            members,
            thesis_facts={"bucket": "same"},
        )

        assert second.group_id == first.group_id
        assert second.status == "pending_review"
        assert second.suppressed_members == []

    def test_different_signature_same_tuple_is_suppressed(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        members = [_member(signals=_all_support_signals())]
        first = service_propose_group(
            tuple_identity_instance,
            "fits",
            members,
            thesis_facts={"bucket": "direct"},
            source_workflow_name="direct_path",
        )

        second = service_propose_group(
            tuple_identity_instance,
            "fits",
            members,
            thesis_facts={"bucket": "cascade"},
        )

        assert second.status == "suppressed"
        assert second.suppressed is True
        assert second.member_count == 0
        assert len(second.suppressed_members) == 1
        suppressed = second.suppressed_members[0]
        assert suppressed.reason == "pending_proposal"
        assert suppressed.existing_group_id == first.group_id
        assert suppressed.existing_group_status == "pending_review"
        assert suppressed.source_workflow_name == "direct_path"

    def test_mixed_tuple_conflicts_keep_new_members(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        first = service_propose_group(
            tuple_identity_instance,
            "fits",
            [_member(signals=_all_support_signals())],
            thesis_facts={"bucket": "first"},
        )
        mixed_members = [
            _member(signals=_all_support_signals()),
            _member(
                from_id="BP-1002",
                to_id="V-2024-ACCORD",
                signals=_all_support_signals(),
            ),
        ]

        second = service_propose_group(
            tuple_identity_instance,
            "fits",
            mixed_members,
            thesis_facts={"bucket": "mixed"},
        )

        assert second.status == "pending_review"
        assert second.group_id is not None
        assert second.group_id != first.group_id
        assert second.member_count == 1
        assert len(second.suppressed_members) == 1
        assert second.suppressed_members[0].reason == "pending_proposal"

    def test_live_existing_edge_suppresses_tuple(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        _approve_live_fit_edge(tuple_identity_instance)

        result = service_propose_group(
            tuple_identity_instance,
            "fits",
            [_member(signals=_all_support_signals())],
            thesis_facts={"bucket": "existing"},
        )

        assert result.status == "suppressed"
        assert result.suppressed is True
        assert len(result.suppressed_members) == 1
        assert result.suppressed_members[0].reason == "existing_edge"

    @pytest.mark.parametrize("assertion_status", ["pending", "rejected"])
    def test_non_live_existing_edge_does_not_suppress_tuple(
        self,
        tuple_identity_instance: CruxibleInstance,
        assertion_status: str,
    ) -> None:
        _approve_live_fit_edge(tuple_identity_instance, assertion_status=assertion_status)

        result = service_propose_group(
            tuple_identity_instance,
            "fits",
            [_member(signals=_all_support_signals())],
            thesis_facts={"bucket": assertion_status},
        )

        assert result.status == "pending_review"
        assert result.suppressed_members == []

    def test_applying_group_suppresses_tuple(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        first = service_propose_group(
            tuple_identity_instance,
            "fits",
            [_member(signals=_all_support_signals())],
            thesis_facts={"bucket": "first"},
        )
        group_store = tuple_identity_instance.get_group_store()
        try:
            with group_store.transaction():
                group_store.update_group_status(first.group_id, "applying")
        finally:
            group_store.close()

        second = service_propose_group(
            tuple_identity_instance,
            "fits",
            [_member(signals=_all_support_signals())],
            thesis_facts={"bucket": "second"},
        )

        assert second.status == "suppressed"
        assert second.suppressed_members[0].existing_group_status == "applying"

    def test_pending_tuple_lookup_rejects_mixed_relationship_types(
        self,
        tuple_identity_instance: CruxibleInstance,
    ) -> None:
        group_store = tuple_identity_instance.get_group_store()
        try:
            with pytest.raises(ValueError, match="relationship_type"):
                group_store.find_pending_groups_for_tuples(
                    "fits",
                    [("Part", "BP-1", "Vehicle", "V-1", "replaces")],
                )
        finally:
            group_store.close()

    def test_signature_identity_keeps_current_cross_signature_behavior(
        self,
        matching_instance: CruxibleInstance,
    ) -> None:
        members = [_member(signals=_all_support_signals())]
        first = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"bucket": "first"},
        )
        second = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"bucket": "second"},
        )

        assert second.group_id is not None
        assert second.group_id != first.group_id
        assert second.suppressed is False


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_invalid_relationship_type(self, matching_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="not found in config"):
            service_propose_group(
                matching_instance,
                "nonexistent",
                [_member()],
                thesis_facts={"test": True},
            )

    def test_empty_members(self, matching_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="must not be empty"):
            service_propose_group(matching_instance, "fits", [], thesis_facts={"test": True})

    def test_member_relationship_type_mismatch(self, matching_instance: CruxibleInstance) -> None:
        bad = CandidateMember(
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            relationship_type="replaces",  # wrong
            signals=_all_support_signals(),
        )
        with pytest.raises(
            ConfigError, match="has relationship_type 'replaces' but group is for 'fits'"
        ):
            service_propose_group(matching_instance, "fits", [bad], thesis_facts={"test": True})

    def test_member_from_type_mismatch(self, matching_instance: CruxibleInstance) -> None:
        bad = CandidateMember(
            from_type="Vehicle",  # wrong (should be Part)
            from_id="V-1",
            to_type="Vehicle",
            to_id="V-2",
            relationship_type="fits",
            signals=_all_support_signals(),
        )
        with pytest.raises(ConfigError, match="from_type 'Vehicle' does not match"):
            service_propose_group(matching_instance, "fits", [bad], thesis_facts={"test": True})

    def test_member_to_type_mismatch(self, matching_instance: CruxibleInstance) -> None:
        bad = CandidateMember(
            from_type="Part",
            from_id="BP-1",
            to_type="Part",  # wrong (should be Vehicle)
            to_id="P-2",
            relationship_type="fits",
            signals=_all_support_signals(),
        )
        with pytest.raises(ConfigError, match="to_type 'Part' does not match"):
            service_propose_group(matching_instance, "fits", [bad], thesis_facts={"test": True})

    def test_duplicate_members(self, matching_instance: CruxibleInstance) -> None:
        m = _member("BP-1", "V-1", signals=_all_support_signals())
        with pytest.raises(ConfigError, match="Duplicate member"):
            service_propose_group(matching_instance, "fits", [m, m], thesis_facts={"test": True})

    def test_non_serializable_thesis_facts(self, matching_instance: CruxibleInstance) -> None:
        members = [_member(signals=_all_support_signals())]
        with pytest.raises(ConfigError, match="thesis_facts must be JSON-serializable"):
            service_propose_group(
                matching_instance,
                "fits",
                members,
                thesis_facts={"bad": object()},
            )

    def test_max_group_size(self, matching_instance: CruxibleInstance) -> None:
        # max_group_size is 200 in config
        members = [_member(f"BP-{i}", f"V-{i}", signals=_all_support_signals()) for i in range(201)]
        with pytest.raises(ConfigError, match="exceeds max_group_size"):
            service_propose_group(matching_instance, "fits", members, thesis_facts={"test": True})

    def test_workflow_suppress_applies_before_max_group_size(
        self, matching_instance: CruxibleInstance
    ) -> None:
        graph = matching_instance.load_graph()
        for idx in range(201):
            graph.add_entity(
                EntityInstance(
                    entity_type="Part",
                    entity_id=f"BP-{idx}",
                    properties={
                        "part_number": f"BP-{idx}",
                        "name": f"Brake Part {idx}",
                        "category": "brakes" if idx < 200 else "engine",
                    },
                )
            )
            graph.add_entity(
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id=f"V-{idx}",
                    properties={
                        "vehicle_id": f"V-{idx}",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                )
            )
        matching_instance.save_graph(graph)

        config = matching_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="suppress_brake_members",
                applies_to="workflow",
                workflow_name="triage_fits",
                relationship_type="fits",
                effect="suppress",
                match=DecisionPolicyMatch(**{"from": {"category": "brakes"}}),
            )
        )
        matching_instance.save_config(config)

        members = [
            _member(f"BP-{idx}", f"V-{idx}", signals=_all_support_signals()) for idx in range(201)
        ]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"style": "casual"},
            source_workflow_name="triage_fits",
        )

        assert result.group_id is not None
        assert result.member_count == 1
        assert result.policy_summary == {"suppress_brake_members": 200}


# ---------------------------------------------------------------------------
# Signal validation tests
# ---------------------------------------------------------------------------


class TestSignalValidation:
    def test_undeclared_integration_signal(self, matching_instance: CruxibleInstance) -> None:
        sigs = _all_support_signals() + [
            CandidateSignal(signal_source="unknown_integration", signal="support"),
        ]
        members = [_member(signals=sigs)]
        with pytest.raises(ConfigError, match="undeclared signal source 'unknown_integration'"):
            service_propose_group(matching_instance, "fits", members, thesis_facts={"test": True})

    def test_duplicate_signals_same_integration(self, matching_instance: CruxibleInstance) -> None:
        sigs = _all_support_signals() + [
            CandidateSignal(signal_source="bolt_pattern_check", signal="contradict"),
        ]
        members = [_member(signals=sigs)]
        with pytest.raises(
            ConfigError, match="duplicate signals from signal source 'bolt_pattern_check'"
        ):
            service_propose_group(matching_instance, "fits", members, thesis_facts={"test": True})

    def test_missing_blocking_signal(self, matching_instance: CruxibleInstance) -> None:
        # Only provide year_range_check and description_fit_v1 (missing bolt_pattern_check)
        sigs = [
            CandidateSignal(signal_source="year_range_check", signal="support"),
            CandidateSignal(signal_source="description_fit_v1", signal="support"),
        ]
        members = [_member(signals=sigs)]
        with pytest.raises(
            ConfigError, match="missing signal from blocking signal source 'bolt_pattern_check'"
        ):
            service_propose_group(matching_instance, "fits", members, thesis_facts={"test": True})

    def test_missing_required_signal(self, matching_instance: CruxibleInstance) -> None:
        # Only provide blocking signal sources (missing description_fit_v1 which is required)
        sigs = [
            CandidateSignal(signal_source="bolt_pattern_check", signal="support"),
            CandidateSignal(signal_source="year_range_check", signal="support"),
        ]
        members = [_member(signals=sigs)]
        with pytest.raises(
            ConfigError, match="missing signal from required signal source 'description_fit_v1'"
        ):
            service_propose_group(matching_instance, "fits", members, thesis_facts={"test": True})

    def test_missing_advisory_signal_ok(self, matching_instance: CruxibleInstance) -> None:
        """Advisory signals may be absent — no error."""
        members = [_member(signals=_all_support_signals())]  # no style_tags_v1
        result = service_propose_group(matching_instance, "fits", members, thesis_facts={"test": 1})
        assert result.group_id.startswith("GRP-")

    def test_signal_sources_used_undeclared(self, matching_instance: CruxibleInstance) -> None:
        members = [_member(signals=_all_support_signals())]
        with pytest.raises(ConfigError, match="Signal source 'unknown' not declared"):
            service_propose_group(
                matching_instance,
                "fits",
                members,
                signal_sources_used=["unknown"],
                thesis_facts={"test": True},
            )


# ---------------------------------------------------------------------------
# Signature tests
# ---------------------------------------------------------------------------


class TestSignature:
    def test_deterministic_signature(self, matching_instance: CruxibleInstance) -> None:
        facts = {"style": "casual", "season": "summer"}
        members = [_member(signals=_all_support_signals())]
        r1 = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        r2 = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-2", "V-2", signals=_all_support_signals())],
            thesis_facts=facts,
        )
        assert r1.signature == r2.signature

    def test_different_analysis_state_same_signature(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"color": "warm"}
        members1 = [_member("BP-1", "V-1", signals=_all_support_signals())]
        members2 = [_member("BP-2", "V-2", signals=_all_support_signals())]
        r1 = service_propose_group(
            matching_instance,
            "fits",
            members1,
            thesis_facts=facts,
            analysis_state={"centroid": [0.1, 0.2]},
        )
        r2 = service_propose_group(
            matching_instance,
            "fits",
            members2,
            thesis_facts=facts,
            analysis_state={"centroid": [0.3, 0.4]},
        )
        assert r1.signature == r2.signature

    def test_signature_matches_compute_function(self, matching_instance: CruxibleInstance) -> None:
        facts = {"a": 1, "b": 2}
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        expected = compute_group_signature("fits", facts)
        assert result.signature == expected


class TestPendingBuckets:
    def test_same_signature_reuses_group_and_bumps_pending_version(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
        )
        second = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
        )

        assert second.group_id == first.group_id
        assert second.receipt_id is not None

        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert group.member_count == 1
        assert [(member.from_id, member.to_id) for member in members] == [
            ("BP-1002", "V-2024-ACCORD")
        ]

        receipt_store = matching_instance.get_receipt_store()
        try:
            receipt = receipt_store.get_receipt(second.receipt_id)
        finally:
            receipt_store.close()

        assert receipt is not None
        assert receipt.operation_type == "group_rewrite"
        validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
        assert any(
            node.detail.get("added_tuples")
            == [
                {
                    "from_type": "Part",
                    "from_id": "BP-1002",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-ACCORD",
                    "relationship_type": "fits",
                }
            ]
            and node.detail.get("removed_tuples")
            == [
                {
                    "from_type": "Part",
                    "from_id": "BP-1001",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-CIVIC",
                    "relationship_type": "fits",
                }
            ]
            for node in validation_nodes
        )

    def test_empty_delta_clears_pending_group_and_emits_receipt(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        approved = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
        )
        service_resolve_group(
            matching_instance,
            approved.group_id,
            "approve",
            expected_pending_version=1,
        )

        pending = service_propose_group(
            matching_instance,
            "fits",
            [
                _member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals()),
                _member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals()),
            ],
            thesis_facts=facts,
        )
        cleared = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
        )

        assert pending.group_id is not None
        assert cleared.group_id is None
        assert cleared.status == "suppressed"
        assert cleared.suppressed is True
        assert cleared.receipt_id is not None

        group_store = matching_instance.get_group_store()
        try:
            assert group_store.get_group(pending.group_id) is None
        finally:
            group_store.close()

        receipt_store = matching_instance.get_receipt_store()
        try:
            receipt = receipt_store.get_receipt(cleared.receipt_id)
        finally:
            receipt_store.close()

        assert receipt is not None
        assert receipt.operation_type == "group_clear"
        validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
        assert any(
            node.detail.get("cleared_tuples")
            == [
                {
                    "from_type": "Part",
                    "from_id": "BP-1002",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-ACCORD",
                    "relationship_type": "fits",
                }
            ]
            for node in validation_nodes
        )

    def test_retain_missing_keeps_absent_pending_members(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )
        second = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )

        assert second.group_id == first.group_id

        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert group.member_count == 2
        assert {(member.from_id, member.to_id) for member in members} == {
            ("BP-1001", "V-2024-CIVIC"),
            ("BP-1002", "V-2024-ACCORD"),
        }

        receipt_store = matching_instance.get_receipt_store()
        try:
            receipt = receipt_store.get_receipt(second.receipt_id)
        finally:
            receipt_store.close()

        assert receipt is not None
        assert receipt.operation_type == "group_rewrite"
        validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
        assert any(
            node.detail.get("added_tuples")
            == [
                {
                    "from_type": "Part",
                    "from_id": "BP-1002",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-ACCORD",
                    "relationship_type": "fits",
                }
            ]
            and node.detail.get("removed_tuples") == []
            for node in validation_nodes
        )

    def test_retain_missing_does_not_clear_pending_on_empty_delta(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        approved = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
        )
        service_resolve_group(
            matching_instance,
            approved.group_id,
            "approve",
            expected_pending_version=1,
        )

        pending = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )
        retained = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )

        assert retained.group_id == pending.group_id
        assert retained.receipt_id is None
        assert retained.status == "pending_review"
        assert retained.member_count == 1

        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(pending.group_id)
            members = group_store.get_members(pending.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 1
        assert [(member.from_id, member.to_id) for member in members] == [
            ("BP-1002", "V-2024-ACCORD")
        ]

    def test_retain_missing_keeps_overridden_pending_member(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        graph = matching_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-2024-CIVIC",
                properties={"group_override": True},
            )
        )
        matching_instance.save_graph(graph)

        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )
        second = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )

        assert first.review_priority == "review"
        assert second.group_id == first.group_id
        assert second.review_priority == "review"

        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert group.review_priority == "review"
        assert {(member.from_id, member.to_id) for member in members} == {
            ("BP-1001", "V-2024-CIVIC"),
            ("BP-1002", "V-2024-ACCORD"),
        }

    def test_delta_subtracts_approved_and_ignores_property_drift(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        approved = service_propose_group(
            matching_instance,
            "fits",
            [
                _member(
                    "BP-1001",
                    "V-2024-CIVIC",
                    signals=_all_support_signals(),
                    properties={"raw_score": 0.99},
                )
            ],
            thesis_facts=facts,
        )
        service_resolve_group(
            matching_instance,
            approved.group_id,
            "approve",
            expected_pending_version=1,
        )

        result = service_propose_group(
            matching_instance,
            "fits",
            [
                _member(
                    "BP-1001",
                    "V-2024-CIVIC",
                    signals=_all_support_signals(),
                    properties={"raw_score": 0.10},
                ),
                _member(
                    "BP-1002",
                    "V-2024-ACCORD",
                    signals=_all_support_signals(),
                    properties={"raw_score": 0.55},
                ),
            ],
            thesis_facts=facts,
        )

        assert result.group_id is not None
        assert result.member_count == 1
        group_store = matching_instance.get_group_store()
        try:
            members = group_store.get_members(result.group_id)
        finally:
            group_store.close()

        assert [(member.from_id, member.to_id) for member in members] == [
            ("BP-1002", "V-2024-ACCORD")
        ]

    def test_resolve_unknown_candidate_property_surfaces_validation_error(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        result = service_propose_group(
            matching_instance,
            "fits",
            [
                _member(
                    "BP-1001",
                    "V-2024-CIVIC",
                    signals=_all_support_signals(),
                    properties={"not_declared": "x"},
                )
            ],
            thesis_facts={"rule_id": "bad_property", "rule_version": 1},
        )

        with pytest.raises(DataValidationError, match="unexpected property 'not_declared'"):
            service_resolve_group(
                matching_instance,
                result.group_id,
                "approve",
                expected_pending_version=1,
            )

    def test_rule_version_bump_creates_fresh_bucket_without_prior(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        facts_v1 = {"rule_id": "fit_rule", "rule_version": 1}
        initial = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts_v1,
        )
        service_resolve_group(
            matching_instance,
            initial.group_id,
            "approve",
            expected_pending_version=1,
        )

        facts_v2 = {"rule_id": "fit_rule", "rule_version": 2}
        reproposed = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts_v2,
        )

        assert reproposed.signature != initial.signature
        assert reproposed.prior_resolution is None
        assert reproposed.status == "pending_review"
        assert reproposed.member_count == 1

    def test_override_blocks_auto_resolve_for_new_signature(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_fitment_entities(matching_instance)
        facts_v1 = {"rule_id": "fit_rule", "rule_version": 1}
        initial = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts_v1,
        )
        service_resolve_group(
            matching_instance,
            initial.group_id,
            "approve",
            expected_pending_version=1,
        )

        graph = matching_instance.load_graph()
        assert graph.update_relationship_state(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC",
            "fits",
            property_updates={"group_override": True},
        )
        matching_instance.save_graph(graph)

        facts_v2 = {"rule_id": "fit_rule_v2", "rule_version": 1}
        _create_prior_resolution(
            matching_instance,
            thesis_facts=facts_v2,
            trust_status="trusted",
        )

        reproposed = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts_v2,
        )

        assert reproposed.status == "pending_review"
        assert reproposed.review_priority == "review"

    def test_integrity_error_fallback_rewrites_existing_pending_group(
        self,
        matching_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
        )

        original_find_pending_group = GroupStore.find_pending_group
        call_count = {"value": 0}

        def hide_existing_pending_once(self, relationship_type: str, signature: str):
            if relationship_type == "fits" and signature == first.signature:
                call_count["value"] += 1
                if call_count["value"] == 1:
                    return None
            return original_find_pending_group(self, relationship_type, signature)

        monkeypatch.setattr(GroupStore, "find_pending_group", hide_existing_pending_once)

        second = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
        )

        assert call_count["value"] >= 2
        assert second.group_id == first.group_id
        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert [(member.from_id, member.to_id) for member in members] == [
            ("BP-1002", "V-2024-ACCORD")
        ]

    def test_integrity_error_fallback_merges_pending_group_for_retain_missing(
        self,
        matching_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1001", "V-2024-CIVIC", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )

        original_find_pending_group = GroupStore.find_pending_group
        call_count = {"value": 0}

        def hide_existing_pending_once(self, relationship_type: str, signature: str):
            if relationship_type == "fits" and signature == first.signature:
                call_count["value"] += 1
                if call_count["value"] == 1:
                    return None
            return original_find_pending_group(self, relationship_type, signature)

        monkeypatch.setattr(GroupStore, "find_pending_group", hide_existing_pending_once)

        second = service_propose_group(
            matching_instance,
            "fits",
            [_member("BP-1002", "V-2024-ACCORD", signals=_all_support_signals())],
            thesis_facts=facts,
            pending_refresh_mode="retain_missing",
        )

        assert call_count["value"] >= 2
        assert second.group_id == first.group_id

        group_store = matching_instance.get_group_store()
        try:
            group = group_store.get_group(first.group_id)
            members = group_store.get_members(first.group_id)
        finally:
            group_store.close()

        assert group is not None
        assert group.pending_version == 2
        assert {(member.from_id, member.to_id) for member in members} == {
            ("BP-1001", "V-2024-CIVIC"),
            ("BP-1002", "V-2024-ACCORD"),
        }

        receipt_store = matching_instance.get_receipt_store()
        try:
            receipt = receipt_store.get_receipt(second.receipt_id)
        finally:
            receipt_store.close()

        assert receipt is not None
        validation_nodes = [node for node in receipt.nodes if node.node_type == "validation"]
        assert any(
            node.detail.get("race_resolved_as_rewrite") is True
            and node.detail.get("removed_tuples") == []
            and node.detail.get("added_tuples")
            == [
                {
                    "from_type": "Part",
                    "from_id": "BP-1002",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-ACCORD",
                    "relationship_type": "fits",
                }
            ]
            for node in validation_nodes
        )


# ---------------------------------------------------------------------------
# Auto-resolve tests
# ---------------------------------------------------------------------------


def _create_prior_resolution(
    instance: CruxibleInstance,
    relationship_type: str = "fits",
    thesis_facts: dict[str, Any] | None = None,
    trust_status: str = "watch",
    action: str = "approve",
    confirmed: bool = True,
) -> str:
    """Helper: create a prior resolution directly in the store."""
    facts = thesis_facts or {}
    signature = compute_group_signature(relationship_type, facts)
    store = instance.get_group_store()
    try:
        with store.transaction():
            res_id = store.save_resolution(
                relationship_type,
                signature,
                action,
                "prior rationale",
                "prior thesis",
                facts,
                {"prior_state": True},
                "human",
                trust_status=trust_status,
                confirmed=confirmed,
            )
        return res_id
    finally:
        store.close()


class TestAutoResolve:
    def test_prior_trusted_all_support_auto_resolved(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="trusted")
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "auto_resolved"
        assert result.prior_resolution is not None
        assert result.prior_resolution.trust_status == "trusted"

    def test_workflow_policy_require_review_blocks_auto_resolve(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="trusted")
        _seed_policy_graph(matching_instance)
        config = matching_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="review_brake_parts",
                applies_to="workflow",
                workflow_name="triage_fits",
                relationship_type="fits",
                effect="require_review",
                match=DecisionPolicyMatch(**{"from": {"category": "brakes"}}),
            )
        )
        matching_instance.save_config(config)

        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts=facts,
            source_workflow_name="triage_fits",
        )
        assert result.status == "pending_review"
        assert result.review_priority == "review"
        assert result.suppressed is False
        assert result.policy_summary == {"review_brake_parts": 1}

    def test_workflow_policy_suppress_returns_suppressed_result(
        self, matching_instance: CruxibleInstance
    ) -> None:
        _seed_policy_graph(matching_instance)
        config = matching_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="suppress_brake_parts",
                applies_to="workflow",
                workflow_name="triage_fits",
                relationship_type="fits",
                effect="suppress",
                match=DecisionPolicyMatch(**{"from": {"category": "brakes"}}),
            )
        )
        matching_instance.save_config(config)

        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"style": "casual"},
            source_workflow_name="triage_fits",
        )
        assert result.group_id is None
        assert result.status == "suppressed"
        assert result.suppressed is True
        assert result.member_count == 0
        assert result.policy_summary == {"suppress_brake_parts": 1}

    def test_expired_workflow_policy_is_ignored(self, matching_instance: CruxibleInstance) -> None:
        _seed_policy_graph(matching_instance)
        config = matching_instance.load_config()
        config.decision_policies.append(
            DecisionPolicySchema(
                name="expired_suppress_brake_parts",
                applies_to="workflow",
                workflow_name="triage_fits",
                relationship_type="fits",
                effect="suppress",
                match=DecisionPolicyMatch(**{"from": {"category": "brakes"}}),
                expires_at="2020-01-01T00:00:00Z",
            )
        )
        matching_instance.save_config(config)

        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"style": "casual"},
            source_workflow_name="triage_fits",
        )
        assert result.group_id is not None
        assert result.suppressed is False
        assert result.policy_summary == {}

    def test_prior_trusted_blocking_contradict_pending(
        self, matching_instance: CruxibleInstance
    ) -> None:
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="trusted")
        sigs = [
            CandidateSignal(signal_source="bolt_pattern_check", signal="contradict"),
            CandidateSignal(signal_source="year_range_check", signal="support"),
            CandidateSignal(signal_source="description_fit_v1", signal="support"),
        ]
        members = [_member(signals=sigs)]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"
        assert result.prior_resolution is not None  # advisory

    def test_prior_invalidated_pending(self, matching_instance: CruxibleInstance) -> None:
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="invalidated")
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"
        assert result.review_priority == "critical"

    def test_prior_watch_trusted_only_pending(self, matching_instance: CruxibleInstance) -> None:
        """Default watch trust + trusted_only policy → no auto-resolve."""
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="watch")
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"

    def test_prior_rejected_does_not_enable_auto_resolve(
        self, matching_instance: CruxibleInstance
    ) -> None:
        """Only approved confirmed priors count for auto-resolve."""
        facts = {"style": "casual"}
        _create_prior_resolution(
            matching_instance,
            thesis_facts=facts,
            action="reject",
            trust_status="watch",
            confirmed=True,
        )
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"
        # reject not visible to find_resolution(action="approve")
        assert result.prior_resolution is None

    def test_unconfirmed_prior_invisible(self, matching_instance: CruxibleInstance) -> None:
        """Unconfirmed approval does not act as precedent."""
        facts = {"style": "casual"}
        _create_prior_resolution(
            matching_instance,
            thesis_facts=facts,
            trust_status="trusted",
            confirmed=False,
        )
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"
        assert result.prior_resolution is None

    def test_always_review_on_unsure_blocks_auto_resolve(
        self, matching_instance: CruxibleInstance
    ) -> None:
        """description_fit_v1 has always_review_on_unsure=True."""
        facts = {"style": "casual"}
        _create_prior_resolution(matching_instance, thesis_facts=facts, trust_status="trusted")
        sigs = [
            CandidateSignal(signal_source="bolt_pattern_check", signal="support"),
            CandidateSignal(signal_source="year_range_check", signal="support"),
            CandidateSignal(signal_source="description_fit_v1", signal="unsure"),
        ]
        members = [_member(signals=sigs)]
        result = service_propose_group(matching_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"
        assert result.review_priority == "review"


class TestAutoResolveTrustedOrWatch:
    """Test auto_resolve_requires_prior_trust=trusted_or_watch."""

    PERMISSIVE_YAML = """\
version: "1.0"
name: permissive_matching
description: trusted_or_watch policy

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes]
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_or_watch

constraints: []
"""

    @pytest.fixture
    def permissive_instance(self, tmp_path: Path) -> CruxibleInstance:
        (tmp_path / "config.yaml").write_text(self.PERMISSIVE_YAML)
        return CruxibleInstance.init(tmp_path, "config.yaml")

    def test_watch_plus_trusted_or_watch_auto_resolves(
        self, permissive_instance: CruxibleInstance
    ) -> None:
        facts = {"k": "v"}
        _create_prior_resolution(permissive_instance, thesis_facts=facts, trust_status="watch")
        sigs = [CandidateSignal(signal_source="check_v1", signal="support")]
        members = [_member(signals=sigs)]
        result = service_propose_group(permissive_instance, "fits", members, thesis_facts=facts)
        assert result.status == "auto_resolved"


class TestAutoResolveNoContradict:
    """Test auto_resolve_when=no_contradict policy."""

    NO_CONTRADICT_YAML = """\
version: "1.0"
name: no_contradict_matching
description: no_contradict policy

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes]
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
    proposal_policy:
      signals:
        blocker:
          role: blocking
        required_check:
          role: required
      auto_resolve_when: no_contradict
      auto_resolve_requires_prior_trust: trusted_only

constraints: []
"""

    @pytest.fixture
    def nc_instance(self, tmp_path: Path) -> CruxibleInstance:
        (tmp_path / "config.yaml").write_text(self.NO_CONTRADICT_YAML)
        return CruxibleInstance.init(tmp_path, "config.yaml")

    def test_unsure_on_required_passes_no_contradict(self, nc_instance: CruxibleInstance) -> None:
        facts = {"k": "v"}
        _create_prior_resolution(nc_instance, thesis_facts=facts, trust_status="trusted")
        sigs = [
            CandidateSignal(signal_source="blocker", signal="support"),
            CandidateSignal(signal_source="required_check", signal="unsure"),
        ]
        members = [_member(signals=sigs)]
        result = service_propose_group(nc_instance, "fits", members, thesis_facts=facts)
        assert result.status == "auto_resolved"

    def test_contradict_on_blocking_fails_no_contradict(
        self, nc_instance: CruxibleInstance
    ) -> None:
        facts = {"k": "v"}
        _create_prior_resolution(nc_instance, thesis_facts=facts, trust_status="trusted")
        sigs = [
            CandidateSignal(signal_source="blocker", signal="contradict"),
            CandidateSignal(signal_source="required_check", signal="support"),
        ]
        members = [_member(signals=sigs)]
        result = service_propose_group(nc_instance, "fits", members, thesis_facts=facts)
        assert result.status == "pending_review"


# ---------------------------------------------------------------------------
# Priority derivation tests
# ---------------------------------------------------------------------------


class TestDeriveReviewPriority:
    def test_blocking_contradict_critical(self) -> None:
        matching = ProposalPolicySchema(
            signals={"blocker": SignalPolicySchema(role="blocking")}
        )
        members = [_member(signals=[CandidateSignal(signal_source="blocker", signal="contradict")])]
        assert derive_review_priority(members, matching, None) == "critical"

    def test_invalidated_prior_critical(self) -> None:
        matching = ProposalPolicySchema(
            signals={"check": SignalPolicySchema(role="required")}
        )
        members = [_member(signals=[CandidateSignal(signal_source="check", signal="support")])]
        prior = _fake_resolution("invalidated")
        assert derive_review_priority(members, matching, prior) == "critical"

    def test_always_review_on_unsure_review(self) -> None:
        matching = ProposalPolicySchema(
            signals={
                "check": SignalPolicySchema(
                    role="required",
                    always_review_on_unsure=True,
                )
            }
        )
        members = [_member(signals=[CandidateSignal(signal_source="check", signal="unsure")])]
        prior = _fake_resolution("trusted")
        assert derive_review_priority(members, matching, prior) == "review"

    def test_unsure_on_blocking_review(self) -> None:
        matching = ProposalPolicySchema(
            signals={"blocker": SignalPolicySchema(role="blocking")}
        )
        members = [_member(signals=[CandidateSignal(signal_source="blocker", signal="unsure")])]
        prior = _fake_resolution("trusted")
        assert derive_review_priority(members, matching, prior) == "review"

    def test_unsure_on_required_review(self) -> None:
        matching = ProposalPolicySchema(signals={"req": SignalPolicySchema(role="required")})
        members = [_member(signals=[CandidateSignal(signal_source="req", signal="unsure")])]
        prior = _fake_resolution("trusted")
        assert derive_review_priority(members, matching, prior) == "review"

    def test_no_prior_review(self) -> None:
        matching = ProposalPolicySchema(
            signals={"check": SignalPolicySchema(role="required")}
        )
        members = [_member(signals=[CandidateSignal(signal_source="check", signal="support")])]
        assert derive_review_priority(members, matching, None) == "review"

    def test_prior_watch_review(self) -> None:
        matching = ProposalPolicySchema(
            signals={"check": SignalPolicySchema(role="required")}
        )
        members = [_member(signals=[CandidateSignal(signal_source="check", signal="support")])]
        prior = _fake_resolution("watch")
        assert derive_review_priority(members, matching, prior) == "review"

    def test_all_support_trusted_normal(self) -> None:
        matching = ProposalPolicySchema(
            signals={
                "blocker": SignalPolicySchema(role="blocking"),
                "req": SignalPolicySchema(role="required"),
            }
        )
        members = [
            _member(
                signals=[
                    CandidateSignal(signal_source="blocker", signal="support"),
                    CandidateSignal(signal_source="req", signal="support"),
                ]
            )
        ]
        prior = _fake_resolution("trusted")
        assert derive_review_priority(members, matching, prior) == "normal"

    def test_advisory_ignored_for_priority(self) -> None:
        """Advisory contradict does NOT escalate priority."""
        matching = ProposalPolicySchema(
            signals={
                "req": SignalPolicySchema(role="required"),
                "adv": SignalPolicySchema(role="advisory"),
            }
        )
        members = [
            _member(
                signals=[
                    CandidateSignal(signal_source="req", signal="support"),
                    CandidateSignal(signal_source="adv", signal="contradict"),
                ]
            )
        ]
        prior = _fake_resolution("trusted")
        assert derive_review_priority(members, matching, prior) == "normal"

    def test_no_matching_no_prior(self) -> None:
        members = [_member()]
        assert derive_review_priority(members, None, None) == "review"

    def test_no_matching_with_prior(self) -> None:
        members = [_member()]
        assert derive_review_priority(members, None, _fake_resolution("trusted")) == "normal"

    def test_critical_beats_review(self) -> None:
        """When both critical and review conditions exist, critical wins."""
        matching = ProposalPolicySchema(
            signals={
                "blocker": SignalPolicySchema(role="blocking"),
                "req": SignalPolicySchema(role="required"),
            }
        )
        members = [
            _member(
                signals=[
                    CandidateSignal(signal_source="blocker", signal="contradict"),
                    CandidateSignal(signal_source="req", signal="unsure"),
                ]
            )
        ]
        assert derive_review_priority(members, matching, None) == "critical"


# ---------------------------------------------------------------------------
# suggested_priority tests
# ---------------------------------------------------------------------------


class TestSuggestedPriority:
    def test_stored_but_not_governing(self, matching_instance: CruxibleInstance) -> None:
        members = [_member(signals=_all_support_signals())]
        result = service_propose_group(
            matching_instance,
            "fits",
            members,
            thesis_facts={"k": "v"},
            suggested_priority="high",
        )
        # suggested_priority stored on group but doesn't affect review_priority
        store = matching_instance.get_group_store()
        try:
            group = store.get_group(result.group_id)
            assert group is not None
            assert group.suggested_priority == "high"
            # review_priority is Cruxible-derived, not influenced by suggestion
            assert group.review_priority in ("critical", "review", "normal")
        finally:
            store.close()
