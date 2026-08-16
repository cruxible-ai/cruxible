"""PC-A1 request-attribution and transport-capability separation tests."""

from __future__ import annotations

from datetime import datetime, timezone

from cruxible_core.playbill.actor_context import (
    TRANSPORT_CAPABILITIES,
    GovernedActorContext,
    derived_actor_kind,
    dump_actor_context,
    load_actor_context,
)
from cruxible_core.playbill.proposals import AuthenticatedActor


def test_actor_context_is_playbill_owned_request_attribution() -> None:
    actor = GovernedActorContext(
        actor_type="service_account",
        actor_id="agent-1",
        org_id="org-1",
        operation_id="op-1",
        timestamp=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )
    assert derived_actor_kind(actor) == "agent"
    assert load_actor_context(dump_actor_context(actor)) == actor
    assert TRANSPORT_CAPABILITIES == (
        "activate",
        "administer",
        "operate",
        "propose",
        "read",
        "review",
    )


def test_authenticated_actor_capabilities_are_endpoint_permissions_only() -> None:
    actor = AuthenticatedActor(
        actor_id="reviewer",
        capabilities=("administer", "propose"),
    )
    assert actor.capabilities == ("administer", "propose")
