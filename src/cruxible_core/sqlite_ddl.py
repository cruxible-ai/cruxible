"""Transaction-safe execution of the stores' DDL scripts.

``sqlite3.Connection.executescript`` COMMITS any pending transaction before it
runs. That is fatal for a migration that must be atomic: the storage backend
takes an explicit write lock (``BEGIN IMMEDIATE``) BEFORE initialization so two
processes cannot race a schema upgrade, and an ``executescript`` anywhere inside
that window would silently drop the lock and let a second process observe a
half-upgraded database.

Every store therefore runs its schema through :func:`execute_schema_script`,
which executes the statements one at a time on the caller's connection and so
joins whatever transaction is already open instead of ending it.

This module is a LEAF: it imports nothing from ``cruxible_core`` so the stores
(which the storage backend itself imports) can depend on it without a cycle.
"""

from __future__ import annotations

import sqlite3


def split_schema_statements(script: str) -> list[str]:
    """Split a DDL script into individual statements.

    A plain semicolon split is correct for these scripts and only for these:
    they are CREATE TABLE / CREATE INDEX statements whose string literals never
    contain a semicolon, and there are no triggers or BEGIN...END bodies. A
    schema that needs either must not be routed through here.
    """
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Run a DDL script statement-by-statement, preserving the open transaction."""
    for statement in split_schema_statements(script):
        conn.execute(statement)


__all__ = ["execute_schema_script", "split_schema_statements"]
