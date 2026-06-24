"""Tests for config schema Pydantic models."""

from textwrap import dedent, indent

import pytest
from pydantic import ValidationError

from cruxible_core.config.loader import load_config_from_string, save_config
from cruxible_core.config.predicates import StructuredPredicateSpec
from cruxible_core.config.schema import (
    BUILTIN_CONTRACTS,
    ActorIdentityGuardCondition,
    AssertCountSpec,
    AssertExistsSpec,
    AssertNotTruncatedSpec,
    AssertSpec,
    BoundsQualityCheck,
    CardinalityQualityCheck,
    ConstraintSchema,
    ContractSchema,
    CoreConfig,
    CoWriteGuardCondition,
    CoWriteRequirement,
    EntityTypeSchema,
    EnumSchema,
    EvidenceRequirementGuardCondition,
    JsonContentQualityCheck,
    MutationGuardSchema,
    NamedQueryResultCountGuardCondition,
    NamedQueryResultCountQualityCheck,
    NamedQuerySchema,
    PropertyQualityCheck,
    PropertySchema,
    ProposalPolicySchema,
    ProviderArtifactSchema,
    ProviderSchema,
    RelatedExclusionSpec,
    RelationshipPropertyConsistencyQualityCheck,
    RelationshipSchema,
    RuntimeConfigSchema,
    SignalPolicySchema,
    TraversalStep,
    UniquenessQualityCheck,
    WorkflowSchema,
    WorkflowStepSchema,
    WorkflowTestExpectSchema,
    WorkflowTestSchema,
)
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError


