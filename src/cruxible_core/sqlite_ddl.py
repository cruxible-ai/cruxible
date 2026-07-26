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

    Statements are accumulated line by line until
    :func:`sqlite3.complete_statement` says one is complete, so a semicolon
    inside an ``--`` comment (or a string literal) never splits a statement in
    half. Chunks containing only comments and whitespace are dropped rather
    than executed. Triggers/BEGIN...END bodies remain out of scope: SQLite
    treats the first ``;`` inside a body as completing the statement, so a
    schema that needs them must not be routed through here.
    """
    statements: list[str] = []
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        candidate = "\n".join(pending)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip().rstrip(";").strip())
            pending = []
    tail = "\n".join(pending).strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if _has_executable_content(statement)]


def _has_executable_content(statement: str) -> bool:
    """Return whether a chunk contains anything beyond comments and ``;``."""
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--") and stripped != ";":
            return True
    return False


def execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Run a DDL script statement-by-statement, preserving the open transaction."""
    for statement in split_schema_statements(script):
        conn.execute(statement)


__all__ = ["execute_schema_script", "split_schema_statements"]
