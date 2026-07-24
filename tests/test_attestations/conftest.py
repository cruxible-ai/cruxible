"""Shared fixtures for claim attestation tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import service_add_entities, service_add_relationships

CONFIG_YAML = """\
version: "1.0"
name: attestation_stage_d

entity_types:
  Service:
    properties:
      service_id:
        type: string
        primary_key: true
  Control:
    properties:
      control_id:
        type: string
        primary_key: true

relationships:
  - name: protected_by
    from: Service
    to: Control
    properties:
      severity:
        type: string

contracts:
  ProcedureInput:
    fields:
      value:
        type: int
  ProcedureOutput:
    fields:
      value:
        type: int
"""


@pytest.fixture
def attestation_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Service",
                entity_id="svc-1",
                properties={"service_id": "svc-1"},
            ),
            EntityInstance(
                entity_type="Control",
                entity_id="ctl-1",
                properties={"control_id": "ctl-1"},
            ),
        ],
    )
    return instance


def actor(actor_id: str, operation_id: str | None = None) -> GovernedActorContext:
    """Build a stable attributed test actor."""
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org-attestation",
        operation_id=operation_id or f"op-{actor_id}",
        timestamp=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
    )


def evidence(label: str = "observation") -> EvidenceRef:
    """Build one valid evidence pointer."""
    return EvidenceRef(
        source="test",
        source_record_id=f"record-{label}",
        artifact_id=f"artifact-{label}",
    )


def add_live_claim(
    instance: CruxibleInstance,
    *,
    severity: str = "high",
) -> RelationshipInstance:
    """Add or update the canonical live test claim."""
    relationship = RelationshipInstance(
        relationship_type="protected_by",
        from_type="Service",
        from_id="svc-1",
        to_type="Control",
        to_id="ctl-1",
        properties={"severity": severity},
    )
    service_add_relationships(
        instance,
        [relationship],
        "test",
        "attestation-fixture",
        actor_context=actor("claim-writer"),
    )
    stored = instance.load_graph().get_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    assert stored is not None
    return stored