class TestPropertySchema:
    def test_minimal(self):
        prop = PropertySchema(type="string")
        assert prop.type == "string"
        assert prop.primary_key is False
        assert prop.optional is False
        assert prop.enum is None

    def test_type_defaults_to_string(self):
        prop = PropertySchema()
        assert prop.type == "string"
        assert prop.optional is False

    @pytest.mark.parametrize(
        "type_name",
        [
            "string",
            "int",
            "integer",
            "float",
            "number",
            "bool",
            "date",
            "datetime",
            "json",
        ],
    )
    def test_accepts_supported_property_types(self, type_name: str):
        prop = PropertySchema(type=type_name)
        assert prop.type == type_name

    def test_rejects_unsupported_property_type(self):
        with pytest.raises(ValidationError, match="Input should be"):
            PropertySchema(type="unsupported")

    def test_rejects_unsupported_property_type_during_config_load(self):
        with pytest.raises(ConfigError, match="Config validation failed") as exc_info:
            load_config_from_string(
                """
version: "1.0"
name: invalid_property_type
entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
      status:
        type: unsupported
relationships: []
"""
            )

        assert any(
            "properties → status → type" in error and "'json'" in error
            for error in exc_info.value.errors
        )

    def test_required_alias_sets_optional_false(self):
        prop = PropertySchema(required=True)
        assert prop.optional is False

    def test_required_false_sets_optional_true(self):
        prop = PropertySchema(required=False)
        assert prop.optional is True

    def test_rejects_conflicting_required_optional_aliases(self):
        with pytest.raises(ValidationError, match="required and optional"):
            PropertySchema(required=True, optional=True)

    def test_primary_key_cannot_be_optional(self):
        with pytest.raises(ValidationError, match="primary_key"):
            PropertySchema(primary_key=True, optional=True)

    def test_full(self):
        prop = PropertySchema(
            type="string",
            primary_key=True,
            indexed=True,
            enum=["a", "b"],
            description="test",
        )
        assert prop.primary_key is True
        assert prop.indexed is True
        assert prop.enum == ["a", "b"]

    def test_json_schema_allowed_for_json_type(self):
        prop = PropertySchema(
            type="json",
            json_schema={"type": "array", "items": {"type": "object"}},
        )
        assert prop.json_schema == {"type": "array", "items": {"type": "object"}}

    def test_json_schema_rejected_for_non_json_type(self):
        with pytest.raises(ValidationError, match="json_schema is only allowed"):
            PropertySchema(type="string", json_schema={"type": "string"})

    def test_enum_and_enum_ref_are_mutually_exclusive(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            PropertySchema(type="string", enum=["a"], enum_ref="shared")

    def test_inline_enum_rejects_duplicate_values(self):
        with pytest.raises(ValidationError, match="unique"):
            PropertySchema(type="string", enum=["a", "a"])

    def test_inline_enum_default_must_be_allowed(self):
        with pytest.raises(ValidationError, match="default must be one of"):
            PropertySchema(type="string", enum=["a", "b"], default="c")

    def test_inline_enum_accepts_non_string_values(self):
        prop = PropertySchema(type="int", enum=[1, 2, 3], default=2)
        assert prop.enum == [1, 2, 3]


class TestStructuredPredicateSpec:
    def test_accepts_generic_predicate_map(self):
        spec = StructuredPredicateSpec.model_validate(
            {
                "payload.properties.status": {"eq": "active"},
                "payload.properties.deleted_at": {"exists": False},
            }
        )

        assert spec.root["payload.properties.status"] == {"eq": "active"}

    def test_rejects_invalid_generic_predicate_shape(self):
        with pytest.raises(ValidationError, match="predicate operator 'exists' requires"):
            StructuredPredicateSpec.model_validate(
                {"payload.properties.deleted_at": {"exists": "false"}}
            )

    def test_accepts_string_contains_predicates(self):
        spec = StructuredPredicateSpec.model_validate(
            {
                "payload.properties.title": {"contains": "review"},
                "payload.properties.summary": {"icontains": "release"},
            }
        )

        assert spec.root["payload.properties.title"] == {"contains": "review"}
        assert spec.root["payload.properties.summary"] == {"icontains": "release"}

    def test_rejects_non_string_contains_predicate_value(self):
        with pytest.raises(ValidationError, match="requires a string value"):
            StructuredPredicateSpec.model_validate({"payload.properties.title": {"contains": 1}})


class TestEnumSchema:
    def test_valid_shared_enum(self):
        enum = EnumSchema(values=["active", "retired"], description="Lifecycle")
        assert enum.values == ["active", "retired"]

    def test_ordered_enum_low_to_high(self):
        enum = EnumSchema(values=["low", "medium", "high"], ordered="low_to_high")

        assert enum.ordered == "low_to_high"

    def test_ordered_enum_rejects_boolean(self):
        with pytest.raises(ValidationError):
            EnumSchema(values=["low", "high"], ordered=True)

    def test_rejects_empty_enum(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            EnumSchema(values=[])

    def test_rejects_duplicate_values(self):
        with pytest.raises(ValidationError, match="unique"):
            EnumSchema(values=["active", "active"])


class TestEntityTypeSchema:
    def test_graph_properties_default_optional_and_string(self):
        entity = EntityTypeSchema(
            properties={
                "id": PropertySchema(primary_key=True),
                "label": PropertySchema(),
                "hostname": PropertySchema(required=True),
            }
        )

        assert entity.properties["id"].type == "string"
        assert entity.properties["id"].optional is False
        assert entity.properties["label"].type == "string"
        assert entity.properties["label"].optional is True
        assert entity.properties["hostname"].optional is False

    def test_constraints_default_empty(self):
        entity = EntityTypeSchema(properties={"name": PropertySchema(type="string")})
        assert entity.constraints == []

    def test_get_primary_key(self):
        entity = EntityTypeSchema(
            properties={
                "id": PropertySchema(type="string", primary_key=True),
                "name": PropertySchema(type="string"),
            }
        )
        assert entity.get_primary_key() == "id"

    def test_no_primary_key(self):
        entity = EntityTypeSchema(properties={"name": PropertySchema(type="string")})
        assert entity.get_primary_key() is None

    def test_write_policy_defaults_to_none(self):
        entity = EntityTypeSchema(properties={"name": PropertySchema(type="string")})
        assert entity.write_policy is None

    @pytest.mark.parametrize("policy", ["direct", "proposal_only"])
    def test_write_policy_accepts_enum_values(self, policy):
        entity = EntityTypeSchema(
            properties={"name": PropertySchema(type="string")},
            write_policy=policy,
        )
        assert entity.write_policy == policy

    def test_write_policy_rejects_unknown_value(self):
        with pytest.raises(ValidationError, match="Input should be"):
            EntityTypeSchema(
                properties={"name": PropertySchema(type="string")},
                write_policy="refuse",
            )


class TestRelationshipSchema:
    def test_from_alias(self):
        """Relationship uses 'from'/'to' in YAML but from_entity/to_entity in Python."""
        rel = RelationshipSchema(
            name="fits",
            **{"from": "Part", "to": "Vehicle"},
        )
        assert rel.from_entity == "Part"
        assert rel.to_entity == "Vehicle"

    def test_populate_by_name(self):
        rel = RelationshipSchema(
            name="fits",
            from_entity="Part",
            to_entity="Vehicle",
        )
        assert rel.from_entity == "Part"

    def test_defaults(self):
        rel = RelationshipSchema(name="r", from_entity="A", to_entity="B")
        assert rel.cardinality == "many_to_many"
        assert rel.properties == {}
        assert rel.reverse_name is None
        assert rel.proposal_identity == "thesis_signature"
        assert rel.write_policy is None

    @pytest.mark.parametrize("policy", ["direct", "proposal_only"])
    def test_write_policy_accepts_enum_values(self, policy):
        rel = RelationshipSchema(name="r", from_entity="A", to_entity="B", write_policy=policy)
        assert rel.write_policy == policy

    def test_write_policy_rejects_unknown_value(self):
        with pytest.raises(ValidationError, match="Input should be"):
            RelationshipSchema(name="r", from_entity="A", to_entity="B", write_policy="nope")

    def test_write_policy_parses_under_extra_forbid(self):
        # RelationshipSchema is extra="forbid"; the declared field must parse.
        rel = RelationshipSchema.model_validate(
            {"name": "r", "from": "A", "to": "B", "write_policy": "proposal_only"}
        )
        assert rel.write_policy == "proposal_only"

    def test_relationship_properties_default_optional_and_string(self):
        rel = RelationshipSchema(
            name="runs",
            from_entity="Asset",
            to_entity="Product",
            properties={"installed_version": PropertySchema()},
        )
        prop = rel.properties["installed_version"]
        assert prop.type == "string"
        assert prop.optional is True

    def test_relationship_tuple_proposal_identity_requires_matching(self):
        with pytest.raises(ValueError, match="proposal_identity"):
            RelationshipSchema(
                name="r",
                from_entity="A",
                to_entity="B",
                proposal_identity="relationship_tuple",
            )

    def test_relationship_tuple_proposal_identity_allows_matching(self):
        rel = RelationshipSchema(
            name="r",
            from_entity="A",
            to_entity="B",
            proposal_policy=ProposalPolicySchema(signals={}),
            proposal_identity="relationship_tuple",
        )
        assert rel.proposal_identity == "relationship_tuple"


class TestRuntimeConfigWritePolicy:
    def test_default_write_policy_defaults_to_direct(self):
        runtime = RuntimeConfigSchema()
        assert runtime.default_write_policy == "direct"

    @pytest.mark.parametrize("policy", ["direct", "proposal_only"])
    def test_default_write_policy_accepts_enum_values(self, policy):
        runtime = RuntimeConfigSchema(default_write_policy=policy)
        assert runtime.default_write_policy == policy

    def test_default_write_policy_rejects_unknown_value(self):
        with pytest.raises(ValidationError, match="Input should be"):
            RuntimeConfigSchema(default_write_policy="maybe")

    def test_parses_under_extra_forbid(self):
        # RuntimeConfigSchema is extra="forbid"; the declared field must parse and
        # unknown sibling keys must still be rejected.
        runtime = RuntimeConfigSchema.model_validate(
            {"trace_payloads": "preview", "default_write_policy": "proposal_only"}
        )
        assert runtime.default_write_policy == "proposal_only"
        with pytest.raises(ValidationError, match="Extra inputs"):
            RuntimeConfigSchema.model_validate({"unknown_key": True})


class TestTraversalStep:
    def test_defaults(self):
        step = TraversalStep(relationship="fits")
        assert step.direction == "outgoing"
        assert step.filter is None
        assert step.constraint is None
        assert step.exclude_if_related == []
        assert step.max_depth == 1

    def test_full(self):
        step = TraversalStep(
            relationship="fits",
            direction="incoming",
            filter={"verified": True},
            constraint="target.year >= 2020",
            exclude_if_related=[
                RelatedExclusionSpec(relationship="retired_fit", direction="incoming")
            ],
            max_depth=2,
        )
        assert step.direction == "incoming"
        assert step.filter == {"verified": True}
        assert step.exclude_if_related[0].relationship == "retired_fit"

    def test_alias_uses_yaml_as_key(self):
        step = TraversalStep.model_validate({"relationship": "fits", "as": "fitment"})
        assert step.alias == "fitment"

    def test_alias_rejects_blank_string(self):
        with pytest.raises(ValidationError, match=r"as must match \[\\w-\]\+"):
            TraversalStep.model_validate({"relationship": "fits", "as": "   "})

    def test_alias_rejects_path_punctuation(self):
        with pytest.raises(ValidationError, match=r"as must match \[\\w-\]\+"):
            TraversalStep.model_validate({"relationship": "fits", "as": "fit.path"})

    def test_related_exclusion_rejects_blank_relationship(self):
        with pytest.raises(ValidationError, match="relationship must be a non-empty string"):
            TraversalStep(
                relationship="fits",
                exclude_if_related=[{"relationship": "   "}],
            )

    def test_related_exclusion_rejects_invalid_direction(self):
        with pytest.raises(ValidationError, match="outgoing|incoming|both"):
            TraversalStep(
                relationship="fits",
                exclude_if_related=[{"relationship": "retired_fit", "direction": "sideways"}],
            )

    def test_where_accepts_structured_predicates(self):
        step = TraversalStep.model_validate(
            {
                "relationship": "fits",
                "where": {
                    "edge.properties.priority": {"in": ["critical", "high"]},
                    "edge.metadata.assertion.lifecycle.status": {"eq": "active"},
                    "candidate.properties.status": {"not_in": ["retired"]},
                    "target.properties.due_by": {"exists": True},
                },
            }
        )

        assert step.where is not None
        assert "edge.properties.priority" in step.where.root

    def test_where_rejects_unknown_operator(self):
        with pytest.raises(ValidationError, match="unsupported predicate operator 'matches'"):
            TraversalStep.model_validate(
                {
                    "relationship": "fits",
                    "where": {"edge.properties.priority": {"matches": "critical"}},
                }
            )

    def test_where_rejects_value_type_operator(self):
        with pytest.raises(ValidationError, match="unsupported predicate operator 'value_type'"):
            TraversalStep.model_validate(
                {
                    "relationship": "fits",
                    "where": {
                        "edge.properties.due_by": {
                            "lte": "$input.cutoff",
                            "value_type": "datetime",
                        }
                    },
                }
            )

    def test_where_rejects_result_scope(self):
        with pytest.raises(
            ValidationError,
            match="result predicates are not supported in where",
        ):
            TraversalStep.model_validate(
                {
                    "relationship": "fits",
                    "where": {"result.properties.status": {"eq": "active"}},
                }
            )

    def test_where_rejects_unscoped_paths(self):
        with pytest.raises(
            ValidationError,
            match="top-level where predicate path 'properties.status' must start with one of",
        ):
            TraversalStep.model_validate(
                {
                    "relationship": "fits",
                    "where": {"properties.status": {"eq": "active"}},
                }
            )

    def test_where_related_accepts_predicate_scopes(self):
        step = TraversalStep.model_validate(
            {
                "relationship": "fits",
                "where_related": [
                    {
                        "relationship": "asset_owned_by",
                        "direction": "outgoing",
                        "target": {"properties.owner_id": {"eq": "$input.owner_id"}},
                    }
                ],
                "where_not_related": [
                    {
                        "relationship": "asset_remediated_vulnerability",
                        "direction": "outgoing",
                        "edge": {"properties.verification_status": {"eq": "verified"}},
                        "target": {"entity_id": {"eq": "$entry.entity_id"}},
                    }
                ],
            }
        )

        assert step.where_related[0].relationship == "asset_owned_by"
        assert step.where_not_related[0].target is not None

    def test_where_related_rejects_blank_relationship(self):
        with pytest.raises(ValidationError, match="relationship must be a non-empty string"):
            TraversalStep.model_validate(
                {
                    "relationship": "fits",
                    "where_related": [{"relationship": "   "}],
                }
            )


class TestNamedQuerySchema:
    def test_mode_is_required(self):
        with pytest.raises(ValidationError, match="mode"):
            NamedQuerySchema(
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", direction="incoming")],
                returns="list[Part]",
            )

    def test_minimal(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
        )
        assert query.entry_point == "Vehicle"
        assert len(query.traversal) == 1
        assert query.result_shape == "path"
        assert query.dedupe == "path"
        assert query.relationship_state == "live"
        assert query.allow_relationship_state_override is False

    def test_collection_query_accepts_entity_collection(self):
        query = NamedQuerySchema(
            mode="collection",
            result_shape="entity",
            returns="Part",
            where={"result.properties.brand": {"eq": "$input.brand"}},
        )

        assert query.mode == "collection"
        assert query.entry_point is None
        assert query.traversal == []
        assert query.dedupe == "entity"

    def test_collection_query_rejects_entry_point(self):
        with pytest.raises(ValidationError, match="must not define entry_point"):
            NamedQuerySchema(
                mode="collection",
                entry_point="Part",
                result_shape="entity",
                returns="Part",
            )

    def test_traversal_query_rejects_top_level_where(self):
        with pytest.raises(ValidationError, match="do not support top-level where"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", direction="incoming")],
                returns="list[Part]",
                where={"result.properties.brand": {"eq": "$input.brand"}},
            )

    def test_relationship_state_accepts_pending_with_override_opt_in(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
            relationship_state="pending",
            allow_relationship_state_override=True,
        )

        assert query.relationship_state == "pending"
        assert query.allow_relationship_state_override is True

    def test_multi_step(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Part",
            traversal=[
                TraversalStep(relationship="replaces", direction="outgoing"),
                TraversalStep(relationship="fits", direction="outgoing"),
            ],
            returns="list[Part]",
        )
        assert len(query.traversal) == 2

    @pytest.mark.parametrize("result_shape", ["entity", "path", "relationship"])
    def test_valid_result_shapes(self, result_shape):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            result_shape=result_shape,
        )
        assert query.result_shape == result_shape

    def test_default_result_shape_is_path_with_path_dedupe(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
        )

        assert query.result_shape == "path"
        assert query.dedupe == "path"

    def test_invalid_result_shape(self):
        with pytest.raises(ValidationError, match="entity|path|relationship"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="table",
            )

    def test_entity_shape_accepts_entity_dedupe(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            result_shape="entity",
            dedupe="entity",
        )
        assert query.dedupe == "entity"

    def test_entity_shape_defaults_to_entity_dedupe(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            result_shape="entity",
        )

        assert query.dedupe == "entity"

    @pytest.mark.parametrize("dedupe", ["entity", "path", "none"])
    def test_path_shape_accepts_dedupe_values(self, dedupe):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            result_shape="path",
            dedupe=dedupe,
        )
        assert query.dedupe == dedupe

    @pytest.mark.parametrize("dedupe", ["path", "none"])
    def test_entity_shape_rejects_path_retaining_dedupe(self, dedupe):
        with pytest.raises(
            ValidationError,
            match="result_shape 'entity' requires dedupe 'entity'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="entity",
                dedupe=dedupe,
            )

    def test_invalid_dedupe_value(self):
        with pytest.raises(ValidationError, match="entity|path|none"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                dedupe="terminal",
            )

    def test_relationship_shape_defaults_to_path_dedupe(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="fits",
            result_shape="relationship",
        )

        assert query.dedupe == "path"

    def test_relationship_shape_rejects_entity_dedupe(self):
        with pytest.raises(
            ValidationError,
            match="result_shape 'relationship' requires dedupe 'path' or 'none'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="fits",
                result_shape="relationship",
                dedupe="entity",
            )

    def test_pending_relationship_state_rejects_entity_shape(self):
        with pytest.raises(
            ValidationError,
            match="relationship_state 'pending' requires result_shape 'path' or 'relationship'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="entity",
                relationship_state="pending",
            )

    def test_pending_relationship_state_uses_path_default(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            relationship_state="pending",
        )

        assert query.result_shape == "path"
        assert query.dedupe == "path"

    def test_pending_relationship_state_rejects_entity_dedupe(self):
        with pytest.raises(
            ValidationError,
            match="relationship_state 'pending' requires dedupe 'path' or 'none'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="path",
                dedupe="entity",
                relationship_state="pending",
            )

    @pytest.mark.parametrize("shape", ["entity", "relationship"])
    def test_reviewable_relationship_state_rejects_non_path_shapes(self, shape):
        with pytest.raises(
            ValidationError,
            match="relationship_state 'reviewable' requires result_shape 'path'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="fits" if shape == "relationship" else "list[Part]",
                result_shape=shape,
                relationship_state="reviewable",
            )

    def test_reviewable_relationship_state_rejects_entity_dedupe(self):
        with pytest.raises(
            ValidationError,
            match="relationship_state 'reviewable' requires dedupe 'path' or 'none'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="path",
                dedupe="entity",
                relationship_state="reviewable",
            )

    def test_reviewable_relationship_state_uses_path_default(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            relationship_state="reviewable",
        )

        assert query.result_shape == "path"
        assert query.dedupe == "path"

    def test_duplicate_traversal_aliases_fail_validation(self):
        with pytest.raises(ValidationError, match="duplicate traversal aliases: hop"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(relationship="fits", alias="hop"),
                    TraversalStep(relationship="replaces", alias="hop"),
                ],
                returns="list[Part]",
            )

    def test_accepts_projection_order_and_limit(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            select={
                "part_id": "$result.entity_id",
                "edge_key": "$path.fit.edge.edge_key",
                "literal": {"mode": "review"},
            },
            order_by=[
                {
                    "by": "$path.fit.edge.properties.due_by",
                    "direction": "asc",
                    "value_type": "date",
                }
            ],
            limit=25,
            max_paths=500,
            max_paths_per_result=20,
        )

        assert query.select is not None
        assert query.order_by[0].by == "$path.fit.edge.properties.due_by"
        assert query.limit == 25
        assert query.max_paths == 500
        assert query.max_paths_per_result == 20

    def test_accepts_include_block(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(
                    relationship="fits",
                    direction="incoming",
                    alias="fit",
                )
            ],
            returns="list[Part]",
            result_shape="path",
            include={
                "replacements": {
                    "from": "$result",
                    "relationship": "replaces",
                    "direction": "incoming",
                    "many": True,
                    "limit": 10,
                    "where": {"edge.properties.confidence": {"gte": 0.8}},
                    "order_by": [
                        {
                            "by": "$edge.properties.confidence",
                            "direction": "desc",
                        }
                    ],
                }
            },
            select={
                "part_id": "$result.entity_id",
                "replacement_count": "$include.replacements.count",
            },
        )

        assert query.include["replacements"].from_ == "$result"
        assert query.include["replacements"].many is True

    def test_include_rejects_unknown_path_alias_anchor(self):
        with pytest.raises(ValidationError, match="unknown traversal alias 'missing'"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="path",
                include={
                    "side": {
                        "from": "$path.missing.source",
                        "relationship": "replaces",
                    }
                },
            )

    def test_include_alias_collision_rejected(self):
        with pytest.raises(ValidationError, match="include aliases must not collide"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="path",
                include={"fit": {"from": "$result", "relationship": "replaces"}},
            )

    def test_include_rejects_entity_shape_without_projection(self):
        with pytest.raises(ValidationError, match="entity queries with include must define select"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="entity",
                include={"side": {"from": "$result", "relationship": "replaces"}},
            )

    def test_include_allows_projected_entity_shape(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits")],
            returns="list[Part]",
            result_shape="entity",
            select={
                "part_id": "$result.entity_id",
                "side_count": "$include.side.count",
            },
            include={
                "side": {
                    "from": "$result",
                    "relationship": "replaces",
                    "many": True,
                }
            },
        )

        assert query.include["side"].from_ == "$result"

    def test_include_order_rejects_query_row_scopes(self):
        with pytest.raises(ValidationError, match="include order_by reference"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="path",
                include={
                    "side": {
                        "from": "$result",
                        "relationship": "replaces",
                        "order_by": [{"by": "$result.entity_id"}],
                    }
                },
            )

    def test_include_order_accepts_input_refs(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", alias="fit")],
            returns="list[Part]",
            result_shape="path",
            include={
                "side": {
                    "from": "$result",
                    "relationship": "replaces",
                    "order_by": [{"by": "$input.priority_order"}],
                }
            },
        )

        assert query.include["side"].order_by[0].by == "$input.priority_order"

    def test_projection_rejects_singular_ref_for_many_include(self):
        with pytest.raises(ValidationError, match="targets many include"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="path",
                include={
                    "side": {
                        "from": "$result",
                        "relationship": "replaces",
                        "many": True,
                    }
                },
                select={"bad": "$include.side.target.entity_id"},
            )

    def test_required_false_defaults_and_is_accepted_for_path_shape(self):
        query = NamedQuerySchema(
            mode="traversal",
            entry_point="Vehicle",
            traversal=[
                TraversalStep(relationship="fits", alias="fit"),
                TraversalStep(relationship="replaces", required=False, alias="replacement"),
            ],
            returns="list[Part]",
            result_shape="path",
        )

        assert query.traversal[0].required is True
        assert query.traversal[1].required is False

    def test_required_false_rejects_entity_shape(self):
        with pytest.raises(
            ValidationError,
            match="required false traversal steps require result_shape 'path' or 'relationship'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", required=False)],
                returns="list[Part]",
                result_shape="entity",
            )

    def test_relationship_shape_rejects_non_required_final_step(self):
        with pytest.raises(
            ValidationError,
            match="final traversal step to be required",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", required=False)],
                returns="fits",
                result_shape="relationship",
            )

    def test_path_budgets_reject_entity_shape(self):
        with pytest.raises(
            ValidationError,
            match="path budgets require result_shape 'path' or 'relationship'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="entity",
                max_paths=10,
            )

    @pytest.mark.parametrize("field", ["max_paths", "max_paths_per_result"])
    def test_path_budgets_reject_non_positive_values(self, field):
        with pytest.raises(ValidationError):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                result_shape="path",
                **{field: 0},
            )

    def test_projection_rejects_unknown_scope(self):
        with pytest.raises(ValidationError, match="unsupported query reference scope"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                select={"bad": "$unknown.value"},
            )

    def test_projection_rejects_unknown_path_alias(self):
        with pytest.raises(ValidationError, match="unknown traversal alias 'missing'"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="path",
                select={"edge_key": "$path.missing.edge.edge_key"},
            )

    def test_projection_rejects_unavailable_shape_scope(self):
        with pytest.raises(
            ValidationError,
            match="not available for result_shape 'entity'",
        ):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits", alias="fit")],
                returns="list[Part]",
                result_shape="entity",
                select={"edge_key": "$path.fit.edge.edge_key"},
            )

    def test_order_by_rejects_non_reference(self):
        with pytest.raises(ValidationError, match="order_by.by must be a query reference"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                order_by=[{"by": "result.entity_id"}],
            )

    def test_order_by_rejects_value_type_and_enum_ref(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                order_by=[
                    {
                        "by": "$result.properties.priority",
                        "value_type": "string",
                        "enum_ref": "priority",
                    }
                ],
            )

    def test_order_by_rejects_blank_enum_ref(self):
        with pytest.raises(ValidationError, match="enum_ref"):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                order_by=[{"by": "$result.properties.priority", "enum_ref": " "}],
            )

    def test_limit_rejects_negative_values(self):
        with pytest.raises(ValidationError):
            NamedQuerySchema(
                mode="traversal",
                entry_point="Vehicle",
                traversal=[TraversalStep(relationship="fits")],
                returns="list[Part]",
                limit=-1,
            )

    def test_relationship_shape_requires_returns_to_match_final_relationship(self):
        with pytest.raises(
            ValidationError,
            match=(
                "Named query 'bad' with result_shape 'relationship' must set returns "
                "to its final relationship type"
            ),
        ):
            CoreConfig(
                name="relationship-return-validation",
                entity_types={
                    "Vehicle": EntityTypeSchema(
                        properties={"vehicle_id": PropertySchema(type="string", primary_key=True)}
                    ),
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(type="string", primary_key=True)}
                    ),
                },
                relationships=[
                    RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
                ],
                named_queries={
                    "bad": NamedQuerySchema(
                        mode="traversal",
                        entry_point="Vehicle",
                        traversal=[TraversalStep(relationship="fits", direction="incoming")],
                        returns="not_fits",
                        result_shape="relationship",
                    )
                },
            )

    def test_where_related_unknown_relationship_fails_config_validation(self):
        with pytest.raises(
            ValidationError,
            match="references unknown relationship 'unknown_owner' in where_related",
        ):
            CoreConfig(
                name="related-predicate-validation",
                entity_types={
                    "Vehicle": EntityTypeSchema(
                        properties={"vehicle_id": PropertySchema(type="string", primary_key=True)}
                    ),
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(type="string", primary_key=True)}
                    ),
                },
                relationships=[
                    RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
                ],
                named_queries={
                    "bad": NamedQuerySchema(
                        mode="traversal",
                        entry_point="Vehicle",
                        traversal=[
                            TraversalStep.model_validate(
                                {
                                    "relationship": "fits",
                                    "where_related": [
                                        {
                                            "relationship": "unknown_owner",
                                            "direction": "outgoing",
                                        }
                                    ],
                                }
                            )
                        ],
                        returns="list[Part]",
                    )
                },
            )


