"""Tests for config schema Pydantic models."""

from textwrap import dedent, indent

import pytest
from pydantic import ValidationError

from cruxible_core.config.loader import load_config_from_string, save_config
from cruxible_core.config.schema import (
    BUILTIN_CONTRACTS,
    AssertSpec,
    BoundsQualityCheck,
    CardinalityQualityCheck,
    ConstraintSchema,
    ContractSchema,
    CoreConfig,
    EntityTypeSchema,
    EnumSchema,
    JsonContentQualityCheck,
    NamedQuerySchema,
    PropertyQualityCheck,
    PropertySchema,
    ProposalPolicySchema,
    ProviderArtifactSchema,
    ProviderSchema,
    RelatedExclusionSpec,
    RelationshipSchema,
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


class TestEnumSchema:
    def test_valid_shared_enum(self):
        enum = EnumSchema(values=["active", "retired"], description="Lifecycle")
        assert enum.values == ["active", "retired"]

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


class TestNamedQuerySchema:
    def test_minimal(self):
        query = NamedQuerySchema(
            entry_point="Vehicle",
            traversal=[TraversalStep(relationship="fits", direction="incoming")],
            returns="list[Part]",
        )
        assert query.entry_point == "Vehicle"
        assert len(query.traversal) == 1

    def test_multi_step(self):
        query = NamedQuerySchema(
            entry_point="Part",
            traversal=[
                TraversalStep(relationship="replaces", direction="outgoing"),
                TraversalStep(relationship="fits", direction="outgoing"),
            ],
            returns="list[Part]",
        )
        assert len(query.traversal) == 2


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
        "legacy_field",
        [{"purpose": "proposal"}, {"canonical": True}],
    )
    def test_workflow_rejects_legacy_type_fields(self, legacy_field: dict[str, object]):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WorkflowSchema(
                **legacy_field,
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
            },
            **{"as": "candidates"},
        )
        assert step.make_candidates is not None
        assert step.make_candidates.relationship_type == "recommended_for"

    def test_list_entities_step_accepts_property_filter_refs(self):
        step = WorkflowStepSchema(
            id="products",
            list_entities={
                "entity_type": "Product",
                "property_filter": {"category": "$input.category"},
                "limit": 5,
            },
            **{"as": "products"},
        )
        assert step.list_entities is not None
        assert step.list_entities.entity_type == "Product"

    def test_list_relationships_step_accepts_property_filter_refs(self):
        step = WorkflowStepSchema(
            id="links",
            list_relationships={
                "relationship_type": "recommended_for",
                "property_filter": {"review_status": "$input.status"},
            },
            **{"as": "links"},
        )
        assert step.list_relationships is not None
        assert step.list_relationships.relationship_type == "recommended_for"

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
        assert config.kind == "world_model"
        assert config.named_queries == {}
        assert config.constraints == []

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
                    kind="model", uri="file:///tmp/model", sha256="abc"
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
