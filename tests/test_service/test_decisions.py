"""Tests for decision records and operation auto-logging."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.decision.types import DecisionEvent
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import (
    OperationContext,
    service_abandon_decision_record,
    service_create_decision_record,
    service_finalize_decision_record,
    service_get_decision_record,
    service_list_decision_events,
    service_lock,
    service_query,
    service_run,
)
from cruxible_core.service.decisions import digest_payload


@pytest.fixture
def workflow_instance(tmp_path, workflow_config_yaml: str) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(workflow_config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")

    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Product",
            entity_id="SKU-123",
            properties={
                "sku": "SKU-123",
                "category": "soda",
                "base_margin": 0.2,
            },
        )
    )
    instance.save_graph(graph)
    return instance


def _actor_context(actor_id: str, operation_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=operation_id,
        timestamp="2026-06-05T12:00:00Z",
    )


def test_digest_payload_is_deterministic() -> None:
    left = {"b": 2, "a": {"x": 1}}
    right = {"a": {"x": 1}, "b": 2}

    assert digest_payload(left) == digest_payload(right)
    digest, summary = digest_payload(left)
    assert digest.startswith("sha256:")
    assert summary == '{"a": {"x": 1}, "b": 2}'


def test_decision_record_actor_context_round_trips_through_store(
    populated_instance: CruxibleInstance,
) -> None:
    opened_actor = _actor_context("usr_open", "op_open")
    finalized_actor = _actor_context("usr_finalize", "op_finalize")

    record = service_create_decision_record(
        populated_instance,
        question="Should we investigate this vehicle impact?",
        subject_type="Vehicle",
        subject_id="V-2024-CIVIC-EX",
        opened_by="agent",
        actor_context=opened_actor,
    ).record

    loaded_open = service_get_decision_record(
        populated_instance,
        record.decision_record_id,
    ).record
    assert loaded_open.opened_actor_context is not None
    assert loaded_open.opened_actor_context.actor_id == "usr_open"
    assert loaded_open.opened_actor_context.operation_id == "op_open"
    assert loaded_open.finalized_actor_context is None

    service_finalize_decision_record(
        populated_instance,
        record.decision_record_id,
        final_decision="No action",
        decision_class="deferred",
        actor_context=finalized_actor,
    )

    loaded_final = service_get_decision_record(
        populated_instance,
        record.decision_record_id,
    ).record
    assert loaded_final.opened_actor_context is not None
    assert loaded_final.opened_actor_context.actor_id == "usr_open"
    assert loaded_final.opened_actor_context.operation_id == "op_open"
    assert loaded_final.finalized_actor_context is not None
    assert loaded_final.finalized_actor_context.actor_id == "usr_finalize"
    assert loaded_final.finalized_actor_context.operation_id == "op_finalize"


def test_abandoned_decision_record_actor_context_round_trips_through_store(
    populated_instance: CruxibleInstance,
) -> None:
    opened_actor = _actor_context("usr_open", "op_open")
    abandoned_actor = _actor_context("usr_abandon", "op_abandon")
    record = service_create_decision_record(
        populated_instance,
        question="Should we abandon this investigation?",
        actor_context=opened_actor,
    ).record

    service_abandon_decision_record(
        populated_instance,
        record.decision_record_id,
        reason="Superseded",
        actor_context=abandoned_actor,
    )

    loaded = service_get_decision_record(
        populated_instance,
        record.decision_record_id,
    ).record
    assert loaded.opened_actor_context is not None
    assert loaded.opened_actor_context.actor_id == "usr_open"
    assert loaded.finalized_actor_context is not None
    assert loaded.finalized_actor_context.actor_id == "usr_abandon"
    assert loaded.finalized_actor_context.operation_id == "op_abandon"


def test_decision_event_actor_context_round_trips_through_store(
    populated_instance: CruxibleInstance,
) -> None:
    event_actor = _actor_context("usr_event", "op_event")
    record = service_create_decision_record(
        populated_instance,
        question="Should we log this event?",
    ).record
    started_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)

    with populated_instance.write_transaction() as uow:
        uow.decisions.append_event(
            DecisionEvent(
                decision_record_id=record.decision_record_id,
                command="manual",
                status="success",
                input_digest="sha256:input",
                input_summary="{}",
                actor_context=event_actor,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    events = service_list_decision_events(
        populated_instance,
        decision_record_id=record.decision_record_id,
    ).items
    assert len(events) == 1
    assert events[0].actor_context is not None
    assert events[0].actor_context.actor_id == "usr_event"
    assert events[0].actor_context.operation_id == "op_event"


def test_query_decision_record_context_records_audit_event(
    populated_instance: CruxibleInstance,
) -> None:
    snapshot = populated_instance.create_snapshot()
    record = service_create_decision_record(
        populated_instance,
        question="Should we investigate this vehicle impact?",
        subject_type="Vehicle",
        subject_id="V-2024-CIVIC-EX",
        opened_by="agent",
    ).record

    query = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
        context=OperationContext(decision_record_id=record.decision_record_id, surface="cli"),
    )

    events = service_list_decision_events(
        populated_instance,
        decision_record_id=record.decision_record_id,
    ).items

    assert len(events) == 1
    assert events[0].command == "query:parts_for_vehicle"
    assert events[0].status == "success"
    assert events[0].receipt_id == query.receipt_id
    assert events[0].head_snapshot_id == snapshot.snapshot_id
    assert events[0].surface == "cli"
    assert events[0].output_digest is not None


def test_query_without_decision_record_context_does_not_record_event(
    populated_instance: CruxibleInstance,
) -> None:
    record = service_create_decision_record(
        populated_instance,
        question="Should we investigate this vehicle impact?",
        subject_type="Vehicle",
        subject_id="V-2024-CIVIC-EX",
        opened_by="agent",
    ).record

    query = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
    )

    events = service_list_decision_events(
        populated_instance,
        decision_record_id=record.decision_record_id,
    ).items

    assert query.receipt_id is not None
    assert events == []


def test_decision_support_requires_open_decision_record_before_execution(
    workflow_instance: CruxibleInstance,
) -> None:
    config = workflow_instance.load_config()
    config.workflows["evaluate_promo"].type = "decision_support"
    workflow_instance.save_config(config)
    service_lock(workflow_instance)

    receipt_store = workflow_instance.get_receipt_store()
    try:
        before = receipt_store.count_receipts(operation_type="workflow")
    finally:
        receipt_store.close()

    with pytest.raises(ConfigError, match="decision_support workflows require decision_record_id"):
        service_run(
            workflow_instance,
            "evaluate_promo",
            {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        )

    receipt_store = workflow_instance.get_receipt_store()
    try:
        after = receipt_store.count_receipts(operation_type="workflow")
    finally:
        receipt_store.close()

    assert after == before


def test_auto_log_failure_does_not_fail_underlying_workflow(
    workflow_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_lock(workflow_instance)

    class BrokenDecisionStore:
        def append_event(self, _event: DecisionEvent) -> str:
            raise RuntimeError("decision store down")

        def close(self) -> None:
            return None

    monkeypatch.setattr(workflow_instance, "get_decision_store", lambda: BrokenDecisionStore())

    result = service_run(
        workflow_instance,
        "evaluate_promo",
        {
            "sku": "SKU-123",
            "start_date": "2026-03-01",
            "end_date": "2026-03-07",
        },
        context=OperationContext(decision_record_id="DR-store-down", surface="cli"),
    )

    assert result.output["decision"] == "approve"


def test_auto_log_store_open_failure_does_not_fail_underlying_workflow(
    workflow_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_lock(workflow_instance)

    def raise_store_open():
        raise RuntimeError("decision store could not open")

    monkeypatch.setattr(workflow_instance, "get_decision_store", raise_store_open)

    result = service_run(
        workflow_instance,
        "evaluate_promo",
        {
            "sku": "SKU-123",
            "start_date": "2026-03-01",
            "end_date": "2026-03-07",
        },
        context=OperationContext(decision_record_id="DR-store-open-down", surface="cli"),
    )

    assert result.output["decision"] == "approve"


def test_closed_record_auto_log_race_is_best_effort(
    populated_instance: CruxibleInstance,
) -> None:
    record = service_create_decision_record(
        populated_instance,
        question="Should we investigate this vehicle impact?",
    ).record
    service_finalize_decision_record(
        populated_instance,
        record.decision_record_id,
        final_decision="No action",
        decision_class="deferred",
    )

    query = service_query(
        populated_instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-2024-CIVIC-EX"},
        context=OperationContext(decision_record_id=record.decision_record_id, surface="cli"),
    )

    events = service_list_decision_events(
        populated_instance,
        decision_record_id=record.decision_record_id,
    ).items
    assert query.receipt_id is not None
    assert events == []

    started_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)
    with pytest.raises(ConfigError, match="is not open"):
        with populated_instance.write_transaction() as uow:
            uow.decisions.append_event(
                DecisionEvent(
                    decision_record_id=record.decision_record_id,
                    command="manual",
                    status="success",
                    input_digest="sha256:input",
                    input_summary="{}",
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
