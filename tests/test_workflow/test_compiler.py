"""Tests for workflow lock and compilation."""

from __future__ import annotations

import pytest
from tests.support.workflow_helpers import write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import PropertySchema, WorkflowStepSchema
from cruxible_core.errors import ConfigError
from cruxible_core.workflow import (
    build_lock,
    compile_workflow,
    execute_workflow,
    get_legacy_lock_path,
    write_lock,
)


class TestWorkflowCompiler:
    def test_compile_workflow_success(self, workflow_instance: CruxibleInstance) -> None:
        write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        plan = compile_workflow(
            config,
            build_lock(config),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.workflow == "evaluate_promo"
        assert plan.contract_in == "PromoInput"
        assert plan.contract_out is None
        assert plan.steps[0].kind == "query"
        assert plan.steps[0].params_preview["sku"] == "SKU-123"
        assert plan.steps[1].provider_version == "1.2.0"
        assert plan.steps[1].artifact_sha256 == "abc123"

    def test_compile_workflow_carries_contract_out(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].contract_out = "MarginResult"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.contract_out == "MarginResult"

    def test_compile_workflow_rejects_unknown_contract_out(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].contract_out = "MissingOutput"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        with pytest.raises(ConfigError, match="unknown contract_out 'MissingOutput'"):
            compile_workflow(
                workflow_instance.load_config(),
                build_lock(workflow_instance.load_config()),
                "evaluate_promo",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

    def test_compile_workflow_carries_query_step_options(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].steps[0].relationship_state = "accepted"
        config.workflows["evaluate_promo"].steps[0].include_source = True
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.steps[0].relationship_state_template == "accepted"
        assert plan.steps[0].include_source is True

    def test_compile_workflow_carries_guard_steps(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].steps.insert(
            1,
            WorkflowStepSchema(
                id="complete_context",
                assert_not_truncated={"step": "context"},
            ),
        )
        config.workflows["evaluate_promo"].steps.insert(
            2,
            WorkflowStepSchema(
                id="has_context",
                assert_count={
                    "step": "context",
                    "count": "returned_results",
                    "op": "gt",
                    "value": 0,
                },
            ),
        )
        config.workflows["evaluate_promo"].steps.insert(
            3,
            WorkflowStepSchema(
                id="has_sku",
                assert_exists={"ref": "$steps.context.results[0].entity_id"},
            ),
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert [step.kind for step in plan.steps[1:4]] == [
            "assert_not_truncated",
            "assert_count",
            "assert_exists",
        ]
        assert plan.steps[1].assert_not_truncated_spec is not None
        assert plan.steps[2].assert_count_spec is not None
        assert plan.steps[3].assert_exists_spec is not None

    def test_compile_workflow_includes_inline_collection_query_step(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.contracts["PromoInput"].fields["category"] = PropertySchema(
            type="string",
            optional=True,
        )
        config.workflows["evaluate_promo"].steps.insert(
            1,
            WorkflowStepSchema(
                id="products",
                query={
                    "mode": "collection",
                    "result_shape": "entity",
                    "returns": "Product",
                    "where": {"result.properties.category": {"eq": "$input.category"}},
                    "limit": 5,
                },
                **{"as": "products"},
            ),
        )
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "category": "soda",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.steps[1].kind == "query"
        assert plan.steps[1].inline_query is not None
        assert plan.steps[1].inline_query.returns == "Product"

    def test_compile_workflow_carries_aggregate_items_step(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        config.workflows["evaluate_promo"].steps.insert(
            1,
            WorkflowStepSchema(
                id="summary",
                aggregate_items={
                    "items": "$steps.context.results",
                    "measures": {"row_count": {"count": True}},
                },
                **{"as": "summary"},
            ),
        )
        config.workflows["evaluate_promo"].returns = "summary"
        workflow_instance.save_config(config)
        write_lock_for_instance(workflow_instance)

        plan = compile_workflow(
            workflow_instance.load_config(),
            build_lock(workflow_instance.load_config()),
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert plan.steps[1].kind == "aggregate_items"
        assert plan.steps[1].aggregate_items_spec is not None
        assert plan.steps[1].aggregate_items_spec.measures["row_count"].operation == "count"

    def test_compile_workflow_rejects_bad_input_contract(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        with pytest.raises(ConfigError, match="missing required field 'end_date'"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {"sku": "SKU-123", "start_date": "2026-03-01"},
            )

    def test_compile_workflow_empty_input_error_mentions_cli_flags(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(workflow_instance)
        config = workflow_instance.load_config()

        with pytest.raises(ConfigError, match="empty input payload provided"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {},
            )

        with pytest.raises(ConfigError, match="Use --input or --input-file"):
            compile_workflow(
                config,
                build_lock(config),
                "evaluate_promo",
                {},
            )

    def test_compile_workflow_rejects_lock_digest_mismatch(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        lock = build_lock(config)
        lock.config_digest = "sha256:bad"

        with pytest.raises(ConfigError, match="Lock file config digest does not match"):
            compile_workflow(
                config,
                lock,
                "evaluate_promo",
                {
                    "sku": "SKU-123",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-07",
                },
            )

    def test_compile_workflow_includes_built_in_proposal_steps(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(proposal_workflow_instance)
        config = proposal_workflow_instance.load_config()

        plan = compile_workflow(
            config,
            build_lock(config),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert [step.kind for step in plan.steps] == [
            "query",
            "provider",
            "make_candidates",
            "map_signals",
            "propose_relationship_group",
        ]
        assert plan.steps[2].make_candidates_spec is not None
        assert plan.steps[2].make_candidates_spec.relationship_type == "recommended_for"
        assert plan.steps[3].map_signals_spec is not None
        assert plan.steps[3].map_signals_spec.signal_source == "catalog"
        assert plan.steps[4].propose_relationship_group_spec is not None
        assert plan.steps[4].propose_relationship_group_spec.signals_from == ["catalog_signals"]

    def test_compile_workflow_preserves_pending_refresh_mode(
        self, proposal_workflow_instance: CruxibleInstance
    ) -> None:
        config = proposal_workflow_instance.load_config()
        for step in config.workflows["propose_campaign_recommendations"].steps:
            if step.propose_relationship_group is not None:
                step.propose_relationship_group.pending_refresh_mode = "retain_missing"
        proposal_workflow_instance.save_config(config)
        write_lock_for_instance(proposal_workflow_instance)

        plan = compile_workflow(
            proposal_workflow_instance.load_config(),
            build_lock(proposal_workflow_instance.load_config()),
            "propose_campaign_recommendations",
            {"campaign_id": "CMP-1"},
        )

        assert plan.steps[4].propose_relationship_group_spec is not None
        assert (
            plan.steps[4].propose_relationship_group_spec.pending_refresh_mode == "retain_missing"
        )

    def test_compile_canonical_workflow_carries_canonical_metadata(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        write_lock_for_instance(canonical_workflow_instance)
        config = canonical_workflow_instance.load_config()
        lock = build_lock(config, canonical_workflow_instance.get_config_path().parent)

        plan = compile_workflow(
            config,
            lock,
            "build_reference",
            {},
            config_base_path=canonical_workflow_instance.get_config_path().parent,
        )

        assert plan.canonical is True
        assert plan.lock_digest == lock.lock_digest
        assert plan.steps[0].provider_entrypoint_sha256 is not None
        assert "apply_entities" in [step.kind for step in plan.steps]

    def test_compile_canonical_workflow_allows_transform_provider_without_artifact(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        provider_name = config.workflows["build_reference"].steps[0].provider
        assert provider_name is not None
        config.providers[provider_name].artifact = None
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)
        config = canonical_workflow_instance.load_config()
        lock = build_lock(config, canonical_workflow_instance.get_config_path().parent)

        plan = compile_workflow(
            config,
            lock,
            "build_reference",
            {},
            config_base_path=canonical_workflow_instance.get_config_path().parent,
        )

        assert plan.steps[0].provider_name == provider_name
        assert plan.steps[0].artifact_name is None
        assert plan.steps[0].artifact_sha256 is None

    def test_compile_workflow_carries_apply_all_step(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.workflows["build_reference"].steps.append(
            WorkflowStepSchema(
                id="apply_everything",
                apply_all={
                    "entities_from": ["vendors", "products"],
                    "relationships_from": ["product_vendor"],
                },
                **{"as": "apply_everything"},
            )
        )
        config.workflows["build_reference"].returns = "apply_everything"
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        plan = compile_workflow(
            canonical_workflow_instance.load_config(),
            build_lock(canonical_workflow_instance.load_config()),
            "build_reference",
            {},
            config_base_path=canonical_workflow_instance.get_config_path().parent,
        )

        assert plan.steps[-1].kind == "apply_all"
        assert plan.steps[-1].apply_all_spec is not None
        assert plan.steps[-1].apply_all_spec.entities_from == ["vendors", "products"]

    def test_compile_rejects_apply_steps_in_non_canonical_workflow(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.workflows["build_reference"].type = "utility"
        canonical_workflow_instance.save_config(config)
        write_lock_for_instance(canonical_workflow_instance)

        with pytest.raises(ConfigError, match="must be type: canonical to use apply_entities"):
            compile_workflow(
                canonical_workflow_instance.load_config(),
                build_lock(canonical_workflow_instance.load_config()),
                "build_reference",
                {},
                config_base_path=canonical_workflow_instance.get_config_path().parent,
            )

    def test_build_lock_rejects_stale_canonical_artifact_hash(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].sha256 = "sha256:bad"
        canonical_workflow_instance.save_config(config)

        with pytest.raises(ConfigError) as exc_info:
            build_lock(
                canonical_workflow_instance.load_config(),
                canonical_workflow_instance.get_config_path().parent,
            )
        message = str(exc_info.value)
        assert "Artifact 'canonical_bundle' sha256 mismatch." in message
        assert "expected (config): sha256:bad" in message
        assert "actual (on disk):" in message
        assert "cruxible lock --force" in message

    def test_build_lock_force_accepts_live_canonical_artifact_hash(
        self, canonical_workflow_instance: CruxibleInstance
    ) -> None:
        config = canonical_workflow_instance.load_config()
        config.artifacts["canonical_bundle"].sha256 = "sha256:bad"
        canonical_workflow_instance.save_config(config)

        lock = build_lock(
            canonical_workflow_instance.load_config(),
            canonical_workflow_instance.get_config_path().parent,
            force=True,
        )

        assert lock.artifacts["canonical_bundle"].sha256 != "sha256:bad"
        assert lock.artifacts["canonical_bundle"].sha256.startswith("sha256:")

    def test_executor_uses_legacy_lock_path_as_fallback(
        self, workflow_instance: CruxibleInstance
    ) -> None:
        config = workflow_instance.load_config()
        legacy_path = get_legacy_lock_path(workflow_instance)
        write_lock(build_lock(config, workflow_instance.get_config_path().parent), legacy_path)

        result = execute_workflow(
            workflow_instance,
            config,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

        assert result.output["decision"] == "approve"
