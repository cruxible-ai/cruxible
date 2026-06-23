"""Tests for the status -> entity-lifecycle migration."""

from __future__ import annotations

from cruxible_core.graph.assertion_state import entity_lifecycle_status
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.migrations import migrate_status_to_lifecycle


def _graph_with_statuses() -> EntityGraph:
    g = EntityGraph()
    g.add_entity(
        EntityInstance(
            entity_type="WorkItem",
            entity_id="WI-1",
            properties={"status": "superseded", "title": "Old plan"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="WorkItem",
            entity_id="WI-2",
            properties={"status": "active", "title": "Current plan"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Decision",
            entity_id="D-1",
            properties={"status": "superseded", "summary": "Reversed"},
        )
    )
    return g


def test_migration_moves_superseded_status_onto_lifecycle() -> None:
    g = _graph_with_statuses()
    report = migrate_status_to_lifecycle(g)

    assert report.migrated == 2
    assert report.scanned == 2

    wi1 = g.get_entity("WorkItem", "WI-1")
    assert wi1 is not None
    # Retirement moved to the lifecycle axis...
    assert entity_lifecycle_status(wi1.metadata) == "superseded"
    # ...and the domain status reset to a valid progress-terminal value.
    assert wi1.properties["status"] == "closed"
    # Sibling property untouched.
    assert wi1.properties["title"] == "Old plan"

    decision = g.get_entity("Decision", "D-1")
    assert decision is not None
    assert entity_lifecycle_status(decision.metadata) == "superseded"

    # Non-retirement entity is untouched.
    wi2 = g.get_entity("WorkItem", "WI-2")
    assert wi2 is not None
    assert entity_lifecycle_status(wi2.metadata) == "live"
    assert wi2.properties["status"] == "active"


def test_migration_dry_run_reports_without_mutating() -> None:
    g = _graph_with_statuses()
    report = migrate_status_to_lifecycle(g, dry_run=True)

    assert report.dry_run is True
    assert report.migrated == 2
    assert {(m[0], m[1]) for m in report.migrations} == {
        ("WorkItem", "WI-1"),
        ("Decision", "D-1"),
    }
    # Graph unchanged.
    wi1 = g.get_entity("WorkItem", "WI-1")
    assert wi1 is not None
    assert entity_lifecycle_status(wi1.metadata) == "live"
    assert wi1.properties["status"] == "superseded"


def test_migration_is_idempotent() -> None:
    g = _graph_with_statuses()
    migrate_status_to_lifecycle(g)
    # Second pass finds nothing left at `superseded` (status now `closed`).
    second = migrate_status_to_lifecycle(g)
    assert second.migrated == 0
    assert second.scanned == 0


def test_migration_skips_entities_with_existing_lifecycle() -> None:
    g = EntityGraph()
    g.add_entity(
        EntityInstance(
            entity_type="WorkItem",
            entity_id="WI-X",
            properties={"status": "superseded"},
            metadata={"lifecycle": {"status": "retired"}},
        )
    )
    report = migrate_status_to_lifecycle(g)
    assert report.migrated == 0
    assert report.skipped_existing_lifecycle == 1
    # Explicit lifecycle decision preserved.
    entity = g.get_entity("WorkItem", "WI-X")
    assert entity is not None
    assert entity_lifecycle_status(entity.metadata) == "retired"