def _config_with_contract_json_schema(schema_yaml: str, *, enums: str = "") -> str:
    return f"""
version: "1.0"
name: nested_json_schema
{dedent(enums).lstrip()}entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
relationships: []
contracts:
  Input:
    fields:
      payload:
        type: json
        json_schema:
{indent(dedent(schema_yaml).strip(), "          ")}
"""


class TestCoreConfigQueryValidation:
    def test_accepts_enum_ref(self):
        config = CoreConfig(
            name="test",
            enums={"status": EnumSchema(values=["active", "retired"])},
            entity_types={
                "Thing": EntityTypeSchema(
                    properties={
                        "thing_id": PropertySchema(type="string", primary_key=True),
                        "status": PropertySchema(type="string", enum_ref="status"),
                    }
                )
            },
            relationships=[],
        )
        assert config.entity_types["Thing"].properties["status"].enum_ref == "status"

    def test_collection_entity_query_accepts_known_returns(self):
        config = CoreConfig(
            name="collection_entity_returns",
            entity_types={
                "Part": EntityTypeSchema(
                    properties={"part_number": PropertySchema(primary_key=True)}
                )
            },
            named_queries={
                "parts": NamedQuerySchema(
                    mode="collection",
                    result_shape="entity",
                    returns="Part",
                ),
                "listed_parts": NamedQuerySchema(
                    mode="collection",
                    result_shape="entity",
                    returns="list[Part]",
                ),
            },
        )

        assert config.named_queries["parts"].returns == "Part"
        assert config.named_queries["listed_parts"].returns == "list[Part]"

    def test_collection_entity_query_rejects_unknown_returns(self):
        with pytest.raises(
            ValidationError,
            match=(
                "Named query 'missing_parts' with collection entity collection "
                "returns unknown entity 'MissingPart'"
            ),
        ):
            CoreConfig(
                name="collection_entity_returns",
                entity_types={
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(primary_key=True)}
                    )
                },
                named_queries={
                    "missing_parts": NamedQuerySchema(
                        mode="collection",
                        result_shape="entity",
                        returns="MissingPart",
                    )
                },
            )

    def test_query_order_by_accepts_ordered_enum_ref(self):
        config = CoreConfig(
            name="test",
            enums={
                "priority": EnumSchema(
                    values=["low", "medium", "high"],
                    ordered="low_to_high",
                )
            },
            entity_types={
                "Vehicle": EntityTypeSchema(
                    properties={"vehicle_id": PropertySchema(primary_key=True)}
                ),
                "Part": EntityTypeSchema(
                    properties={"part_number": PropertySchema(primary_key=True)}
                ),
            },
            relationships=[
                RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
            ],
            named_queries={
                "ordered": NamedQuerySchema(
                    mode="traversal",
                    entry_point="Vehicle",
                    traversal=[TraversalStep(relationship="fits", direction="incoming")],
                    returns="list[Part]",
                    order_by=[
                        {
                            "by": "$result.properties.priority",
                            "enum_ref": "priority",
                        }
                    ],
                    include={
                        "side": {
                            "from": "$result",
                            "relationship": "fits",
                            "direction": "outgoing",
                            "order_by": [
                                {
                                    "by": "$edge.properties.priority",
                                    "enum_ref": "priority",
                                }
                            ],
                        }
                    },
                )
            },
        )

        assert config.named_queries["ordered"].order_by[0].enum_ref == "priority"

    def test_query_order_by_rejects_unknown_enum_ref(self):
        with pytest.raises(ValidationError, match="unknown enum_ref 'priority'"):
            CoreConfig(
                name="test",
                entity_types={
                    "Vehicle": EntityTypeSchema(
                        properties={"vehicle_id": PropertySchema(primary_key=True)}
                    ),
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(primary_key=True)}
                    ),
                },
                relationships=[
                    RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
                ],
                named_queries={
                    "ordered": NamedQuerySchema(
                        mode="traversal",
                        entry_point="Vehicle",
                        traversal=[TraversalStep(relationship="fits", direction="incoming")],
                        returns="list[Part]",
                        order_by=[
                            {
                                "by": "$result.properties.priority",
                                "enum_ref": "priority",
                            }
                        ],
                    )
                },
            )

    def test_query_order_by_rejects_unordered_enum_ref(self):
        with pytest.raises(ValidationError, match="enum is not ordered"):
            CoreConfig(
                name="test",
                enums={"priority": EnumSchema(values=["low", "high"])},
                entity_types={
                    "Vehicle": EntityTypeSchema(
                        properties={"vehicle_id": PropertySchema(primary_key=True)}
                    ),
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(primary_key=True)}
                    ),
                },
                relationships=[
                    RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
                ],
                named_queries={
                    "ordered": NamedQuerySchema(
                        mode="traversal",
                        entry_point="Vehicle",
                        traversal=[TraversalStep(relationship="fits", direction="incoming")],
                        returns="list[Part]",
                        include={
                            "side": {
                                "from": "$result",
                                "relationship": "fits",
                                "direction": "outgoing",
                                "order_by": [
                                    {
                                        "by": "$edge.properties.priority",
                                        "enum_ref": "priority",
                                    }
                                ],
                            }
                        },
                    )
                },
            )

    def test_accepts_nested_json_schema_enum_ref(self):
        config = load_config_from_string(
            _config_with_contract_json_schema(
                """
                type: array
                items:
                  type: object
                  properties:
                    verdict:
                      type: string
                      enum_ref: verdict
                """,
                enums="""
enums:
  verdict:
    values: [support, contradict]
""",
            )
        )

        schema = config.contracts["Input"].fields["payload"].json_schema
        assert schema is not None
        assert schema["items"]["properties"]["verdict"]["enum_ref"] == "verdict"

    def test_rejects_missing_nested_json_schema_enum_ref(self):
        with pytest.raises(ConfigError, match="enum_ref 'verdict' is not defined"):
            load_config_from_string(
                _config_with_contract_json_schema(
                    """
                    type: object
                    properties:
                      verdict:
                        type: string
                        enum_ref: verdict
                    """
                )
            )

    def test_rejects_nested_json_schema_enum_and_enum_ref(self):
        with pytest.raises(ConfigError, match="mutually exclusive"):
            load_config_from_string(
                _config_with_contract_json_schema(
                    """
                    type: object
                    properties:
                      verdict:
                        type: string
                        enum: [support]
                        enum_ref: verdict
                    """,
                    enums="""
enums:
  verdict:
    values: [support, contradict]
""",
                )
            )

    def test_rejects_unsupported_nested_json_schema_keyword(self):
        with pytest.raises(ConfigError, match="unsupported json_schema keyword"):
            load_config_from_string(
                _config_with_contract_json_schema(
                    """
                    type: object
                    additionalProperties: false
                    """
                )
            )

    @pytest.mark.parametrize(
        ("schema_yaml", "message"),
        [
            ("properties: []", "properties"),
            ("type: array\nitems: []", "items"),
            ("type: object\nrequired: id", "required"),
            ("enum: []", "enum"),
        ],
    )
    def test_rejects_malformed_nested_json_schema_keywords(
        self,
        schema_yaml: str,
        message: str,
    ):
        with pytest.raises(ConfigError, match=message):
            load_config_from_string(_config_with_contract_json_schema(schema_yaml))

    @pytest.mark.parametrize("type_name", ["int", "json"])
    def test_rejects_top_level_enum_ref_on_non_string_properties(self, type_name: str):
        with pytest.raises(ConfigError, match="enum_ref is only allowed"):
            load_config_from_string(
                f"""
version: "1.0"
name: enum_ref_type_check
enums:
  status:
    values: [open, closed]
entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
      status:
        type: {type_name}
        enum_ref: status
relationships: []
"""
            )

    def test_rejects_missing_enum_ref(self):
        with pytest.raises(ValidationError, match="enum_ref 'status' is not defined"):
            CoreConfig(
                name="test",
                entity_types={
                    "Thing": EntityTypeSchema(
                        properties={
                            "thing_id": PropertySchema(type="string", primary_key=True),
                            "status": PropertySchema(type="string", enum_ref="status"),
                        }
                    )
                },
                relationships=[],
            )

    def test_rejects_enum_ref_default_outside_values(self):
        with pytest.raises(ValidationError, match="default must be one of enum_ref"):
            CoreConfig(
                name="test",
                enums={"status": EnumSchema(values=["active", "retired"])},
                entity_types={
                    "Thing": EntityTypeSchema(
                        properties={
                            "thing_id": PropertySchema(type="string", primary_key=True),
                            "status": PropertySchema(
                                type="string",
                                enum_ref="status",
                                default="unknown",
                            ),
                        }
                    )
                },
                relationships=[],
            )

    def test_rejects_unknown_related_exclusion_relationship(self):
        with pytest.raises(ValidationError, match="exclude_if_related"):
            CoreConfig(
                name="test",
                entity_types={
                    "Vehicle": EntityTypeSchema(
                        properties={"vehicle_id": PropertySchema(type="string", primary_key=True)}
                    ),
                    "Part": EntityTypeSchema(
                        properties={"part_number": PropertySchema(type="string", primary_key=True)}
                    ),
                },
                relationships=[
                    RelationshipSchema(name="fits", from_entity="Part", to_entity="Vehicle")
                ],
                named_queries={
                    "parts_for_vehicle": NamedQuerySchema(
                        mode="traversal",
                        entry_point="Vehicle",
                        traversal=[
                            TraversalStep(
                                relationship="fits",
                                direction="incoming",
                                exclude_if_related=[
                                    RelatedExclusionSpec(
                                        relationship="suppressed_fit",
                                        direction="incoming",
                                    )
                                ],
                            )
                        ],
                        returns="list[Part]",
                    )
                },
            )


