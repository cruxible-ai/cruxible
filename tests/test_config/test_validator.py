"""Tests for config cross-reference validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import (
    ConstraintSchema,
    ContractSchema,
    CoreConfig,
    DecisionPolicyMatch,
    DecisionPolicySchema,
    EntityTypeSchema,
    FeedbackProfileSchema,
    FeedbackReasonCodeSchema,
    NamedQuerySchema,
    OutcomeCodeSchema,
    OutcomeProfileSchema,
    PropertySchema,
    ProposalPolicySchema,
    ProviderArtifactSchema,
    ProviderSchema,
    RelationshipSchema,
    SignalPolicySchema,
    TraversalStep,
    WorkflowSchema,
    WorkflowStepSchema,
    WorkflowTestSchema,
)
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError


def _minimal_config(**overrides) -> CoreConfig:
    """Create a minimal valid config with optional overrides."""
    defaults = dict(
        name="test",
        entity_types={
            "A": EntityTypeSchema(
                properties={
                    "id": PropertySchema(type="string", primary_key=True),
                    "status": PropertySchema(type="string", optional=True),
                }
            ),
            "B": EntityTypeSchema(
                properties={
                    "id": PropertySchema(type="string", primary_key=True),
                    "status": PropertySchema(type="string", optional=True),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="links",
                from_entity="A",
                to_entity="B",
                properties={"score": PropertySchema(type="float", optional=True)},
            ),
        ],
    )
    defaults.update(overrides)
    return CoreConfig(**defaults)


class TestValidateRelationships:
    def test_valid_relationships(self):
        config = _minimal_config()
        warnings = validate_config(config)
        assert not warnings or all("primary_key" not in w for w in warnings)

    def test_invalid_from_entity(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(name="bad", from_entity="Missing", to_entity="B"),
            ]
        )
        with pytest.raises(ConfigError, match="cross-reference"):
            validate_config(config)

    def test_invalid_to_entity(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(name="bad", from_entity="A", to_entity="Missing"),
            ]
        )
        with pytest.raises(ConfigError, match="cross-reference"):
            validate_config(config)

    def test_duplicate_relationship_names(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(name="links", from_entity="A", to_entity="B"),
                RelationshipSchema(name="links", from_entity="B", to_entity="A"),
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("Duplicate" in e for e in exc_info.value.errors)

    def test_duplicate_relationship_reverse_names(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(
                    name="links",
                    from_entity="A",
                    to_entity="B",
                    reverse_name="linked_from",
                ),
                RelationshipSchema(
                    name="connects",
                    from_entity="B",
                    to_entity="A",
                    reverse_name="linked_from",
                ),
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("reverse_name" in e for e in exc_info.value.errors)

    def test_reverse_name_cannot_collide_with_canonical_name(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(
                    name="links",
                    from_entity="A",
                    to_entity="B",
                    reverse_name="connects",
                ),
                RelationshipSchema(name="connects", from_entity="B", to_entity="A"),
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("collides" in e for e in exc_info.value.errors)


class TestValidateNamedQueries:
    def test_valid_query(self):
        config = _minimal_config(
            named_queries={
                "find_b": NamedQuerySchema(
                    entry_point="A",
                    traversal=[TraversalStep(relationship="links")],
                    returns="list[B]",
                )
            }
        )
        validate_config(config)

    def test_valid_query_with_reverse_name(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(
                    name="links",
                    from_entity="A",
                    to_entity="B",
                    reverse_name="linked_from",
                ),
            ],
            named_queries={
                "find_a": NamedQuerySchema(
                    entry_point="B",
                    traversal=[TraversalStep(relationship="linked_from")],
                    returns="list[A]",
                )
            },
        )
        validate_config(config)

    def test_invalid_entry_point(self):
        config = _minimal_config(
            named_queries={
                "bad": NamedQuerySchema(
                    entry_point="Missing",
                    traversal=[TraversalStep(relationship="links")],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("entry_point" in e for e in exc_info.value.errors)

    def test_invalid_traversal_relationship(self):
        config = _minimal_config(
            named_queries={
                "bad": NamedQuerySchema(
                    entry_point="A",
                    traversal=[TraversalStep(relationship="nonexistent")],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("nonexistent" in e for e in exc_info.value.errors)

    def test_invalid_traversal_filter_property(self):
        config = _minimal_config(
            named_queries={
                "bad": NamedQuerySchema(
                    entry_point="A",
                    traversal=[TraversalStep(relationship="links", filter={"scroe": 1})],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("filter" in e and "scroe" in e for e in exc_info.value.errors)

    def test_invalid_traversal_target_filter_property(self):
        config = _minimal_config(
            named_queries={
                "bad": NamedQuerySchema(
                    entry_point="A",
                    traversal=[
                        TraversalStep(relationship="links", target_filter={"statuz": "open"})
                    ],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("target_filter" in e and "statuz" in e for e in exc_info.value.errors)

    def test_valid_traversal_filters(self):
        config = _minimal_config(
            named_queries={
                "find_open_b": NamedQuerySchema(
                    entry_point="A",
                    traversal=[
                        TraversalStep(
                            relationship="links",
                            filter={"score": 0.9},
                            target_filter={"status": "open"},
                        )
                    ],
                    returns="list[B]",
                )
            }
        )
        validate_config(config)

    def test_rejects_source_side_traversal_constraint(self):
        config = _minimal_config(
            named_queries={
                "bad": NamedQuerySchema(
                    entry_point="A",
                    traversal=[
                        TraversalStep(
                            relationship="links",
                            constraint="source.status == open",
                        )
                    ],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "source-side traversal constraints are not supported" in e
            for e in exc_info.value.errors
        )


class TestValidateLoopOneControls:
    def test_supported_constraint_invalid_reference_errors(self):
        config = _minimal_config(
            constraints=[
                ConstraintSchema(
                    name="bad_constraint",
                    rule="links.FROM.missing == links.TO.id",
                )
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("bad_constraint" in e and "missing" in e for e in exc_info.value.errors)

    def test_feedback_profile_rejects_unknown_scope_property(self):
        config = _minimal_config(
            feedback_profiles={
                "links": FeedbackProfileSchema(
                    reason_codes={
                        "mismatch": FeedbackReasonCodeSchema(
                            description="Mismatch",
                            remediation_hint="constraint",
                        )
                    },
                    scope_keys={"bad_key": "FROM.missing"},
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("bad_key" in e and "missing" in e for e in exc_info.value.errors)

    def test_decision_policies_require_unique_names(self):
        config = _minimal_config(
            named_queries={
                "find_b": NamedQuerySchema(
                    entry_point="A",
                    traversal=[TraversalStep(relationship="links")],
                    returns="list[B]",
                )
            },
            decision_policies=[
                DecisionPolicySchema(
                    name="dup_policy",
                    applies_to="query",
                    query_name="find_b",
                    relationship_type="links",
                    effect="suppress",
                    match=DecisionPolicyMatch(),
                ),
                DecisionPolicySchema(
                    name="dup_policy",
                    applies_to="query",
                    query_name="find_b",
                    relationship_type="links",
                    effect="suppress",
                    match=DecisionPolicyMatch(),
                ),
            ],
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("Duplicate decision policy name" in e for e in exc_info.value.errors)

    def test_workflow_policy_requires_proposal_type(self):
        config = _minimal_config(
            contracts={
                "WorkflowInput": ContractSchema(fields={"id": PropertySchema(type="string")}),
            },
            artifacts={
                "artifact": ProviderArtifactSchema(
                    kind="model", uri="file:///tmp/model", sha256="abc"
                )
            },
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="WorkflowInput",
                    contract_out="WorkflowInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                    artifact="artifact",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            },
            decision_policies=[
                DecisionPolicySchema(
                    name="bad_workflow_policy",
                    applies_to="workflow",
                    workflow_name="wf",
                    relationship_type="links",
                    effect="suppress",
                    match=DecisionPolicyMatch(),
                )
            ],
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("must be type: proposal" in e for e in exc_info.value.errors)

    def test_receipt_outcome_profile_requires_known_query_surface(self):
        config = _minimal_config(
            outcome_profiles={
                "query_quality": OutcomeProfileSchema(
                    anchor_type="receipt",
                    surface_type="query",
                    surface_name="missing_query",
                    outcome_codes={
                        "bad_result": OutcomeCodeSchema(
                            description="Bad result",
                            remediation_hint="provider_fix",
                        )
                    },
                    scope_keys={"surface": "SURFACE.name"},
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("missing_query" in e for e in exc_info.value.errors)

    def test_resolution_outcome_profile_rejects_unsupported_path(self):
        with pytest.raises(ValidationError):
            OutcomeProfileSchema(
                anchor_type="resolution",
                relationship_type="links",
                outcome_codes={
                    "bad_link": OutcomeCodeSchema(
                        description="Bad approved link",
                        remediation_hint="trust_adjustment",
                    )
                },
                scope_keys={"bad": "RECEIPT.operation_type"},
            )


class TestValidateMultiRelationshipStep:
    def test_multi_relationship_all_valid(self):
        config = _minimal_config(
            named_queries={
                "q": NamedQuerySchema(
                    entry_point="A",
                    traversal=[TraversalStep(relationship=["links"], direction="outgoing")],
                    returns="list[B]",
                )
            }
        )
        validate_config(config)  # should not raise

    def test_multi_relationship_invalid_name(self):
        config = _minimal_config(
            named_queries={
                "q": NamedQuerySchema(
                    entry_point="A",
                    traversal=[
                        TraversalStep(relationship=["links", "bogus"], direction="outgoing")
                    ],
                    returns="list[B]",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("bogus" in e for e in exc_info.value.errors)

    def test_empty_list_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            TraversalStep(relationship=[], direction="outgoing")


class TestValidatePrimaryKeys:
    def test_errors_on_missing_primary_key(self):
        config = _minimal_config(
            entity_types={
                "NoPK": EntityTypeSchema(properties={"name": PropertySchema(type="string")}),
            },
            relationships=[],
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("primary_key" in e for e in exc_info.value.errors)


class TestValidateWorkflowExecution:
    def _workflow_config(self, **overrides) -> CoreConfig:
        defaults = dict(
            contracts={
                "WorkflowInput": ContractSchema(fields={"id": PropertySchema(type="string")}),
            },
            artifacts={
                "artifact": ProviderArtifactSchema(
                    kind="model", uri="file:///tmp/model", sha256="abc"
                )
            },
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="WorkflowInput",
                    contract_out="WorkflowInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                    artifact="artifact",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            },
            tests=[WorkflowTestSchema(name="smoke", workflow="wf", input={"id": "1"})],
        )
        defaults.update(overrides)
        return _minimal_config(**defaults)

    def test_builtin_contract_refs_validate(self):
        config = self._workflow_config(
            contracts={},
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="cruxible.JsonObject",
                    contract_out="cruxible.JsonItems",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="cruxible.EmptyInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            },
            tests=[],
        )

        validate_config(config)

    def test_missing_provider_contract(self):
        config = self._workflow_config(contracts={})
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("contract_in" in error for error in exc_info.value.errors)

    def test_missing_provider_artifact(self):
        config = self._workflow_config(
            artifacts={},
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="WorkflowInput",
                    contract_out="WorkflowInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                    artifact="artifact",
                )
            },
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("artifact 'artifact'" in error for error in exc_info.value.errors)

    def test_canonical_workflow_allows_transform_provider_without_artifact(self):
        config = self._workflow_config(
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="WorkflowInput",
                    contract_out="WorkflowInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    type="canonical",
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        ),
                        WorkflowStepSchema(
                            id="apply_loaded",
                            apply_entities={"entities_from": "loaded"},
                            **{"as": "applied"},
                        ),
                    ],
                    returns="applied",
                )
            },
        )

        validate_config(config)

    def test_invalid_workflow_returns_alias(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="missing",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("returns alias" in error for error in exc_info.value.errors)

    def test_invalid_workflow_reference_future_alias(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$steps.missing.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("unknown or future step alias" in error for error in exc_info.value.errors)

    def test_missing_test_workflow(self):
        config = self._workflow_config(tests=[WorkflowTestSchema(name="smoke", workflow="nope")])
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("workflow 'nope'" in error for error in exc_info.value.errors)

    def test_make_candidates_rejects_unknown_relationship(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="candidates",
                            make_candidates={
                                "relationship_type": "missing",
                                "items": [],
                                "from_type": "A",
                                "from_id": "$input.id",
                                "to_type": "B",
                                "to_id": "$input.id",
                            },
                            **{"as": "candidates"},
                        )
                    ],
                    returns="candidates",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "make_candidates relationship_type 'missing'" in error
            for error in exc_info.value.errors
        )

    def test_list_entities_rejects_unknown_entity_type(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="entities",
                            list_entities={
                                "entity_type": "Missing",
                                "property_filter": {"name": "$input.id"},
                            },
                            **{"as": "entities"},
                        )
                    ],
                    returns="entities",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "list_entities entity_type 'Missing'" in error for error in exc_info.value.errors
        )

    def test_list_relationships_rejects_unknown_relationship(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="edges",
                            list_relationships={
                                "relationship_type": "missing",
                                "property_filter": {"status": "$input.id"},
                            },
                            **{"as": "edges"},
                        )
                    ],
                    returns="edges",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "list_relationships relationship_type 'missing'" in error
            for error in exc_info.value.errors
        )

    def test_proposal_relationship_group_rejects_undeclared_signal_source(self):
        config = self._workflow_config(
            relationships=[
                RelationshipSchema(
                    name="links",
                    from_entity="A",
                    to_entity="B",
                    proposal_policy=ProposalPolicySchema(
                        signals={"catalog": SignalPolicySchema(role="required")}
                    ),
                ),
            ],
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="signals",
                            map_signals={
                                "signal_source": "missing",
                                "items": [],
                                "from_id": "$input.id",
                                "to_id": "$input.id",
                                "enum": {
                                    "path": "verdict",
                                    "map": {"support": "support"},
                                },
                            },
                            **{"as": "signals"},
                        ),
                        WorkflowStepSchema(
                            id="proposal",
                            propose_relationship_group={
                                "relationship_type": "links",
                                "candidates_from": "signals",
                                "signals_from": ["signals"],
                            },
                            **{"as": "proposal"},
                        ),
                    ],
                    returns="proposal",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "map_signals signal_source 'missing'" in error
            and "proposal_policy.signals" in error
            for error in exc_info.value.errors
        )

    def test_map_signals_open_mode_allows_labels_without_relationship_policy(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="signals",
                            map_signals={
                                "signal_source": "agent_check",
                                "items": [],
                                "from_id": "$input.id",
                                "to_id": "$input.id",
                                "enum": {
                                    "path": "verdict",
                                    "map": {"support": "support"},
                                },
                            },
                            **{"as": "signals"},
                        )
                    ],
                    returns="signals",
                )
            },
        )

        validate_config(config)

    def test_propose_relationship_group_rejects_unknown_aliases(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="proposal",
                            propose_relationship_group={
                                "relationship_type": "links",
                                "candidates_from": "missing_candidates",
                                "signals_from": ["missing_signals"],
                            },
                            **{"as": "proposal"},
                        )
                    ],
                    returns="proposal",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("candidates_from alias 'missing_candidates'" in e for e in exc_info.value.errors)
        assert any("signals_from alias 'missing_signals'" in e for e in exc_info.value.errors)

    def test_canonical_workflow_requires_apply_step(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    type="canonical",
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "canonical workflows require at least one apply_* step" in error
            for error in exc_info.value.errors
        )

    def test_decision_support_workflow_rejects_apply_steps(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    type="decision_support",
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        ),
                        WorkflowStepSchema(
                            id="apply_loaded",
                            apply_entities={"entities_from": "loaded"},
                            **{"as": "applied"},
                        ),
                    ],
                    returns="applied",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "decision_support workflows must not use apply_* steps" in error
            for error in exc_info.value.errors
        )

    def test_proposal_type_requires_relationship_proposal_return(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    type="proposal",
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "proposal workflows must return a proposal-bearing alias" in error
            for error in exc_info.value.errors
        )

    def test_item_reference_outside_builtin_steps_is_rejected(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$item.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("unsupported reference '$item.id'" in e for e in exc_info.value.errors)

    def test_dataflow_step_item_source_references_are_rejected(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="shaped",
                            shape_items={
                                "items": "$item.rows",
                                "fields": {"id": "$item.id"},
                            },
                            **{"as": "shaped"},
                        )
                    ],
                    returns="shaped",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any("unsupported reference '$item.rows'" in e for e in exc_info.value.errors)

    def test_filter_items_where_refs_must_use_input(self):
        config = self._workflow_config(
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="loaded",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        ),
                        WorkflowStepSchema(
                            id="filtered",
                            filter_items={
                                "items": "$steps.loaded.items",
                                "where": {
                                    "status": "$item.status",
                                    "owner": "$steps.loaded.owner",
                                },
                            },
                            **{"as": "filtered"},
                        ),
                    ],
                    returns="filtered",
                )
            }
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "filter_items where reference '$item.status' must use $input only" in e
            for e in exc_info.value.errors
        )
        assert any(
            "filter_items where reference '$steps.loaded.owner' must use $input only" in e
            for e in exc_info.value.errors
        )

    def test_dataflow_steps_accept_scoped_item_refs(self):
        config = self._workflow_config(
            contracts={
                "WorkflowInput": ContractSchema(
                    fields={
                        "id": PropertySchema(type="string"),
                        "status": PropertySchema(type="string", optional=True),
                    }
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="loaded",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        ),
                        WorkflowStepSchema(
                            id="shaped",
                            shape_items={
                                "items": "$steps.loaded.items",
                                "fields": {"id": "$item.id", "status": "$item.status"},
                            },
                            **{"as": "shaped"},
                        ),
                        WorkflowStepSchema(
                            id="filtered",
                            filter_items={
                                "items": "$steps.shaped.items",
                                "where": {"status": "$input.status"},
                                "comparisons": [
                                    {"left": "$item.id", "op": "ne", "right": "$input.id"}
                                ],
                            },
                            **{"as": "filtered"},
                        ),
                        WorkflowStepSchema(
                            id="joined",
                            join_items={
                                "left_items": "$steps.filtered.items",
                                "right_items": "$steps.shaped.items",
                                "left_key": "$item.id",
                                "right_key": "$item.id",
                                "fields": {"id": "$item.left.id"},
                            },
                            **{"as": "joined"},
                        ),
                        WorkflowStepSchema(
                            id="deduped",
                            dedupe_items={
                                "items": "$steps.joined.items",
                                "keys": ["$item.id"],
                            },
                            **{"as": "deduped"},
                        ),
                    ],
                    returns="deduped",
                )
            },
        )

        validate_config(config)


class TestValidateKinds:
    def test_ontology_rejects_world_model_execution_blocks(self):
        config = _minimal_config(
            kind="ontology",
            contracts={
                "WorkflowInput": ContractSchema(fields={"id": PropertySchema(type="string")}),
            },
            providers={
                "provider": ProviderSchema(
                    kind="function",
                    contract_in="WorkflowInput",
                    contract_out="WorkflowInput",
                    ref="tests.support.workflow_test_providers.lift_predictor",
                    version="1.0.0",
                )
            },
            workflows={
                "wf": WorkflowSchema(
                    contract_in="WorkflowInput",
                    steps=[
                        WorkflowStepSchema(
                            id="provider_step",
                            provider="provider",
                            input={"id": "$input.id"},
                            **{"as": "loaded"},
                        )
                    ],
                    returns="loaded",
                )
            },
        )

        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert any(
            "kind 'ontology' may not define workflows" in error for error in exc_info.value.errors
        )


class TestConfigErrorStr:
    def test_str_includes_individual_errors(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(name="bad", from_entity="Ghost", to_entity="B"),
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        text = str(exc_info.value)
        assert "Ghost" in text

    def test_str_includes_all_errors(self):
        config = _minimal_config(
            relationships=[
                RelationshipSchema(name="bad", from_entity="X", to_entity="Y"),
            ]
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        text = str(exc_info.value)
        assert "X" in text
        assert "Y" in text


class TestCarPartsConfig:
    def test_car_parts_validates(self, configs_dir: Path):
        config = load_config(configs_dir / "car_parts.yaml")
        validate_config(config)  # should not raise
