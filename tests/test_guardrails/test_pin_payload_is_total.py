"""Every source field is CLASSIFIED into a pin payload or an exemption.

A pin exists to record the world a procedure was accepted against. A field
that is behaviourally relevant and silently absent from the payload makes the
pin a description of that world rather than the world -- and the gap is
invisible, because the pin still verifies against itself.

So the classification is total: adding a field to a locked provider or artifact
fails this test until someone says which side it falls on.
"""

from __future__ import annotations

import pytest

from cruxible_core.procedure.pins import (
    ARTIFACT_PIN_FIELDS,
    PIN_KINDS,
    PIN_PAYLOAD_FIELDS,
    PROVIDER_PIN_FIELDS,
)
from cruxible_core.workflow.types import LockedArtifact, LockedProvider

PROVIDER_PIN_EXEMPTIONS: frozenset[str] = frozenset()
"""Locked-provider fields deliberately outside the payload. Empty by design:
all nine are behaviourally relevant, and three of them -- `deterministic`,
`side_effects`, `config` -- change what a provider is allowed to do and does."""

ARTIFACT_PIN_EXEMPTIONS: frozenset[str] = frozenset()


def test_every_locked_provider_field_is_pinned_or_exempt() -> None:
    unclassified = sorted(
        set(LockedProvider.model_fields) - set(PROVIDER_PIN_FIELDS) - PROVIDER_PIN_EXEMPTIONS
    )
    assert unclassified == [], (
        f"{unclassified} are locked-provider fields absent from the provider pin "
        "payload. Add them, or exempt them with the reason they cannot change "
        "what the accepted procedure does."
    )


def test_every_locked_artifact_field_is_pinned_or_exempt() -> None:
    unclassified = sorted(
        set(LockedArtifact.model_fields) - set(ARTIFACT_PIN_FIELDS) - ARTIFACT_PIN_EXEMPTIONS
    )
    assert unclassified == [], (
        f"{unclassified} are locked-artifact fields absent from the artifact pin payload"
    )


def test_the_provider_payload_is_all_nine_fields() -> None:
    assert len(PROVIDER_PIN_FIELDS) == 9
    assert {"deterministic", "side_effects", "config"}.issubset(PROVIDER_PIN_FIELDS)


@pytest.mark.parametrize("kind", PIN_KINDS)
def test_every_pin_kind_declares_a_closed_payload(kind: str) -> None:
    assert PIN_PAYLOAD_FIELDS[kind], f"pin kind '{kind}' declares no payload fields"


def test_the_parameter_payload_carries_the_value() -> None:
    """A run needs no parameter lookup at all, which is what makes the pin the
    executable dependency rather than a pointer at one."""
    assert "value" in PIN_PAYLOAD_FIELDS["parameter"]
