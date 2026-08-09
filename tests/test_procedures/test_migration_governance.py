"""Governance and ordinary-verb orchestration for procedure migration."""

from __future__ import annotations

import inspect

import pytest

from cruxible_core.cli.commands import procedures as procedure_commands
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.procedure import migration as procedure_migration
from cruxible_core.procedure.types import ProcedureRecord
from cruxible_core.service import (
    procedure_migrations,
    service_accept_procedure,
    service_migrate_procedures,
    service_propose_procedure,
)
from tests.test_procedures.conftest import actor, provider_definition


def _live_v1(instance: InstanceProtocol, name: str) -> ProcedureRecord:
    proposed = service_propose_procedure(
        instance,
        provider_definition(name),
        actor_context=actor(f"{name}-author"),
    )
    return service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=proposed.procedure.version,
        actor_context=actor(f"{name}-reviewer"),
    ).procedure


def _counts(instance: InstanceProtocol) -> tuple[int, int]:
    procedures = instance.get_procedure_store()
    receipts = instance.get_receipt_store()
    try:
        return procedures.count_procedures(), receipts.count_receipts()
    finally:
        procedures.close()
        receipts.close()


def test_same_actor_apply_is_refused_up_front_before_any_write(
    procedure_instance: InstanceProtocol,
) -> None:
    _live_v1(procedure_instance, "same_actor_refusal")
    before = _counts(procedure_instance)
    same_actor = actor("migration-self-reviewer")

    with pytest.raises(
        ConfigError,
        match="refused before any write.*both identify actor 'migration-self-reviewer'",
    ):
        service_migrate_procedures(
            procedure_instance,
            apply=True,
            proposer_actor=same_actor,
            reviewer_actor=same_actor.model_copy(update={"operation_id": "op-review"}),
        )

    assert _counts(procedure_instance) == before


def test_rerun_after_propose_only_creates_no_second_pending_lift(
    procedure_instance: InstanceProtocol,
) -> None:
    predecessor = _live_v1(procedure_instance, "propose_only_idempotence")

    first = service_migrate_procedures(
        procedure_instance,
        apply=True,
        proposer_actor=actor("migration-proposer"),
    )
    second = service_migrate_procedures(
        procedure_instance,
        apply=True,
        proposer_actor=actor("migration-proposer", "op-second-sweep"),
    )

    assert [(item.outcome, item.dedupe_disposition) for item in first.items] == [
        ("proposed", "none")
    ]
    assert [(item.outcome, item.dedupe_disposition) for item in second.items] == [
        ("already_pending", "matching")
    ]
    store = procedure_instance.get_procedure_store()
    try:
        pending = store.list_procedures(name="propose_only_idempotence", status="pending")
        live = store.list_procedures(name="propose_only_idempotence", status="live")
    finally:
        store.close()
    assert len(pending) == 1
    assert [row.procedure_id for row in live] == [predecessor.procedure_id]


def test_distinct_actors_propose_accept_and_retire_through_ordinary_verbs(
    procedure_instance: InstanceProtocol,
) -> None:
    predecessor = _live_v1(procedure_instance, "supervised_lift")

    result = service_migrate_procedures(
        procedure_instance,
        apply=True,
        proposer_actor=actor("migration-proposer"),
        reviewer_actor=actor("migration-reviewer"),
    )

    assert [(item.name, item.outcome) for item in result.items] == [("supervised_lift", "accepted")]
    successor_id = result.items[0].successor_procedure_id
    assert successor_id is not None
    store = procedure_instance.get_procedure_store()
    try:
        retired = store.get_procedure(predecessor.procedure_id)
        successor = store.get_procedure(successor_id)
    finally:
        store.close()
    assert retired is not None
    assert retired.status == "retired"
    assert successor is not None
    assert successor.status == "live"
    assert successor.definition.graph_format == 2
    assert successor.proposed_actor_context is not None
    assert successor.proposed_actor_context.actor_id == "migration-proposer"
    assert successor.resolved_actor_context is not None
    assert successor.resolved_actor_context.actor_id == "migration-reviewer"


def test_migrate_path_uses_ordinary_verbs_and_has_no_direct_procedure_table_write() -> None:
    migration_source = "\n".join(
        inspect.getsource(module)
        for module in (
            procedure_migration,
            procedure_migrations,
            procedure_commands,
        )
    )
    direct_write_fragments = (
        "save_procedure(",
        "transition_procedure(",
        "insert into procedures",
        "update procedures",
        "delete from procedures",
    )

    assert all(fragment not in migration_source.lower() for fragment in direct_write_fragments)
    assert "service_propose_procedure(" in migration_source
    assert "service_accept_procedure(" in migration_source
    assert ".propose_procedure(" in migration_source
    assert ".resolve_procedure(" in migration_source


def test_sweep_uses_stable_dependency_order(
    procedure_instance: InstanceProtocol,
) -> None:
    _live_v1(procedure_instance, "z_last")
    _live_v1(procedure_instance, "a_first")

    result = service_migrate_procedures(procedure_instance, apply=False)

    assert [item.name for item in result.items] == ["a_first", "z_last"]
