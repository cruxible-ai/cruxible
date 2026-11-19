"""Resolve capability pins and read capabilities by name, from the typed index.

The one place other slices resolve a capability: :func:`capability_lookup_at`
returns the wave 0 :data:`CapabilityLookup` callable over one accepted
coordinate, and :func:`capabilities_by_name` lists one name's versions. Both
seek the ``capabilities`` table (``capabilities_by_name_version`` for a name, the
identity key for a pin) and parse only the rows they return; nothing scans every
capability. A capability staged in an uncommitted change set is not in the
index: resolve it from the candidate tree with ``tree_capability_resolver``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from cruxible_client.contracts.capabilities import (
    Capability,
    CapabilityLookup,
    CapabilityRef,
    capability_identity,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
    from cruxible_core.runtime.instance import PlaybillInstance

#: One name's versions, oldest first, on ``capabilities_by_name_version``.
CAPABILITIES_BY_NAME_SQL = (
    "SELECT identity FROM capabilities INDEXED BY capabilities_by_name_version "
    "WHERE capability_name=? ORDER BY capability_version"
)
#: One name's newest version, on the same index.
NEWEST_CAPABILITY_SQL = (
    "SELECT identity FROM capabilities INDEXED BY capabilities_by_name_version "
    "WHERE capability_name=? ORDER BY capability_version DESC LIMIT 1"
)


def capability_in_projection(projection: Any, ref: CapabilityRef) -> Capability | None:
    """The accepted version ``ref`` names in a bound projection, or ``None``.

    A pin that carries a digest resolves only to the version with that digest
    (compared with the envelope's recorded digest; the bytes are not re-hashed).
    """

    identity = capability_identity(ref.name, ref.version).qualified
    row = projection.typed.envelope(identity)
    if row is None or (ref.digest is not None and row.artifact_digest != ref.digest):
        return None
    return cast(Capability, projection.typed.source(identity))


def capability_lookup_at(
    instance: PlaybillInstance, at: AcceptedProjectionCoordinate
) -> CapabilityLookup:
    """Resolve capability pins against the accepted state at ``at`` (a base or head).

    The returned callable binds the projection at ``at`` for each pin it has not
    resolved before and remembers the answer: one identity seek per distinct pin.
    """

    found: dict[CapabilityRef, Capability | None] = {}

    def resolve(ref: CapabilityRef) -> Capability | None:
        if ref not in found:
            with instance.bind_accepted_projection(at) as projection:
                found[ref] = capability_in_projection(projection, ref)
        return found[ref]

    return resolve


def capabilities_by_name(
    instance: PlaybillInstance, at: AcceptedProjectionCoordinate, name: str
) -> tuple[Capability, ...]:
    """Every accepted version of capability ``name`` at ``at``, oldest first."""

    with instance.bind_accepted_projection(at) as projection:
        return capability_versions_in_projection(projection, name)


def capability_versions_in_projection(projection: Any, name: str) -> tuple[Capability, ...]:
    """One name's accepted versions in a bound projection, oldest first: one index seek."""

    rows = projection.typed.connection.execute(CAPABILITIES_BY_NAME_SQL, (name,)).fetchall()
    return tuple(cast(Capability, projection.typed.source(str(row[0]))) for row in rows)


def newest_capability_identity(projection: Any, name: str) -> str | None:
    """The identity of one name's newest accepted version in a bound projection."""

    row = projection.typed.connection.execute(NEWEST_CAPABILITY_SQL, (name,)).fetchone()
    return None if row is None else str(row[0])


__all__ = [
    "capabilities_by_name",
    "capability_in_projection",
    "capability_lookup_at",
    "capability_versions_in_projection",
    "newest_capability_identity",
]
