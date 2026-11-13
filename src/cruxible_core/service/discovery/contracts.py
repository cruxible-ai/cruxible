"""One keyset page of accepted Contract versions, read from the bound projection's index.

Rows are read name then version through ``contracts_by_name_version``, a
covering seek after the page's last key: never a scan or an in-memory sort
of every accepted Contract.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

#: The first page: ordered by (name, version), no lower bound.
CONTRACTS_FIRST_PAGE_SQL = (
    "SELECT identity, contract_name, contract_version FROM contracts "
    "INDEXED BY contracts_by_name_version "
    "ORDER BY contract_name, contract_version LIMIT ?"
)
#: A later page: a seek past the last (name, version) key, on the same index.
CONTRACTS_PAGE_SQL = (
    "SELECT identity, contract_name, contract_version FROM contracts "
    "INDEXED BY contracts_by_name_version "
    "WHERE (contract_name, contract_version) > (?, ?) "
    "ORDER BY contract_name, contract_version LIMIT ?"
)


@dataclass(frozen=True)
class ContractKey:
    """Where a page stopped: the last row's (name, version)."""

    name: str
    version: int

    def encode(self) -> list[str]:
        return [self.name, str(self.version)]

    @classmethod
    def decode(cls, last_key: tuple[str, ...]) -> ContractKey | None:
        if len(last_key) != 2 or not last_key[1].isdigit():
            return None
        return cls(last_key[0], int(last_key[1]))


def contracts_page(
    connection: sqlite3.Connection,
    *,
    limit: int,
    after: ContractKey | None,
) -> tuple[tuple[tuple[str, str, int], ...], ContractKey | None]:
    """Up to ``limit`` ``(identity, name, version)`` rows after ``after``, and the next key."""

    want = limit + 1
    rows = list(
        connection.execute(CONTRACTS_FIRST_PAGE_SQL, (want,))
        if after is None
        else connection.execute(CONTRACTS_PAGE_SQL, (after.name, after.version, want))
    )
    stop: ContractKey | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        stop = ContractKey(str(rows[-1][1]), int(rows[-1][2]))
    return (
        tuple((str(identity), str(name), int(version)) for identity, name, version in rows),
        stop,
    )


__all__ = ["ContractKey", "contracts_page"]
