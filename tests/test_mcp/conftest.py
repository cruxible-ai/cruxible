"""Shared fixtures for MCP tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.mcp import handlers as mcp_handlers
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import api
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
    monkeypatch.delenv("CRUXIBLE_REQUIRE_SERVER", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_TOOLS", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_TOOL_ALLOWLIST", raising=False)
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
        return api.init_governed(
            root_dir=root_dir,
            config_path=config_path,
            config_yaml=config_yaml,
            data_dir=data_dir,
            kit=kit,
        )

    def validate(self, config_path: str | None = None, config_yaml: str | None = None):
        return api.validate(config_path=config_path, config_yaml=config_yaml)

    def server_info(self):
        return api.server_info()

    def workflow_lock(self, instance_id: str, *, force: bool = False):
        return api.workflow_lock(instance_id, force=force)

    def workflow_plan(self, instance_id: str, *, workflow_name: str, input_payload=None):
        return api.workflow_plan(instance_id, workflow_name, input_payload)

    def workflow_run(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload=None,
        decision_record_id: str | None = None,
    ):
        return api.workflow_run(
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
        return api.workflow_apply(
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
        return api.propose_workflow(
            instance_id,
            workflow_name,
            input_payload,
            decision_record_id=decision_record_id,
            surface="mcp",
        )

    def query(
        self,
        instance_id: str,
        query_name: str,
        params: dict,
        limit: int | None = None,
        offset: int = 0,
        decision_record_id: str | None = None,
    ):
        return api.query(
            instance_id,
            query_name,
            params,
            limit=limit,
            offset=offset,
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
        return api.create_decision_record(
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
        return api.get_decision_record(
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
        offset: int = 0,
    ):
        return api.list_decision_records(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
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
        offset: int = 0,
    ):
        return api.list_decision_events(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
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
        return api.finalize_decision_record(
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
        return api.abandon_decision_record(
            instance_id,
            decision_record_id,
            reason=reason,
        )

    def list_queries(self, instance_id: str, *, limit: int | None = None, offset: int = 0):
        return api.list_queries(instance_id, limit=limit, offset=offset)

    def describe_query(self, instance_id: str, query_name: str):
        return api.describe_query(instance_id, query_name)

    def receipt(self, instance_id: str, receipt_id: str):
        return api.receipt(instance_id, receipt_id)

    def feedback(self, instance_id: str, **kwargs):
        return api.feedback(instance_id, **kwargs)

    def feedback_batch(self, instance_id: str, *, items, source: str):
        return api.feedback_batch(instance_id, items=items, source=source)

    def feedback_from_query(self, instance_id: str, **kwargs):
        return api.feedback_from_query(instance_id, **kwargs)

    def outcome(
        self,
        instance_id: str,
        *,
        receipt_id: str | None = None,
        outcome: str,
        anchor_type: str = "receipt",
        anchor_id: str | None = None,
        source: str = "human",
        outcome_code: str | None = None,
        scope_hints: dict | None = None,
        outcome_profile_key: str | None = None,
        detail=None,
    ):
        return api.outcome(
            instance_id,
            receipt_id=receipt_id,
            outcome=outcome,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            source=source,
            outcome_code=outcome_code,
            scope_hints=scope_hints,
            outcome_profile_key=outcome_profile_key,
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
        limit: int = 50,
        offset: int = 0,
    ):
        return api.list_resources(
            instance_id,
            resource_type=resource_type,
            entity_type=entity_type,
            relationship_type=relationship_type,
            property_filter=property_filter,
            limit=limit,
            offset=offset,
        )

    def evaluate(
        self,
        instance_id: str,
        *,
        max_findings: int = 100,
        exclude_orphan_types: list[str] | None = None,
    ):
        return api.evaluate(
            instance_id,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
        )

    def schema(self, instance_id: str):
        return api.schema(instance_id)

    def sample(self, instance_id: str, entity_type: str, limit: int | None = None):
        return api.sample(instance_id, entity_type, limit=limit)

    def add_relationships(self, instance_id: str, relationships, *, dry_run: bool = False):
        return api.add_relationships(instance_id, relationships, dry_run=dry_run)

    def add_entities(self, instance_id: str, entities, *, dry_run: bool = False):
        return api.add_entities(instance_id, entities, dry_run=dry_run)

    def add_constraint(
        self,
        instance_id: str,
        *,
        name: str,
        rule: str,
        severity: str = "warning",
        description: str | None = None,
    ):
        return api.add_constraint(
            instance_id,
            name=name,
            rule=rule,
            severity=severity,
            description=description,
        )

    def add_decision_policy(
        self,
        instance_id: str,
        *,
        name: str,
        applies_to: str,
        relationship_type: str,
        effect: str,
        match=None,
        description: str | None = None,
        rationale: str = "",
        query_name: str | None = None,
        workflow_name: str | None = None,
        expires_at: str | None = None,
    ):
        return api.add_decision_policy(
            instance_id,
            name=name,
            applies_to=applies_to,
            relationship_type=relationship_type,
            effect=effect,
            match=match,
            description=description,
            rationale=rationale,
            query_name=query_name,
            workflow_name=workflow_name,
            expires_at=expires_at,
        )

    def get_entity(self, instance_id: str, entity_type: str, entity_id: str):
        return api.get_entity(instance_id, entity_type, entity_id)

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
        return api.get_relationship(
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
        return api.get_relationship_lineage(
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
        return api.propose_group(
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
        return api.resolve_group(
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
        return api.update_trust_status(
            instance_id,
            resolution_id=resolution_id,
            trust_status=trust_status,
            reason=reason,
        )

    def get_group(self, instance_id: str, group_id: str):
        return api.get_group(instance_id, group_id)

    def get_group_status(
        self,
        instance_id: str,
        *,
        group_id: str | None = None,
        signature: str | None = None,
    ):
        return api.get_group_status(
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
        offset: int = 0,
    ):
        return api.list_groups(
            instance_id,
            relationship_type=relationship_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    def list_resolutions(
        self,
        instance_id: str,
        *,
        relationship_type: str | None = None,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        return api.list_resolutions(
            instance_id,
            relationship_type=relationship_type,
            action=action,
            limit=limit,
            offset=offset,
        )

    def create_state_overlay(
        self,
        *,
        root_dir: str,
        transport_ref: str | None = None,
        state_ref: str | None = None,
        kit: str | None = None,
        no_kit: bool = False,
    ):
        return api.create_state_overlay_governed(
            transport_ref,
            state_ref,
            kit,
            no_kit,
            root_dir,
        )

    def state_publish(
        self,
        instance_id: str,
        *,
        transport_ref: str,
        state_id: str,
        release_id: str,
        compatibility: str,
    ):
        return api.state_publish(
            instance_id,
            transport_ref=transport_ref,
            state_id=state_id,
            release_id=release_id,
            compatibility=compatibility,
        )

    def state_status(self, instance_id: str):
        return api.state_status(instance_id)

    def state_pull_preview(self, instance_id: str):
        return api.state_pull_preview(instance_id)

    def state_pull_apply(self, instance_id: str, *, expected_apply_digest: str):
        return api.state_pull_apply(
            instance_id,
            expected_apply_digest=expected_apply_digest,
        )


@pytest.fixture
def governed_client(monkeypatch: pytest.MonkeyPatch) -> GovernedLocalClient:
    """Patch MCP handlers to use an in-process governed client adapter."""
    client = GovernedLocalClient()
    monkeypatch.setattr(mcp_handlers, "_get_client", lambda: client)
    return client
