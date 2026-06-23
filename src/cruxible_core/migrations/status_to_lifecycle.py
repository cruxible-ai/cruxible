"""Migrate retirement values out of domain ``status`` onto entity lifecycle.

Domain ``status`` enums model *progress* (planned/active/closed). Retirement and
deletion are a separate canonical axis -- the entity ``lifecycle.status``
(``live``/``superseded``/``retired``/``orphaned``), gated out of live reads. This
migration moves any entity currently carrying a retirement value (e.g.
``status: superseded``) in a domain status property onto ``lifecycle.status``,
clearing the obsolete domain value so the entity validates against a cleaned-up
status enum that no longer lists the retirement value.

The default mapping covers ``superseded`` (the value removed from the
project-state / agent-operation ``work_item_status`` and ``decision_status``
enums). The migration is idempotent: an entity already carrying
``lifecycle.status`` is left untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cruxible_core.graph.assertion_state import (
    EntityLifecycleStatus,
    build_entity_lifecycle_metadata,
    entity_lifecycle_status,
)

if TYPE_CHECKING:
    from cruxible_core.graph.entity_graph import EntityGraph

# Domain status value -> entity lifecycle status it migrates to. Only retirement
# values appear here; progress values (planned/active/closed/...) stay in the
# domain status enum and are never touched.
DEFAULT_RETIREMENT_MAPPING: dict[str, EntityLifecycleStatus] = {
    "superseded": "superseded",
}

# After moving the retirement value off the domain status, the property is reset
# to this progress-terminal value so the entity still satisfies the (cleaned-up)
# status enum, which retains ``closed`` as its terminal progress state.
DOMAIN_STATUS_AFTER_RETIREMENT = "closed"


@dataclass
class StatusToLifecycleReport:
    """Summary of what a status-to-lifecycle migration did (or would do)."""

    dry_run: bool
    status_property: str
    scanned: int = 0
    migrated: int = 0
    skipped_existing_lifecycle: int = 0
    # (entity_type, entity_id, from_status, to_lifecycle) for each entity moved.
    migrations: list[tuple[str, str, str, str]] = field(default_factory=list)


def migrate_status_to_lifecycle(
    graph: EntityGraph,
    *,
    status_property: str = "status",
    retirement_mapping: Mapping[str, EntityLifecycleStatus] | None = None,
    domain_status_after: str = DOMAIN_STATUS_AFTER_RETIREMENT,
    dry_run: bool = False,
) -> StatusToLifecycleReport:
    """Move retirement status values onto ``lifecycle.status`` for every entity.

    Args:
        graph: The graph to scan and (unless ``dry_run``) mutate in place.
        status_property: Name of the domain status property to inspect.
        retirement_mapping: Domain-status -> lifecycle-status map. Defaults to
            ``{"superseded": "superseded"}``.
        domain_status_after: Value to write back into the domain status property
            once the retirement value has moved (so the entity validates against
            the cleaned-up enum). Set to ``None``-like only if the property is
            optional; the default keeps a valid terminal progress value.
        dry_run: When true, report what would change without mutating the graph.

    Returns:
        A :class:`StatusToLifecycleReport`. Idempotent: entities that already
        carry an explicit ``lifecycle.status`` are skipped.
    """
    mapping = dict(retirement_mapping or DEFAULT_RETIREMENT_MAPPING)
    report = StatusToLifecycleReport(dry_run=dry_run, status_property=status_property)

    for entity_type in graph.list_entity_types():
        for entity in graph.list_entities(entity_type):
            current_status = entity.properties.get(status_property)
            if not isinstance(current_status, str) or current_status not in mapping:
                continue
            report.scanned += 1

            # Idempotency: never overwrite an explicit lifecycle decision.
            if entity_lifecycle_status(entity.metadata) != "live":
                report.skipped_existing_lifecycle += 1
                continue

            target_lifecycle = mapping[current_status]
            report.migrated += 1
            report.migrations.append(
                (entity.entity_type, entity.entity_id, current_status, target_lifecycle)
            )
            if dry_run:
                continue

            # Construct + validate the typed lifecycle, then store its serialized
            # form -- never a hand-authored ``{"lifecycle": {...}}`` blob.
            graph.update_entity_metadata(
                entity.entity_type,
                entity.entity_id,
                build_entity_lifecycle_metadata(status=target_lifecycle),
            )
            if domain_status_after is not None:
                graph.update_entity_properties(
                    entity.entity_type,
                    entity.entity_id,
                    {status_property: domain_status_after},
                )

    return report


__all__ = [
    "DEFAULT_RETIREMENT_MAPPING",
    "DOMAIN_STATUS_AFTER_RETIREMENT",
    "StatusToLifecycleReport",
    "migrate_status_to_lifecycle",
]