class TestConstraintSchema:
    def test_defaults(self):
        c = ConstraintSchema(name="test", rule="a == b")
        assert c.severity == "warning"

    def test_error_severity(self):
        c = ConstraintSchema(name="test", rule="a == b", severity="error")
        assert c.severity == "error"


class TestQualityCheckSchema:
    def test_property_check_parses(self):
        check = PropertyQualityCheck(
            name="non_empty_name",
            target="entity",
            entity_type="Vendor",
            property="name",
            rule="non_empty",
        )
        assert check.kind == "property"

    def test_json_content_check_parses(self):
        check = JsonContentQualityCheck(
            name="no_empty_json",
            target="relationship",
            relationship_type="vulnerability_affects_product",
            property="affected_versions",
            rule="no_empty_objects_in_array",
        )
        assert check.kind == "json_content"

    def test_uniqueness_requires_properties(self):
        with pytest.raises(ValidationError, match="at least one property"):
            UniquenessQualityCheck(name="unique", entity_type="Product", properties=[])

    def test_bounds_requires_a_limit(self):
        with pytest.raises(ValidationError, match="min_count, max_count, or both"):
            BoundsQualityCheck(name="bounds", target="entity_count", entity_type="Product")

    def test_cardinality_requires_a_limit(self):
        with pytest.raises(ValidationError, match="min_count, max_count, or both"):
            CardinalityQualityCheck(
                name="cardinality",
                entity_type="Product",
                relationship_type="product_from_vendor",
                direction="outgoing",
            )

    def test_relationship_property_consistency_parses(self):
        check = RelationshipPropertyConsistencyQualityCheck(
            name="product_vendor_id_matches",
            entity_type="Product",
            relationship_type="product_from_vendor",
            direction="outgoing",
            source_property="vendor_id",
            target_property="vendor_id",
        )
        assert check.kind == "relationship_property_consistency"

    def test_named_query_result_count_check_parses(self):
        check = NamedQueryResultCountQualityCheck(
            name="no_deferred_release_gating_work",
            query_name="deferred_release_gating_work_items",
            params={"release_line_id": "release-0.2"},
            max_count=0,
        )
        assert check.kind == "named_query_result_count"

    def test_named_query_result_count_requires_a_limit(self):
        with pytest.raises(ValidationError, match="min_count, max_count, or both"):
            NamedQueryResultCountQualityCheck(
                name="missing_limit",
                query_name="some_query",
            )


