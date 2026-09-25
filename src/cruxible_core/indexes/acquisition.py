"""Short namespace proofs while SQLite acquires files by pathname."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from cruxible_client.contracts.errors import ProjectionIntegrityError


class DatabasePathChangedError(ProjectionIntegrityError):
    """Acquisition raced with a namespace change; retry without changing source state."""


# An unrelated entry change in any ancestor refuses an attempt exactly as a swap
# does, and a shared system temporary directory changes many times a second. A
# bounded retry with a short growing pause outlasts that churn instead of
# spending every attempt inside one burst; a swap repeated on every attempt still
# exhausts the bound and refuses. The pause is never taken after the last attempt.
ACQUISITION_ATTEMPTS = 8
_ACQUISITION_PAUSE_CAP_SECONDS = 0.05


def pause_before_acquisition_retry(attempt: int) -> None:
    """Wait before retrying after `attempt` (zero-based) raised DatabasePathChangedError."""

    time.sleep(min(0.001 * (2**attempt), _ACQUISITION_PAUSE_CAP_SECONDS))


def _ancestor_stamp(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    lexical = path.absolute().parent
    resolved = lexical.resolve(strict=True)
    parents = {lexical, *lexical.parents, resolved, *resolved.parents}
    result = []
    for parent in sorted(parents):
        value = parent.lstat()
        result.append((str(parent), value.st_dev, value.st_ino, value.st_ctime_ns))
    return tuple(result)


@contextmanager
def guard_database_path(path: Path) -> Iterator[None]:
    """Detect ancestor replacement, including a directory swapped back after open.

    Main-file stamps alone miss an ancestor rename: the file's inode and ctime
    stay unchanged. Renaming the ancestor changes its own ctime. Both lexical
    and resolved ancestors cover symlink spellings as well as their targets.
    Unrelated directory-entry changes can conservatively refuse this short
    acquisition; they do not establish that any source or database row changed.
    """

    try:
        before = _ancestor_stamp(path)
    except OSError as exc:
        raise DatabasePathChangedError("database namespace is unavailable; retry required") from exc
    yield
    try:
        after = _ancestor_stamp(path)
    except OSError as exc:
        raise DatabasePathChangedError(
            "database namespace changed during acquisition; retry required"
        ) from exc
    if after != before:
        raise DatabasePathChangedError(
            "database namespace changed during acquisition; retry required"
        )


def open_working_snapshot(
    path: Path,
    *,
    expected_stamp: tuple[int, ...],
    file_stamp: Callable[[], tuple[int, ...] | None],
) -> sqlite3.Connection:
    """Pin a WAL snapshot only while its verified physical proof remains unchanged."""

    for attempt in range(ACQUISITION_ATTEMPTS):
        connection = None
        try:
            with guard_database_path(path):
                if file_stamp() != expected_stamp:
                    raise ProjectionIntegrityError(
                        "working database changed before snapshot acquisition"
                    )
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN DEFERRED")
                # BEGIN does not acquire the WAL snapshot. Read while the path
                # proof and the exact physical stamp still protect acquisition.
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                if file_stamp() != expected_stamp:
                    raise ProjectionIntegrityError(
                        "working database changed during snapshot acquisition"
                    )
            return connection
        except DatabasePathChangedError:
            if connection is not None:
                connection.close()
            if attempt == ACQUISITION_ATTEMPTS - 1:
                raise
            pause_before_acquisition_retry(attempt)
        except BaseException:
            if connection is not None:
                connection.close()
            raise
    raise AssertionError("snapshot acquisition retry bound is unreachable")
