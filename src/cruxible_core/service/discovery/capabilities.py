"""One keyset page of accepted capability versions, read from the bound projection's index.

Rows are read name then version through ``capabilities_by_name_version``, a
covering seek after the page's last key: never a scan or an in-memory sort of
every accepted capability.
"""

from __future__ import annotations

import sqlite3

from cruxible_core.service.discovery.contracts import ContractKey

#: The first page: ordered by (name, version), no lower bound.
CAPABILITIES_FIRST_PAGE_SQL = (
    "SELECT identity, capability_name, capability_version FROM capabilities "
    "INDEXED BY capabilities_by_name_version "
    "ORDER BY capability_name, capability_version LIMIT ?"
)
#: A later page: a seek past the last (name, version) key, on the same index.
CAPABILITIES_PAGE_SQL = (
    "SELECT identity, capability_name, capability_version FROM capabilities "
    "INDEXED BY capabilities_by_name_version "
    "WHERE (capability_name, capability_version) > (?, ?) "
    "ORDER BY capability_name, capability_version LIMIT ?"
)

#: Where a page stopped: the last row's (name, version), as for Contracts.
CapabilityKey = ContractKey


def capabilities_page(
    connection: sqlite3.Connection,
    *,
    limit: int,
    after: CapabilityKey | None,
) -> tuple[tuple[tuple[str, str, int], ...], CapabilityKey | None]:
    """Up to ``limit`` ``(identity, name, version)`` rows after ``after``, and the next key."""

    want = limit + 1
    rows = list(
        connection.execute(CAPABILITIES_FIRST_PAGE_SQL, (want,))
        if after is None
        else connection.execute(CAPABILITIES_PAGE_SQL, (after.name, after.version, want))
    )
    stop: CapabilityKey | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        stop = CapabilityKey(str(rows[-1][1]), int(rows[-1][2]))
    return (
        tuple((str(identity), str(name), int(version)) for identity, name, version in rows),
        stop,
    )


__all__ = ["CapabilityKey", "capabilities_page"]