class TestMutationGuardSchema:
    def test_guard_parses_with_only_load_bearing_fields(self):
        guard = MutationGuardSchema(
            name="closed_requires_review",
            entity_type="WorkItem",
            property="status",
            new_value="closed",
            condition=NamedQueryResultCountGuardCondition(
                type="query",
                query_name="approved_review",
                params={"work_item_id": "$entity.entity_id"},
                min_count=1,
            ),
            message="Approved review required.",
        )

        assert guard.new_value == "closed"
        assert guard.condition.query_name == "approved_review"

    def test_guard_parses_actor_identity_condition(self):
        guard = MutationGuardSchema(
            name="approval_requires_authorized_actor",
            entity_type="ReviewRequest",
            property="status",
            new_value="approved",
            condition=ActorIdentityGuardCondition(type="actor", allowed_actor_ids=[" robert "]),
            message="Authorized approver required.",
        )

        assert isinstance(guard.condition, ActorIdentityGuardCondition)
        assert guard.condition.allowed_actor_ids == ["robert"]

    def test_guard_parses_relationship_evidence_condition(self):
        guard = MutationGuardSchema(
            name="fitment_requires_source_evidence",
            relationship_type="fits",
            condition=EvidenceRequirementGuardCondition(
                type="evidence",
                require_evidence="source_evidence",
                min_count=2,
            ),
            message="Fitment observations require source evidence.",
        )

        assert guard.relationship_type == "fits"
        assert isinstance(guard.condition, EvidenceRequirementGuardCondition)
        assert guard.condition.min_count == 2

    def test_guard_parses_co_write_condition(self):
        guard = MutationGuardSchema(
            name="closed_requires_co_written_review",
            entity_type="WorkItem",
            property="status",
            new_value="closed",
            condition=CoWriteGuardCondition(
                type="co_write",
                requires=CoWriteRequirement(
                    entity_type="Review",
                    via_relationship="review_for_work_item",
                ),
            ),
            message="A co-written review is required to close.",
        )

        assert isinstance(guard.condition, CoWriteGuardCondition)
        assert guard.condition.requires.entity_type == "Review"
        assert guard.condition.requires.via_relationship == "review_for_work_item"
        assert guard.condition.requires.kind is None

    def test_guard_parses_list_new_value(self):
        guard = MutationGuardSchema(
            name="terminal_status_requires_review",
            entity_type="ReviewRequest",
            property="status",
            new_value=["changes_requested", "approved", "withdrawn"],
            condition=ActorIdentityGuardCondition(type="actor", allowed_actor_ids=["robert"]),
            message="Terminal transitions require an authorized actor.",
        )

        assert guard.new_value == ["changes_requested", "approved", "withdrawn"]

    def test_guard_condition_requires_type_discriminator(self):
        with pytest.raises(ValidationError, match="discriminator"):
            MutationGuardSchema.model_validate(
                {
                    "name": "missing_type",
                    "entity_type": "WorkItem",
                    "property": "status",
                    "new_value": "closed",
                    "condition": {
                        "query_name": "approved_review",
                        "min_count": 1,
                    },
                }
            )

    def test_guard_actor_identity_condition_requires_allowed_actor_ids(self):
        with pytest.raises(ValidationError, match="List should have at least 1 item"):
            ActorIdentityGuardCondition(type="actor", allowed_actor_ids=[])

    def test_guard_actor_identity_condition_rejects_blank_actor_id(self):
        with pytest.raises(ValidationError, match="non-empty"):
            ActorIdentityGuardCondition(type="actor", allowed_actor_ids=[" "])

    def test_guard_actor_identity_condition_rejects_duplicate_actor_id(self):
        with pytest.raises(ValidationError, match="duplicate allowed_actor_ids"):
            ActorIdentityGuardCondition(type="actor", allowed_actor_ids=["robert", " robert "])

    def test_guard_condition_requires_a_limit(self):
        with pytest.raises(ValidationError, match="min_count, max_count, or both"):
            NamedQueryResultCountGuardCondition(type="query", query_name="approved_review")

    def test_guard_evidence_condition_requires_positive_min_count(self):
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            EvidenceRequirementGuardCondition(
                type="evidence",
                require_evidence="source_evidence",
                min_count=0,
            )

    def test_guard_evidence_condition_requires_relationship_type(self):
        with pytest.raises(ValidationError, match="relationship_type"):
            MutationGuardSchema(
                name="missing_relationship",
                condition=EvidenceRequirementGuardCondition(
                    type="evidence",
                    require_evidence="source_evidence",
                ),
            )

    def test_guard_evidence_condition_rejects_entity_fields(self):
        with pytest.raises(ValidationError, match="entity-property fields"):
            MutationGuardSchema(
                name="mixed_scope",
                relationship_type="fits",
                entity_type="Part",
                condition=EvidenceRequirementGuardCondition(
                    type="evidence",
                    require_evidence="source_evidence",
                ),
            )

    def test_guard_rejects_retired_discriminator_fields(self):
        # operation/effect were removed pre-0.2 freeze; they remain rejected as
        # extras on the guard even though the condition now carries a `type`.
        for extra in ({"operation": "entity_update"}, {"effect": "reject"}):
            with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
                MutationGuardSchema(
                    name="retired_field",
                    entity_type="WorkItem",
                    property="status",
                    new_value="closed",
                    condition=NamedQueryResultCountGuardCondition(
                        type="query",
                        query_name="approved_review",
                        min_count=1,
                    ),
                    **extra,
                )

    def test_guard_condition_rejects_retired_kind_field(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            NamedQueryResultCountGuardCondition(
                type="query",
                kind="named_query_result_count",
                query_name="approved_review",
                min_count=1,
            )


class TestWorkflowSchema:
    def test_query_step_requires_alias(self):
        with pytest.raises(ValidationError, match="require 'as'"):
            WorkflowStepSchema(id="context", query="get_context")

    def test_provider_step_forbids_params(self):
        with pytest.raises(ValidationError, match="may not define 'params'"):
            WorkflowStepSchema(
                id="lift",
                provider="predictor",
                params={"sku": "x"},
                input={"sku": "x"},
                **{"as": "lift"},
            )

    def test_assert_step_shape(self):
        step = WorkflowStepSchema(
            id="gate",
            **{
                "assert": AssertSpec(left="$steps.score", op="gte", right=0.5, message="Too low"),
            },
        )
        assert step.assert_spec is not None
        assert step.assert_spec.op == "gte"

    def test_guard_step_shapes(self):
        not_truncated = WorkflowStepSchema(
            id="complete",
            assert_not_truncated=AssertNotTruncatedSpec(step="rows"),
        )
        count = WorkflowStepSchema(
            id="count",
            assert_count=AssertCountSpec(
                step="rows",
                count="returned_results",
                op="gt",
                value=0,
            ),
        )
        exists = WorkflowStepSchema(
            id="exists",
            assert_exists=AssertExistsSpec(ref="$steps.rows.items[0].entity_id"),
        )

        assert not_truncated.assert_not_truncated is not None
        assert count.assert_count is not None
        assert exists.assert_exists is not None

    def test_assert_count_rejects_unknown_count_selector(self):
        with pytest.raises(ValidationError, match="Input should be"):
            WorkflowStepSchema(
                id="count",
                assert_count={
                    "step": "rows",
                    "count": "unknown_count",
                    "op": "gt",
                    "value": 0,
                },
            )

    def test_assert_exists_rejects_literal_ref(self):
        for ref in ("owner.email", "$steps", "$input.", "$steps."):
            with pytest.raises(ValidationError):
                WorkflowStepSchema(
                    id="exists",
                    assert_exists={"ref": ref},
                )

    def test_apply_all_step_shape(self):
        step = WorkflowStepSchema(
            id="apply_all_state",
            apply_all={
                "entities_from": ["vendors", "products"],
                "relationships_from": ["product_vendor"],
            },
            **{"as": "apply_all_state"},
        )

        assert step.apply_all is not None
        assert step.apply_all.entities_from == ["vendors", "products"]
        assert step.apply_all.relationships_from == ["product_vendor"]

    def test_apply_all_requires_inputs(self):
        with pytest.raises(ValidationError, match="apply_all requires"):
            WorkflowStepSchema(id="apply_all_state", apply_all={}, **{"as": "apply_all_state"})

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("as", "guard", "Assert workflow steps may not define 'as'"),
            ("params", {"x": 1}, "Assert workflow steps may not define 'params'"),
            ("input", {"x": 1}, "Assert workflow steps may not define 'input'"),
        ],
    )
    @pytest.mark.parametrize(
        "guard",
        [
            {"assert_not_truncated": {"step": "rows"}},
            {
                "assert_count": {
                    "step": "rows",
                    "count": "returned_results",
                    "op": "gt",
                    "value": 0,
                }
            },
            {"assert_exists": {"ref": "$steps.rows.items[0].entity_id"}},
        ],
    )
    def test_guard_steps_reject_output_and_io_fields(
        self,
        guard: dict[str, object],
        field: str,
        value: object,
        message: str,
    ):
        with pytest.raises(ValidationError, match=message):
            WorkflowStepSchema(id="guard", **guard, **{field: value})

    def test_workflow_requires_contract_in(self):
        workflow = WorkflowSchema(
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="context",
                    query="get_context",
                    params={"sku": "$input.sku"},
                    **{"as": "context"},
                )
            ],
            returns="context",
        )
        assert workflow.contract_in == "PromoInput"

    def test_workflow_accepts_optional_contract_out(self):
        workflow = WorkflowSchema(
            contract_in="PromoInput",
            contract_out="PromoOutput",
            steps=[
                WorkflowStepSchema(
                    id="context",
                    query="get_context",
                    params={"sku": "$input.sku"},
                    **{"as": "context"},
                )
            ],
            returns="context",
        )

        assert workflow.contract_out == "PromoOutput"

    def test_validate_config_rejects_unknown_workflow_contract_out(self):
        config = CoreConfig(
            name="workflow_contracts",
            entity_types={
                "Product": EntityTypeSchema(
                    properties={"sku": PropertySchema(type="string", primary_key=True)}
                )
            },
            contracts={"PromoInput": ContractSchema(fields={})},
            workflows={
                "list_products": WorkflowSchema(
                    contract_in="PromoInput",
                    contract_out="MissingOutput",
                    steps=[
                        WorkflowStepSchema(
                            id="products",
                            query={
                                "mode": "collection",
                                "result_shape": "entity",
                                "returns": "Product",
                            },
                            **{"as": "products"},
                        )
                    ],
                    returns="products",
                )
            },
        )

        with pytest.raises(ConfigError, match="contract_out 'MissingOutput' not found"):
            validate_config(config)

    @pytest.mark.parametrize(
        "workflow_type",
        ["utility", "canonical", "decision_support", "proposal"],
    )
    def test_workflow_accepts_type(
        self,
        workflow_type: str,
    ):
        workflow = WorkflowSchema(
            type=workflow_type,  # type: ignore[arg-type]
            contract_in="PromoInput",
            steps=[
                WorkflowStepSchema(
                    id="context",
                    query="get_context",
                    params={"sku": "$input.sku"},
                    **{"as": "context"},
                )
            ],
            returns="context",
        )
        assert workflow.type == workflow_type

    @pytest.mark.parametrize(
        "removed_field",
        [{"purpose": "proposal"}, {"canonical": True}],
    )
    def test_workflow_rejects_removed_type_fields(self, removed_field: dict[str, object]):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WorkflowSchema(
                **removed_field,
                contract_in="PromoInput",
                steps=[
                    WorkflowStepSchema(
                        id="context",
                        query="get_context",
                        params={"sku": "$input.sku"},
                        **{"as": "context"},
                    )
                ],
                returns="context",
            )

    def test_make_candidates_step_accepts_item_refs(self):
        step = WorkflowStepSchema(
            id="candidates",
            make_candidates={
                "relationship_type": "recommended_for",
                "items": "$steps.rows.items",
                "from_type": "Campaign",
                "from_id": "$input.campaign_id",
                "to_type": "Product",
                "to_id": "$item.product_sku",
                "properties": {"reason": "$item.reason"},
                "evidence": {
                    "refs": "$item.evidence_refs",
                    "rationale": "$item.reason",
                },
            },
            **{"as": "candidates"},
        )
        assert step.make_candidates is not None
        assert step.make_candidates.relationship_type == "recommended_for"
        assert step.make_candidates.evidence is not None
        assert step.make_candidates.evidence.refs == "$item.evidence_refs"

    def test_inline_entity_query_step_accepts_predicate_refs(self):
        step = WorkflowStepSchema(
            id="products",
            query={
                "mode": "collection",
                "result_shape": "entity",
                "returns": "Product",
                "where": {"result.properties.category": {"eq": "$input.category"}},
                "limit": 5,
            },
            **{"as": "products"},
        )
        assert step.query is not None
        assert not isinstance(step.query, str)
        assert step.query.returns == "Product"

    def test_inline_relationship_query_step_accepts_predicate_refs(self):
        step = WorkflowStepSchema(
            id="links",
            query={
                "mode": "collection",
                "result_shape": "relationship",
                "returns": "recommended_for",
                "where": {"edge.properties.status": {"eq": "$input.status"}},
            },
            **{"as": "links"},
        )
        assert step.query is not None
        assert not isinstance(step.query, str)
        assert step.query.returns == "recommended_for"

    def test_query_step_accepts_relationship_state_and_source_output_options(self):
        step = WorkflowStepSchema(
            id="review_rows",
            query="reviewable_recommendations",
            params={"campaign_id": "$input.campaign_id"},
            relationship_state="$input.relationship_state",
            include_source=True,
            **{"as": "review_rows"},
        )

        assert step.relationship_state == "$input.relationship_state"
        assert step.include_source is True

    def test_shape_items_step_accepts_projection_config(self):
        step = WorkflowStepSchema(
            id="shaped",
            shape_items={
                "items": "$steps.parsed.tables.assets.rows",
                "include_input": False,
                "rename": {"tags_json": "tags"},
                "fields": {
                    "asset_id": "$item.asset_id",
                    "priority": "$item.priority",
                },
                "casts": {"priority": "int", "tags": "json"},
                "required": ["asset_id"],
            },
            **{"as": "shaped"},
        )
        assert step.shape_items is not None
        assert step.shape_items.casts["priority"] == "int"

    def test_shape_items_requires_projection_when_not_including_input(self):
        with pytest.raises(ValidationError, match="must define fields or rename"):
            WorkflowStepSchema(
                id="shaped",
                shape_items={"items": "$steps.rows.items", "include_input": False},
                **{"as": "shaped"},
            )

    def test_shape_items_rejects_invalid_cast_type(self):
        with pytest.raises(ValidationError):
            WorkflowStepSchema(
                id="shaped",
                shape_items={
                    "items": "$steps.rows.items",
                    "fields": {"priority": "$item.priority"},
                    "casts": {"priority": "date"},
                },
                **{"as": "shaped"},
            )

    def test_filter_items_rejects_invalid_comparison_op(self):
        with pytest.raises(ValidationError):
            WorkflowStepSchema(
                id="filtered",
                filter_items={
                    "items": "$steps.rows.items",
                    "comparisons": [{"left": "$item.score", "op": "contains", "right": 1}],
                },
                **{"as": "filtered"},
            )

    def test_aggregate_items_step_accepts_grouped_measure_config(self):
        step = WorkflowStepSchema(
            id="aggregate",
            aggregate_items={
                "items": "$steps.rows.items",
                "group_by": {"owner_id": "$item.owner_id"},
                "measures": {
                    "row_count": {"count": True},
                    "critical_count": {
                        "count_where": {
                            "left": "$item.priority",
                            "op": "eq",
                            "right": "critical",
                        }
                    },
                    "asset_count": {"count_distinct": {"value": "$item.asset_id"}},
                    "max_score": {"max": {"value": "$item.score", "value_type": "number"}},
                },
            },
            **{"as": "aggregate"},
        )

        assert step.aggregate_items is not None
        assert step.aggregate_items.measures["row_count"].operation == "count"
        assert step.aggregate_items.measures["max_score"].operation == "max"

    def test_aggregate_items_requires_alias_and_non_empty_measures(self):
        with pytest.raises(ValidationError, match="require 'as'"):
            WorkflowStepSchema(
                id="aggregate",
                aggregate_items={
                    "items": "$steps.rows.items",
                    "measures": {"row_count": {"count": True}},
                },
            )

        with pytest.raises(ValidationError, match="measures must not be empty"):
            WorkflowStepSchema(
                id="aggregate",
                aggregate_items={"items": "$steps.rows.items", "measures": {}},
                **{"as": "aggregate"},
            )

    @pytest.mark.parametrize(
        "measure",
        [
            {},
            {"count": True, "sum": {"value": "$item.score"}},
            {"count": False},
        ],
    )
    def test_aggregate_items_measure_requires_one_operation(self, measure):
        with pytest.raises(ValidationError, match="aggregate measure"):
            WorkflowStepSchema(
                id="aggregate",
                aggregate_items={
                    "items": "$steps.rows.items",
                    "measures": {"bad": measure},
                },
                **{"as": "aggregate"},
            )

    def test_aggregate_items_rejects_group_measure_field_collision(self):
        with pytest.raises(ValidationError, match="field\\(s\\) collide"):
            WorkflowStepSchema(
                id="aggregate",
                aggregate_items={
                    "items": "$steps.rows.items",
                    "group_by": {"owner_id": "$item.owner_id"},
                    "measures": {"owner_id": {"count": True}},
                },
                **{"as": "aggregate"},
            )

    def test_dedupe_items_requires_keys_and_rank_for_ranked_strategies(self):
        with pytest.raises(ValidationError, match="keys must not be empty"):
            WorkflowStepSchema(
                id="deduped",
                dedupe_items={"items": "$steps.rows.items", "keys": []},
                **{"as": "deduped"},
            )
        with pytest.raises(ValidationError, match="requires rank"):
            WorkflowStepSchema(
                id="deduped",
                dedupe_items={
                    "items": "$steps.rows.items",
                    "keys": ["$item.id"],
                    "strategy": "max",
                },
                **{"as": "deduped"},
            )

    def test_map_signals_requires_exactly_one_mapping_mode(self):
        with pytest.raises(ValidationError, match="exactly one of 'score' or 'enum'"):
            WorkflowStepSchema(
                id="catalog_signals",
                map_signals={
                    "signal_source": "catalog",
                    "items": "$steps.rows.items",
                    "from_id": "$input.campaign_id",
                    "to_id": "$item.product_sku",
                },
                **{"as": "signals"},
            )

    def test_map_signals_accepts_evidence_refs(self):
        step = WorkflowStepSchema(
            id="catalog_signals",
            map_signals={
                "signal_source": "catalog",
                "items": "$steps.rows.items",
                "from_id": "$input.campaign_id",
                "to_id": "$item.product_sku",
                "evidence": "$item.reason",
                "evidence_refs": "$item.evidence_refs",
                "enum": {
                    "path": "verdict",
                    "map": {"support": "support"},
                },
            },
            **{"as": "signals"},
        )
        assert step.map_signals is not None
        assert step.map_signals.evidence_refs == "$item.evidence_refs"

    def test_propose_relationship_group_step_accepts_signal_aliases(self):
        step = WorkflowStepSchema(
            id="proposal",
            propose_relationship_group={
                "relationship_type": "recommended_for",
                "candidates_from": "candidates",
                "signals_from": ["catalog_signals"],
                "thesis_text": "Recommend products for campaign",
            },
            **{"as": "proposal"},
        )
        assert step.propose_relationship_group is not None
        assert step.propose_relationship_group.signals_from == ["catalog_signals"]

    def test_propose_relationship_group_step_accepts_pending_refresh_mode(self):
        step = WorkflowStepSchema(
            id="proposal",
            propose_relationship_group={
                "relationship_type": "recommended_for",
                "candidates_from": "candidates",
                "signals_from": ["catalog_signals"],
                "pending_refresh_mode": "retain_missing",
            },
            **{"as": "proposal"},
        )
        assert step.propose_relationship_group is not None
        assert step.propose_relationship_group.pending_refresh_mode == "retain_missing"

    def test_propose_relationship_group_step_accepts_on_empty_complete(self):
        step = WorkflowStepSchema(
            id="proposal",
            propose_relationship_group={
                "relationship_type": "recommended_for",
                "candidates_from": "candidates",
                "signals_from": [],
                "on_empty": "complete",
            },
            **{"as": "proposal"},
        )

        assert step.propose_relationship_group is not None
        assert step.propose_relationship_group.on_empty == "complete"

    def test_propose_relationship_group_step_rejects_thesis_facts(self):
        with pytest.raises(ValidationError):
            WorkflowStepSchema(
                id="proposal",
                propose_relationship_group={
                    "relationship_type": "recommended_for",
                    "candidates_from": "candidates",
                    "signals_from": ["catalog_signals"],
                    "thesis_facts": {"rule_id": "caller_authored"},
                },
                **{"as": "proposal"},
            )

    def test_propose_relationship_group_step_rejects_unknown_on_empty(self):
        with pytest.raises(ValidationError):
            WorkflowStepSchema(
                id="proposal",
                propose_relationship_group={
                    "relationship_type": "recommended_for",
                    "candidates_from": "candidates",
                    "signals_from": [],
                    "on_empty": "skip",
                },
                **{"as": "proposal"},
            )

    def test_workflow_rejects_removed_proposal_output(self):
        with pytest.raises(ValidationError, match="proposal_output"):
            WorkflowSchema(
                contract_in="PromoInput",
                steps=[
                    WorkflowStepSchema(
                        id="recommend",
                        provider="recommender",
                        input={"sku": "$input.sku"},
                        **{"as": "recommendations"},
                    )
                ],
                returns="recommendations",
                proposal_output={
                    "kind": "relationship_group",
                    "relationship_type": "recommended_for",
                },
            )


