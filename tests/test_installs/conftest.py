"""Shared fixtures for install-ledger tests.

The ledger is deliberately independent of config CONTENT — it never reads the
deployed config — so the instance here carries the smallest valid schema that
lets an instance exist at all. Anything richer would imply a coupling the
ledger does not have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.installs.types import ArtifactRef, InstallRecord, ObjectReference
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import service_create_install
from cruxible_core.temporal import utc_now

MINIMAL_CONFIG = """\
version: "1.0"
name: install_ledger

entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
"""


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    """A bare initialized instance with an empty install ledger."""
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def actor(actor_id: str = "installer") -> GovernedActorContext:
    """Build a stable attributed test actor."""
    return GovernedActorContext(
        actor_type="service_account",
        actor_id=actor_id,
        org_id="org-installs",
        operation_id=f"op-{actor_id}",
        timestamp=utc_now(),
    )


def create_install(
    instance: CruxibleInstance,
    *,
    artifact_id: str = "kev-triage",
    artifact_version: str = "1.0.0",
    artifact_digest: str = "sha256:blueprint-a",
    artifact_kind: str = "blueprint",
    install_id: str | None = None,
    actor_id: str = "installer",
) -> InstallRecord:
    """Open one install in ``preparing``."""
    return service_create_install(
        instance,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_digest=artifact_digest,
        actor_context=actor(actor_id),
        install_id=install_id,
    )


def artifact_ref(**overrides: str) -> ArtifactRef:
    """Build a minimal artifact reference."""
    payload = {
        "artifact_kind": "blueprint",
        "artifact_id": "kev-triage",
        "artifact_version": "1.0.0",
        "artifact_digest": "sha256:blueprint-a",
    }
    payload.update(overrides)
    return ArtifactRef(**payload)  # type: ignore[arg-type]


def reference(object_kind: str, object_name: str) -> ObjectReference:
    """Build one declared dependency pointer."""
    return ObjectReference.model_validate({"object_kind": object_kind, "object_name": object_name})


def load_receipt(instance: CruxibleInstance, receipt_id: str | None) -> Receipt:
    """Load one persisted receipt, asserting it exists."""
    assert receipt_id is not None
    receipt = instance.get_receipt_store().get_receipt(receipt_id)
    assert receipt is not None, f"receipt {receipt_id} was not persisted"
    return receipt


def validation_details(receipt: Receipt) -> list[dict[str, object]]:
    """Every validation node's detail payload, in receipt order."""
    return [node.detail or {} for node in receipt.nodes if node.node_type == "validation"]
