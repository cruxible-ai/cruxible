"""Frozen actor and transport-capability vocabulary shared across the wire."""

from __future__ import annotations

from typing import Literal

ActorType = Literal["human_user", "service_account", "system"]
DerivedActorKind = Literal["human", "agent", "system", "unknown"]
TransportCapability = Literal[
    "read",
    "propose",
    "review",
    "activate",
    "operate",
    "administer",
]

TRANSPORT_CAPABILITIES: tuple[TransportCapability, ...] = (
    "activate",
    "administer",
    "operate",
    "propose",
    "read",
    "review",
)

__all__ = [
    "ActorType",
    "DerivedActorKind",
    "TRANSPORT_CAPABILITIES",
    "TransportCapability",
]