class TestWorkflowTests:
    def test_expectation_normalizes_provider_list(self):
        expect = WorkflowTestExpectSchema(receipt_contains_provider="lift_predictor")
        assert expect.required_providers == ["lift_predictor"]

    def test_workflow_test_schema(self):
        test_case = WorkflowTestSchema(
            name="smoke",
            workflow="evaluate_promo",
            input={"sku": "SKU-1"},
        )
        assert test_case.name == "smoke"
        assert test_case.expect.required_providers == []


class TestCoreConfig:
    def test_minimal_config(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "Thing": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                )
            },
            relationships=[],
        )
        assert config.name == "test"
        assert config.version == "1.0"
        assert config.named_queries == {}
        assert config.constraints == []
        assert config.runtime.trace_payloads == "preview"

    @pytest.mark.parametrize("retention", ["full", "preview", "metadata"])
    def test_runtime_trace_payload_retention_accepts_supported_values(
        self,
        retention: str,
    ):
        runtime = RuntimeConfigSchema(trace_payloads=retention)
        assert runtime.trace_payloads == retention

    def test_runtime_trace_payload_retention_rejects_unknown_value(self):
        with pytest.raises(ValidationError, match="Input should be"):
            RuntimeConfigSchema(trace_payloads="external")

    def test_runtime_mutation_payload_retention_defaults_to_metadata(self):
        runtime = RuntimeConfigSchema()
        assert runtime.mutation_payloads == "metadata"

    @pytest.mark.parametrize("retention", ["full", "preview", "metadata"])
    def test_runtime_mutation_payload_retention_accepts_supported_values(
        self,
        retention: str,
    ):
        runtime = RuntimeConfigSchema(mutation_payloads=retention)
        assert runtime.mutation_payloads == retention

    def test_runtime_mutation_payload_retention_rejects_unknown_value(self):
        with pytest.raises(ValidationError, match="Input should be"):
            RuntimeConfigSchema(mutation_payloads="external")

    def test_get_relationship(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "A": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                ),
                "B": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                ),
            },
            relationships=[
                RelationshipSchema(name="links", from_entity="A", to_entity="B"),
            ],
        )
        assert config.get_relationship("links") is not None
        assert config.get_relationship("missing") is None

    def test_resolve_relationship_reference(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "A": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                ),
                "B": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                ),
            },
            relationships=[
                RelationshipSchema(
                    name="links",
                    from_entity="A",
                    to_entity="B",
                    reverse_name="linked_from",
                ),
            ],
        )
        assert config.resolve_relationship_reference("links") is not None
        assert config.resolve_relationship_reference("linked_from") is not None
        resolved = config.resolve_relationship_reference("linked_from")
        assert resolved is not None
        rel, is_reverse = resolved
        assert rel.name == "links"
        assert is_reverse is True

    def test_get_entity_type(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "Thing": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                )
            },
            relationships=[],
        )
        assert config.get_entity_type("Thing") is not None
        assert config.get_entity_type("Missing") is None

    def test_removed_top_level_integrations_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CoreConfig(
                name="test",
                entity_types={
                    "A": EntityTypeSchema(
                        properties={"id": PropertySchema(type="string", primary_key=True)}
                    ),
                },
                relationships=[],
                integrations={},
            )

    def test_removed_top_level_ingestion_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CoreConfig(
                name="test",
                entity_types={
                    "A": EntityTypeSchema(
                        properties={"id": PropertySchema(type="string", primary_key=True)}
                    ),
                },
                relationships=[],
                ingestion={},
            )

    def test_removed_relationship_matching_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RelationshipSchema(
                name="links",
                from_entity="A",
                to_entity="B",
                matching={},
            )

    def test_execution_sections_default_empty(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "Thing": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                )
            },
            relationships=[],
        )
        assert config.contracts == {}
        assert config.artifacts == {}
        assert config.providers == {}
        assert config.workflows == {}
        assert config.tests == []

    def test_contract_fields_must_define_type_explicitly(self):
        with pytest.raises(ValidationError, match="must define type"):
            ContractSchema(fields={"items": PropertySchema()})

    def test_builtin_contracts_are_available(self):
        assert "cruxible.EmptyInput" in BUILTIN_CONTRACTS
        assert BUILTIN_CONTRACTS["cruxible.JsonItems"].fields["items"].type == "json"

    def test_provider_accepts_inline_contracts(self):
        provider = ProviderSchema(
            kind="function",
            contract_in=ContractSchema(fields={"items": PropertySchema(type="json")}),
            contract_out="cruxible.JsonItems",
            ref="tests.support.workflow_test_providers.lift_predictor",
            version="1.0.0",
        )

        assert isinstance(provider.contract_in, ContractSchema)
        assert provider.contract_out == "cruxible.JsonItems"

    def test_execution_sections_round_trip(self):
        config = CoreConfig(
            name="test",
            entity_types={
                "Thing": EntityTypeSchema(
                    properties={"id": PropertySchema(type="string", primary_key=True)}
                )
            },
            relationships=[],
            contracts={"ThingInput": ContractSchema(fields={"id": PropertySchema(type="string")})},
            artifacts={
                "artifact": ProviderArtifactSchema(
                    kind="model", uri="file:///tmp/model", digest="abc"
                )
            },
            providers={
                "loader": ProviderSchema(
                    kind="function",
                    contract_in="ThingInput",
                    contract_out="ThingInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="ThingInput",
                    steps=[
                        WorkflowStepSchema(
                            id="load",
                            provider="loader",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            },
            tests=[WorkflowTestSchema(name="smoke", workflow="wf", input={"id": "1"})],
        )
        assert "ThingInput" in config.contracts
        assert "artifact" in config.artifacts
        assert "loader" in config.providers
        assert "wf" in config.workflows
        assert config.tests[0].name == "smoke"


# ---------------------------------------------------------------------------
# SignalPolicySchema + ProposalPolicySchema
# ---------------------------------------------------------------------------


class TestSignalPolicySchema:
    def test_defaults(self):
        cfg = SignalPolicySchema()
        assert cfg.role == "required"
        assert cfg.always_review_on_unsure is False
        assert cfg.note == ""

    def test_all_roles(self):
        for role in ("blocking", "required", "advisory"):
            cfg = SignalPolicySchema(role=role)
            assert cfg.role == role


class TestProposalPolicySchema:
    def test_defaults(self):
        cfg = ProposalPolicySchema()
        assert cfg.signals == {}
        assert cfg.auto_resolve_when == "all_support"
        assert cfg.auto_resolve_requires_prior_trust == "trusted_only"
        assert cfg.max_group_size == 1000

    def test_full(self):
        cfg = ProposalPolicySchema(
            signals={
                "bolt_check": SignalPolicySchema(role="blocking"),
                "style_v1": SignalPolicySchema(role="advisory"),
            },
            auto_resolve_when="no_contradict",
            auto_resolve_requires_prior_trust="trusted_or_watch",
            max_group_size=200,
        )
        assert len(cfg.signals) == 2
        assert cfg.signals["bolt_check"].role == "blocking"


class TestRelationshipSchemaProposalPolicy:
    def test_proposal_policy_default_none(self):
        rel = RelationshipSchema(name="r", from_entity="A", to_entity="B")
        assert rel.proposal_policy is None

    def test_proposal_policy_section(self):
        rel = RelationshipSchema(
            name="fits",
            from_entity="Part",
            to_entity="Vehicle",
            proposal_policy=ProposalPolicySchema(
                signals={"bolt": SignalPolicySchema(role="blocking")},
                max_group_size=100,
            ),
        )
        assert rel.proposal_policy is not None
        assert rel.proposal_policy.max_group_size == 100


class TestProposalPolicySchemaRoundTrip:
    """Load -> save -> load preserves relationship-local proposal policy."""

    def test_round_trip(self, tmp_path):
        yaml_str = """\
version: "1.0"
name: test_matching
entity_types:
  Shoe:
    properties:
      id:
        type: string
        primary_key: true
  Outfit:
    properties:
      id:
        type: string
        primary_key: true
relationships:
  - name: fits
    from: Shoe
    to: Outfit
    proposal_policy:
      signals:
        cosine_v1:
          role: blocking
          always_review_on_unsure: true
          note: authoritative
      auto_resolve_when: no_contradict
      max_group_size: 200
"""
        config = load_config_from_string(yaml_str)
        rel = config.get_relationship("fits")
        assert rel is not None
        assert rel.proposal_policy is not None
        assert rel.proposal_policy.signals["cosine_v1"].role == "blocking"
        assert rel.proposal_policy.auto_resolve_when == "no_contradict"
        assert rel.proposal_policy.max_group_size == 200

        # Save and reload
        path = tmp_path / "config.yaml"
        save_config(config, path)
        config2 = load_config_from_string(path.read_text())
        rel2 = config2.get_relationship("fits")
        assert rel2 is not None
        assert rel2.proposal_policy is not None
        assert rel2.proposal_policy.signals["cosine_v1"].role == "blocking"
        assert rel2.proposal_policy.signals["cosine_v1"].always_review_on_unsure is True


class TestQualityCheckValidation:
    def _config(self, *, quality_checks):
        return CoreConfig(
            name="quality_validation",
            entity_types={
                "Vendor": EntityTypeSchema(
                    properties={
                        "vendor_id": PropertySchema(type="string", primary_key=True),
                        "name": PropertySchema(type="string"),
                    }
                ),
                "Product": EntityTypeSchema(
                    properties={
                        "product_id": PropertySchema(type="string", primary_key=True),
                        "vendor_id": PropertySchema(type="string"),
                        "vendor_name": PropertySchema(type="string"),
                    }
                ),
            },
            relationships=[
                RelationshipSchema(
                    name="product_from_vendor",
                    from_entity="Product",
                    to_entity="Vendor",
                    properties={
                        "affected_versions": PropertySchema(type="json", optional=True),
                    },
                )
            ],
            quality_checks=quality_checks,
        )

    def test_duplicate_quality_check_names_rejected(self):
        config = self._config(
            quality_checks=[
                PropertyQualityCheck(
                    name="dup",
                    target="entity",
                    entity_type="Vendor",
                    property="name",
                    rule="non_empty",
                ),
                PropertyQualityCheck(
                    name="dup",
                    target="entity",
                    entity_type="Vendor",
                    property="name",
                    rule="required",
                ),
            ]
        )
        with pytest.raises(ConfigError, match="Duplicate quality check name"):
            validate_config(config)

    def test_json_content_requires_json_property(self):
        config = self._config(
            quality_checks=[
                JsonContentQualityCheck(
                    name="bad_json",
                    target="entity",
                    entity_type="Vendor",
                    property="name",
                    rule="no_empty_objects_in_array",
                )
            ]
        )
        with pytest.raises(ConfigError, match="requires property 'name' to have type 'json'"):
            validate_config(config)

    def test_cardinality_requires_compatible_direction(self):
        config = self._config(
            quality_checks=[
                CardinalityQualityCheck(
                    name="bad_cardinality",
                    entity_type="Vendor",
                    relationship_type="product_from_vendor",
                    direction="outgoing",
                    min_count=1,
                )
            ]
        )
        with pytest.raises(ConfigError, match="requires entity_type 'Product'"):
            validate_config(config)

    def test_relationship_property_consistency_requires_valid_properties(self):
        config = self._config(
            quality_checks=[
                RelationshipPropertyConsistencyQualityCheck(
                    name="bad_consistency",
                    entity_type="Product",
                    relationship_type="product_from_vendor",
                    direction="outgoing",
                    source_property="missing_vendor_id",
                    target_property="vendor_id",
                )
            ]
        )
        with pytest.raises(ConfigError, match="source_property 'missing_vendor_id'"):
            validate_config(config)

    def test_relationship_property_consistency_requires_valid_direction(self):
        config = self._config(
            quality_checks=[
                RelationshipPropertyConsistencyQualityCheck(
                    name="bad_direction",
                    entity_type="Vendor",
                    relationship_type="product_from_vendor",
                    direction="outgoing",
                    source_property="vendor_id",
                    target_property="vendor_id",
                )
            ]
        )
        with pytest.raises(ConfigError, match="requires entity_type 'Product'"):
            validate_config(config)
