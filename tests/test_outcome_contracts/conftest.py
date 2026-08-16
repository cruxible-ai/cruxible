"""Shared fixtures for resolution-contract (outcome forcing) tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import EntityInstance
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.service import service_add_entities

# Anchors are wall-clock-relative: the service compares check_at against
# the real clock, so pinned dates rot into due-queue membership the day
# they pass (this file shipped with NOW = 2026-07-25 and failed a week
# later). Relative anchors keep the past/future semantics permanently.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CHECK_AT = NOW - timedelta(days=1)
EXPIRES_AT = NOW + timedelta(days=30)
FUTURE_CHECK_AT = NOW + timedelta(days=7)

# One decision type carrying the kit-declared adoption property
# (outcome_tracking) plus a Service the measurement query can count.
BASE_ENTITY_TYPES = """\
  Decision:
    properties:
      decision_id:
        type: string
        primary_key: true
      status:
        type: string
        enum: [proposed, accepted, rejected]
      outcome_tracking:
        type: string
        enum: [required, not_applicable]
      title:
        type: string
  Service:
    properties:
      service_id:
        type: string
        primary_key: true
      health:
        type: string
        enum: [healthy, degraded]
  Control:
    properties:
      control_id:
        type: string
        primary_key: true
"""

BASE_TAIL = """\
relationships:
  - name: protected_by
    from: Service
    to: Control
    properties:
      severity:
        type: string

named_queries:
  healthy_services:
    mode: collection
    returns: Service
    result_shape: entity
    where:
      result.properties.health:
        eq: healthy
  service_controls:
    mode: traversal
    entry_point: Service
    returns: Control
    result_shape: entity
    traversal:
      - relationship: protected_by
        direction: outgoing
  overridable_service_controls:
    mode: traversal
    entry_point: Service
    returns: Control
    result_shape: entity
    allow_relationship_state_override: true
    traversal:
      - relationship: protected_by
        direction: outgoing
"""


def _config(guard_block: str = "") -> str:
    return (
        'version: "1.0"\nname: outcome_forcing\n\n'
        f"entity_types:\n{BASE_ENTITY_TYPES}\n{BASE_TAIL}{guard_block}"
    )


UNGUARDED_CONFIG = _config()

GUARDED_CONFIG = _config(
    """
mutation_guards:
  - name: decision_acceptance_needs_contract
    entity_type: Decision
    property: status
    new_value: accepted
    message: accepting a tracked decision requires a resolution contract
    where:
      candidate.properties.outcome_tracking:
        eq: required
    condition:
      type: requires_resolution_contract
"""
)

# Same guard, but every Decision opts out: outcome_tracking is not_applicable,
# so the trigger's `where` scope never matches.
OPT_OUT_CONFIG = GUARDED_CONFIG

# The migration shape: the adoption property exists but is OPTIONAL, so records
# predating it carry no value at all. The guard must fail closed on those rather
# than letting them slip out of scope.
LEGACY_TRACKING_CONFIG = GUARDED_CONFIG.replace(
    """      outcome_tracking:
        type: string
        enum: [required, not_applicable]
""",
    """      outcome_tracking:
        type: string
        optional: true
        enum: [required, not_applicable]
""",
)


@pytest.fixture
def contract_instance(tmp_path: Path) -> CruxibleInstance:
    """A guarded instance used to exercise the store and service semantics.

    These tests never take the acceptance path — activation is written directly
    — but opening now requires the type to be covered by an outcome guard, so
    the config must declare one for the service surface to be reachable at all.
    """
    return _instance(tmp_path, GUARDED_CONFIG)


@pytest.fixture
def unguarded_instance(tmp_path: Path) -> CruxibleInstance:
    """An instance whose Decision type no outcome guard covers."""
    return _instance(tmp_path, UNGUARDED_CONFIG)


@pytest.fixture
def guarded_instance(tmp_path: Path) -> CruxibleInstance:
    """An instance whose Decision.status -> accepted requires a contract."""
    return _instance(tmp_path, GUARDED_CONFIG)


@pytest.fixture
def legacy_guarded_instance(tmp_path: Path) -> CruxibleInstance:
    """A guarded instance where the adoption property is optional (pre-migration)."""
    return _instance(tmp_path, LEGACY_TRACKING_CONFIG)


def _instance(root: Path, config_yaml: str) -> CruxibleInstance:
    (root / "config.yaml").write_text(config_yaml)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Service",
                entity_id="svc-1",
                properties={"service_id": "svc-1", "health": "healthy"},
            ),
            EntityInstance(
                entity_type="Control",
                entity_id="ctl-1",
                properties={"control_id": "ctl-1"},
            ),
        ],
        actor_context=actor("seed"),
    )
    return instance


def actor(actor_id: str, operation_id: str | None = None) -> GovernedActorContext:
    """Build a stable attributed test actor."""
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org-outcome",
        operation_id=operation_id or f"op-{actor_id}",
        timestamp=NOW,
    )


def evidence(label: str = "observation") -> EvidenceRef:
    """Build one valid evidence pointer."""
    return EvidenceRef(
        source="test",
        source_record_id=f"record-{label}",
        artifact_id=f"artifact-{label}",
    )


def add_decision(
    instance: CruxibleInstance,
    decision_id: str = "dd-1",
    *,
    status: str = "proposed",
    outcome_tracking: str | None = "required",
    title: str = "Adopt the thing",
) -> EntityInstance:
    """Create one proposed decision subject.

    ``outcome_tracking=None`` omits the adoption property entirely, which is
    what a record created before the property existed looks like.
    """
    properties: dict[str, Any] = {
        "decision_id": decision_id,
        "status": status,
        "title": title,
    }
    if outcome_tracking is not None:
        properties["outcome_tracking"] = outcome_tracking
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Decision",
                entity_id=decision_id,
                properties=properties,
            )
        ],
        actor_context=actor("proposer"),
    )
    stored = instance.load_graph().get_entity("Decision", decision_id)
    assert stored is not None
    return stored


def query_measurement(**overrides: Any) -> dict[str, Any]:
    """The canonical query measurement used across the suite."""
    payload: dict[str, Any] = {
        "kind": "query",
        "query_name": "healthy_services",
        "params": {},
        "expect": {"min_count": 1},
    }
    payload.update(overrides)
    return payload


def attestation_measurement(**overrides: Any) -> dict[str, Any]:
    """The canonical attestation measurement used across the suite."""
    payload: dict[str, Any] = {
        "kind": "attestation",
        "relationship_type": "protected_by",
        "from_type": "Service",
        "from_id": "svc-1",
        "to_type": "Control",
        "to_id": "ctl-1",
    }
    payload.update(overrides)
    return payload
