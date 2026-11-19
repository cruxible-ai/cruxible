"""One keyset page of the pins between live definitions, read from the bound projection.

Every accepted artifact's required pins sit in ``pins``, keyed by
``(source_identity, edge_kind, ordinal)``. Claims pin every contract version
their evidence used (and Subject shells and Documents carry pins of their
own), so the table grows with the instance; a definition's pins are a small,
ontology-sized part of it. A page
walks the primary key one definition kind at a time, over the identity range
``<Kind>:`` .. ``<Kind>;`` (``;`` sorts right after ``:``), so it never reads
a Claim's, a Subject's or an attestation's row. The reverse question -- who
pins this contract, ClaimType or Procedure -- is the same rows read by target.
``pins_by_target`` does not serve it: its leading column is the target, under
which every Claim citing a contract sits beside the Procedures that capture
under it.

Only live sources are listed (a retired definition pins nothing in force).
``current`` says whether a pin names its target's accepted version: one
primary-key seek of the target's owner table per row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cruxible_client.contracts.orient import ORIENT_PIN_SOURCE_KINDS, OrientPin
from cruxible_core.indexes.typed_state import OWNER_BY_IDENTITY_KIND, owner_for_identity

#: One definition kind's live required pins after a (source, ordinal) key, by source then
#: ordinal. The plan is fixed: a range walk of the ``pins`` primary key (``INDEXED BY``; the
#: partial target-unique index also leads with the source but holds no ordinal), then
#: (``CROSS JOIN``) one primary-key seek of the source's own table for its lifecycle.
PINS_SQL_TEMPLATE = (
    "SELECT p.source_identity, p.ordinal, p.pin_role, p.target_identity, p.target_digest "
    "FROM pins p INDEXED BY sqlite_autoindex_pins_1 "
    "CROSS JOIN {table} s ON s.identity=p.source_identity "
    "WHERE (p.source_identity, p.edge_kind, p.ordinal) > (?, 'required_pin', ?) "
    "AND p.source_identity < ? AND p.edge_kind='required_pin' AND s.lifecycle='live' "
    "ORDER BY p.source_identity, p.ordinal LIMIT ?"
)
#: A target's accepted version: a primary-key seek of its owner table.
TARGET_DIGEST_SQL_TEMPLATE = "SELECT artifact_digest FROM {table} WHERE identity=?"


def pins_sql(kind: str) -> str:
    return PINS_SQL_TEMPLATE.format(table=OWNER_BY_IDENTITY_KIND[kind].table)


@dataclass(frozen=True)
class PinKey:
    """Where a page stopped: the last row's source identity and pin ordinal."""

    source: str
    ordinal: int

    def encode(self) -> list[str]:
        return [self.source, str(self.ordinal)]

    @classmethod
    def decode(cls, last_key: tuple[str, ...]) -> PinKey | None:
        if len(last_key) != 2 or not last_key[1].isdigit():
            return None
        return cls(last_key[0], int(last_key[1]))


def _current(connection: sqlite3.Connection, target: str, digest: str) -> bool | None:
    owner = owner_for_identity(target)
    if owner is None:
        return None
    row = connection.execute(
        TARGET_DIGEST_SQL_TEMPLATE.format(table=owner.table), (target,)
    ).fetchone()
    return row is not None and str(row[0]) == digest


def pins_page(
    connection: sqlite3.Connection,
    *,
    limit: int,
    after: PinKey | None,
) -> tuple[tuple[OrientPin, ...], PinKey | None]:
    """Up to ``limit`` live definition pins after ``after``, and the key to continue."""

    want = limit + 1
    rows: list[tuple[str, int, str, str, str | None]] = []
    for kind in ORIENT_PIN_SOURCE_KINDS:
        prefix, end = f"{kind}:", f"{kind};"
        if after is not None and after.source >= end:
            continue
        resume = after is not None and after.source >= prefix
        start = (after.source, after.ordinal) if after is not None and resume else (prefix, -1)
        rows.extend(
            (
                str(source),
                int(ordinal),
                str(role),
                str(target),
                None if digest is None else str(digest),
            )
            for source, ordinal, role, target, digest in connection.execute(
                pins_sql(kind), (*start, end, want - len(rows))
            )
        )
        if len(rows) >= want:
            break
    stop: PinKey | None = None
    if len(rows) > limit:
        rows.pop()
        stop = PinKey(rows[-1][0], rows[-1][1])
    return (
        tuple(
            OrientPin(
                source=source,
                role=role,
                target=target,
                current=(
                    _current(connection, target, digest)
                    if source.startswith("ProcedureMandate:") and digest is not None
                    else _current_name(connection, target)
                ),
            )
            for source, _ordinal, role, target, digest in rows
        ),
        stop,
    )


__all__ = ["PINS_SQL_TEMPLATE", "TARGET_DIGEST_SQL_TEMPLATE", "PinKey", "pins_page", "pins_sql"]


def _current_name(connection: sqlite3.Connection, identity: str) -> bool | None:
    row = connection.execute(
        "SELECT 1 FROM artifact_lookup WHERE identity=?", (identity,)
    ).fetchone()
    return None if row is None else True
