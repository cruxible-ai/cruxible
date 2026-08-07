"""Fixtures for the compute-slot binding ledger tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cruxible_core.bindings.types import ProviderDescriptor, SlotInterface
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.governance.actors import GovernedActorContext

MINIMAL_CONFIG_YAML = """\
version: "1.0"
name: binding_ledger_fixture

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true

relationships: []
"""


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    """An initialized instance with the smallest config that validates."""
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


@pytest.fixture
def actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="agent-alpha",
        org_id="org-acme",
        operation_id="op-1",
        timestamp=datetime.fromisoformat("2026-08-05T12:00:00+00:00"),
    )


@pytest.fixture
def summarize_slot() -> SlotInterface:
    """A constrained slot: two contract sides plus a billing allowlist."""
    return SlotInterface(
        slot_name="summarize",
        contract_in="doc.v1",
        contract_out="summary.v1",
        allowed_billing_modes=("included", "metered"),
        requires_third_party_consent=True,
    )


@pytest.fixture
def fitting_provider() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_name="summarizer-core",
        contract_in="doc.v1",
        contract_out="summary.v1",
        billing_mode="included",
    )
