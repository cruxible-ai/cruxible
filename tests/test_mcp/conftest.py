"""Shared fixtures for MCP tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.mcp import handlers as mcp_handlers
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import local_api
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.registry import reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture(autouse=True)
def clear_instances():
    """Clear the instance manager between tests."""
    get_manager().clear()
    reset_client_cache()
    reset_registry()
    yield
    get_manager().clear()
    reset_client_cache()
    reset_registry()


@pytest.fixture(autouse=True)
def reset_permission_mode(monkeypatch, tmp_path: Path):
    """Reset permission mode cache between tests."""
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_ALLOWED_ROOTS", raising=False)
    monkeypatch.delenv("CRUXIBLE_AGENT_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_REQUIRE_SERVER", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / ".server-state"))
    reset_permissions()
    yield
    reset_permissions()


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with a config file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CAR_PARTS_YAML)
    return tmp_path


@pytest.fixture
def vehicles_csv(tmp_project: Path) -> Path:
    """Create a vehicles CSV file."""
    csv_path = tmp_project / "vehicles.csv"
    csv_path.write_text(
        "vehicle_id,year,make,model\n"
        "V-2024-CIVIC-EX,2024,Honda,Civic\n"
        "V-2024-ACCORD-SPORT,2024,Honda,Accord\n"
    )
    return csv_path


@pytest.fixture
def parts_csv(tmp_project: Path) -> Path:
    """Create a parts CSV file."""
    csv_path = tmp_project / "parts.csv"
    csv_path.write_text(
        "part_number,name,category,price\n"
        "BP-1001,Ceramic Brake Pads,brakes,49.99\n"
        "BP-1002,Performance Brake Pads,brakes,89.99\n"
    )
    return csv_path


@pytest.fixture
def fitments_csv(tmp_project: Path) -> Path:
    """Create a fitments CSV file."""
    csv_path = tmp_project / "fitments.csv"
    csv_path.write_text(
        "part_number,vehicle_id,verified,source\n"
        "BP-1001,V-2024-CIVIC-EX,true,catalog\n"
        "BP-1001,V-2024-ACCORD-SPORT,true,catalog\n"
        "BP-1002,V-2024-CIVIC-EX,true,user_report\n"
    )
    return csv_path


class GovernedLocalClient:
    """In-process client adapter for governed MCP write tests."""

    def init(
        self,
        *,
        root_dir: str,
        config_path: str | None = None,
        config_yaml: str | None = None,
        data_dir: str | None = None,
        kit: str | None = None,
    ):
        return local_api._handle_init_governed(
            root_dir=root_dir,
            config_path=config_path,
            config_yaml=config_yaml,
            data_dir=data_dir,
            kit=kit,
        )

    def validate(self, config_path: str | None = None, config_yaml: str | None = None):
        return local_api._handle_validate_local(config_path=config_path, config_yaml=config_yaml)

    def server_info(self):
        return local_api._handle_server_info_local()

    def workflow_lock(self, instance_id: str, *, force: bool = False):
        return local_api._handle_workflow_lock_local(instance_id, force=force)

    def workflow_plan(self, instance_id: str, *, workflow_name: str, input_payload=None):
        return local_api._handle_workflow_plan_local(instance_id, workflow_name, input_payload)

    def workflow_run(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload=None,
        decision_record_id: str | None = None,
    ):
        return local_api._handle_workflow_run_local(
            instance_id,
            workflow_name,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        )

    def workflow_apply(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        expected_apply_digest: str,
        expected_head_snapshot_id: str | None = None,
        input_payload=None,
        decision_record_id: str | None = None,
    ):
        return local_api._handle_workflow_apply_local(
            instance_id,
            workflow_name,
            expected_apply_digest,
            expected_head_snapshot_id=expected_head_snapshot_id,
            input_payload=input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        )

    def propose_workflow(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload=None,
        decision_record_id: str | None = None,
    ):
        return local_api._handle_propose_workflow_local(
            instance_id,
            workflow_name,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        )

    def ingest(
        self,
        instance_id: str,
        mapping_name: str,
        *,
        file_path: str | None = None,
        data_csv: str | None = None,
        data_json=None,
        data_ndjson: str | None = None,
        upload_id: str | None = None,
    ):
        return local_api._handle_ingest_local(
            instance_id,
            mapping_name,
            file_path=file_path,
            data_csv=data_csv,
            data_json=data_json,
            data_ndjson=data_ndjson,
            upload_id=upload_id,
        )

    def query(
        self,
        instance_id: str,
        query_name: str,
        params: dict,
        limit: int | None = None,
        decision_record_id: str | None = None,
    ):
        return local_api._handle_query_local(
            instance_id,
            query_name,
            params,
            limit=limit,
            decision_record_id=decision_record_id,
            surface="mcp",
        )

    def create_decision_record(
        self,
        instance_id: str,
        *,
        question: str,
        subject_type: str | None = None,
        subject_id: str | None = None,
        opened_by: str = "human",
    ):
        return local_api._handle_create_decision_record_local(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
        )

    def get_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        include_events: bool = True,
    ):
        return local_api._handle_get_decision_record_local(
            instance_id,
            decision_record_id,
            include_events=include_events,
        )

    def list_decision_records(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
        limit: int = 100,
    ):
        return local_api._handle_list_decision_records_local(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
        )

    def list_decision_events(
        self,
        instance_id: str,
        *,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ):
        return local_api._handle_list_decision_events_local(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
        )

    def finalize_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        final_decision: str,
        decision_class: str,
        rationale: str = "",
    ):
        return local_api._handle_finalize_decision_record_local(
            instance_id,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        )

    def abandon_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        reason: str = "",
    ):
        return local_api._handle_abandon_decision_record_local(
            instance_id,
            decision_record_id,
            reason=reason,
        )

    def list_queries(self, instance_id: str):
        return local_api._handle_list_queries_local(instance_id)

    def describe_query(self, instance_id: str, query_name: str):
        return local_api._handle_describe_query_local(instance_id, query_name)

    def receipt(self, instance_id: str, receipt_id: str):
        return local_api._handle_receipt_local(instance_id, receipt_id)

    def feedback(self, instance_id: str, **kwargs):
        return local_api._handle_feedback_local(instance_id, **kwargs)

    def feedback_batch(self, instance_id: str, *, items, source: str):
        return local_api._handle_feedback_batch_local(instance_id, items=items, source=source)

    def outcome(self, instance_id: str, *, receipt_id: str, outcome: str, detail=None):
        return local_api._handle_outcome_local(
            instance_id,
            receipt_id=receipt_id,
            outcome=outcome,
            detail=detail,
        )

    def list(
        self,
        instance_id: str,
        *,
        resource_type: str,
        entity_type: str | None = None,
        relationship_type: str | None = None,
        property_filter: dict | None = None,
    ):
        return local_api._handle_list_local(
            instance_id,
            resource_type=resource_type,
            entity_type=entity_type,
            relationship_type=relationship_type,
            property_filter=property_filter,
        )

    def evaluate(self, instance_id: str, *, query_name: str, params: dict):
        return local_api._handle_evaluate_local(instance_id, query_name, params)

    def schema(self, instance_id: str):
        return local_api._handle_schema_local(instance_id)

    def sample(self, instance_id: str, entity_type: str, limit: int | None = None):
        return local_api._handle_sample_local(instance_id, entity_type, limit=limit)

    def add_relationships(self, instance_id: str, relationships):
        return local_api._handle_add_relationship_local(instance_id, relationships)

    def add_entities(self, instance_id: str, entities):
        return local_api._handle_add_entity_local(instance_id, entities)

    def add_constraint(self, instance_id: str, *, expression: str, name: str | None = None):
        return local_api._handle_add_constraint_local(instance_id, expression, name)

    def add_decision_policy(
        self,
        instance_id: str,
        *,
        name: str,
        applies_to: str,
        condition: str,
        action: str,
        priority: int = 100,
        stop_on_match: bool = True,
    ):
        return local_api._handle_add_decision_policy_local(
            instance_id,
            name=name,
            applies_to=applies_to,
            condition=condition,
            action=action,
            priority=priority,
            stop_on_match=stop_on_match,
        )

    def get_entity(self, instance_id: str, entity_type: str, entity_id: str):
        return local_api._handle_get_entity_local(instance_id, entity_type, entity_id)

    def get_relationship(
        self,
        instance_id: str,
        *,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: str | None = None,
    ):
        return local_api._handle_get_relationship_local(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        )

    def get_relationship_lineage(
        self,
        instance_id: str,
        *,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
    ):
        return local_api._handle_relationship_lineage_local(
            instance_id,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
        )

    def propose_group(
        self,
        instance_id: str,
        *,
        relationship_type: str,
        members,
        thesis_text: str | None = None,
        thesis_facts: dict | None = None,
        analysis_state: dict | None = None,
        signal_sources_used: list[str] | None = None,
        proposed_by: str = "agent",
        suggested_priority: str | None = None,
    ):
        return local_api._handle_propose_group_local(
            instance_id,
            relationship_type=relationship_type,
            members=members,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            signal_sources_used=signal_sources_used,
            proposed_by=proposed_by,
            suggested_priority=suggested_priority,
        )

    def resolve_group(
        self,
        instance_id: str,
        group_id: str,
        *,
        action: str,
        rationale: str = "",
        resolved_by: str = "human",
        expected_pending_version: int = 1,
    ):
        return local_api._handle_resolve_group_local(
            instance_id,
            group_id=group_id,
            action=action,
            rationale=rationale,
            resolved_by=resolved_by,
            expected_pending_version=expected_pending_version,
        )

    def update_trust_status(
        self,
        instance_id: str,
        resolution_id: str,
        *,
        trust_status: str,
        reason: str = "",
    ):
        return local_api._handle_update_trust_status_local(
            instance_id,
            resolution_id=resolution_id,
            trust_status=trust_status,
            reason=reason,
        )

    def get_group(self, instance_id: str, group_id: str):
        return local_api._handle_get_group_local(instance_id, group_id)

    def get_group_status(
        self,
        instance_id: str,
        *,
        group_id: str | None = None,
        signature: str | None = None,
    ):
        return local_api._handle_group_status_local(
            instance_id,
            group_id=group_id,
            signature=signature,
        )

    def list_groups(
        self,
        instance_id: str,
        *,
        relationship_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ):
        return local_api._handle_list_groups_local(
            instance_id,
            relationship_type=relationship_type,
            status=status,
            limit=limit,
        )

    def list_resolutions(
        self,
        instance_id: str,
        *,
        relationship_type: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ):
        return local_api._handle_list_resolutions_local(
            instance_id,
            relationship_type=relationship_type,
            action=action,
            limit=limit,
        )

    def create_world_overlay(
        self,
        *,
        root_dir: str,
        transport_ref: str | None = None,
        world_ref: str | None = None,
        kit: str | None = None,
        no_kit: bool = False,
    ):
        return local_api._handle_create_world_overlay_governed(
            transport_ref,
            world_ref,
            kit,
            no_kit,
            root_dir,
        )

    def world_publish(self, instance_id: str, *, output_dir: str | None = None):
        return local_api._handle_world_publish_local(instance_id, output_dir=output_dir)

    def world_status(self, instance_id: str):
        return local_api._handle_world_status_local(instance_id)

    def world_pull_preview(self, instance_id: str):
        return local_api._handle_world_pull_preview_local(instance_id)

    def world_pull_apply(self, instance_id: str):
        return local_api._handle_world_pull_apply_local(instance_id)


@pytest.fixture
def governed_client(monkeypatch: pytest.MonkeyPatch) -> GovernedLocalClient:
    """Patch MCP handlers to use an in-process governed client adapter."""
    client = GovernedLocalClient()
    monkeypatch.setattr(mcp_handlers, "_get_client", lambda: client)
    return client
