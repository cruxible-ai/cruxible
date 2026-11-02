"""Version and retention for the disposable, candidate-local review projection."""

import sqlite3
from contextlib import closing
from pathlib import Path

REVIEW_PROJECTION_VERSION = 2
REVIEW_PROJECTION_FILENAME = "proposal-review.sqlite3"
REVIEW_TABLES = (
    "review_builds",
    "review_groups",
    "review_rows",
    "review_kinds",
    "review_findings",
)


def initialize_review_projection(connection: sqlite3.Connection, schema: str) -> None:
    """A format change is an explicit cold rebuild, never stale cached rows."""
    if connection.execute("PRAGMA user_version").fetchone()[0] != REVIEW_PROJECTION_VERSION:
        # Schema migration is serialized with other cold builders.
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != REVIEW_PROJECTION_VERSION:
                for table in REVIEW_TABLES:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.execute(f"PRAGMA user_version={REVIEW_PROJECTION_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    connection.executescript(schema)


def prune_review_projection(path: Path, candidate_digest: str) -> None:
    """Delete only the candidate being retired; never scan proposal inventory."""
    if not path.exists():
        return
    with closing(sqlite3.connect(path, timeout=60)) as connection, connection:
        for table in REVIEW_TABLES:
            connection.execute(
                f"DELETE FROM {table} WHERE candidate=? OR (candidate>=? AND candidate<?)",
                (candidate_digest, candidate_digest + ";", candidate_digest + "<"),
            )


def cached_review_candidates(path: Path) -> tuple[str, ...]:
    """Inventory only during the daemon's explicit cold retention sweep."""
    if not path.exists():
        return ()
    with closing(sqlite3.connect(path, timeout=60)) as connection:
        return tuple(
            row[0].split(";", 1)[0]
            for row in connection.execute("SELECT candidate FROM review_builds")
        )
