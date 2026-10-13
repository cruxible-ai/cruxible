"""Counted work for the scale guard: what one measured call does, never how long it takes.

``counting(root)`` opens a measured window around one call against the instance
whose managed root is ``root`` and fills a ``Counter`` with the work done:

- ``claim_parses``: calls of ``parse_claim``;
- ``canonical_encodes``: calls of ``canonical_bytes`` and ``pretty_canonical_bytes``
  (a digest's own encode included);
- ``canonical_digests``: calls of ``canonical_digest``;
- ``file_opens``: ``open`` audit events for paths under ``root``, plus relative
  paths, which inside a window are only ever opened against a directory
  descriptor the instance (or its workspace) holds;
- ``git_spawns`` and ``other_spawns``: ``subprocess.Popen`` audit events, split
  on the program's name;
- ``sql_statements``: statements SQLite ran on connections to databases under
  ``root`` (one per execution, so ``executemany`` counts every row);
- ``sql_scan_rows``: for each of those statements whose query plan scans a table
  in full, that table's row count when the window closes, summed. A scan of a
  table that does not grow with the instance weighs the same at both sizes; a
  scan of one that does weighs its size. A scan walking an index in order under
  a trailing ``LIMIT`` with no sort step stops early and is not weighed;
- ``projection_binds``: calls of ``PlaybillInstance.bind_accepted_projection``;
- ``cas_body_reads``: CAS bodies read and hashed
  (``ContentAddressedBodyStore._verified_bytes`` and ``.availability``);
- ``git_blob_reads``: ledger blobs read through the resident batch reader
  (``GitLedger.read_blobs``, one per distinct object id requested), which
  ``read_tree`` and every blob read go through; the per-process blob cache in
  front of it absorbs repeats.

Function counters are installed when a window opens and removed when it closes:
every module global naming an original is rebound to a counting wrapper, then
back. The audit hook (opens and spawns) cannot be removed, so it is installed
once and does nothing outside a window. SQLite connections outlive windows (an
instance holds some open), so ``trace_sqlite`` installs a connect wrapper for the
scale module's lifetime; its trace callbacks record nothing outside a window.
Query plans are read afterwards on a separate read-only connection, never from
inside a callback.
"""

from __future__ import annotations

import functools
import importlib
import os
import re
import sqlite3
import sys
import types
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

COUNTERS = (
    "claim_parses",
    "canonical_encodes",
    "canonical_digests",
    "file_opens",
    "git_spawns",
    "other_spawns",
    "sql_statements",
    "sql_scan_rows",
    "projection_binds",
    "cas_body_reads",
    "git_blob_reads",
)

_FUNCTIONS = (
    ("cruxible_client.contracts.claims", "parse_claim", "claim_parses"),
    ("cruxible_client.contracts.canonical", "canonical_bytes", "canonical_encodes"),
    ("cruxible_client.contracts.canonical", "pretty_canonical_bytes", "canonical_encodes"),
    ("cruxible_client.contracts.canonical", "canonical_digest", "canonical_digests"),
)
_METHODS = (
    ("cruxible_core.runtime.instance", "PlaybillInstance", "bind_accepted_projection"),
    ("cruxible_core.storage.cas", "ContentAddressedBodyStore", "_verified_bytes"),
    ("cruxible_core.storage.cas", "ContentAddressedBodyStore", "availability"),
    ("cruxible_core.ledger.git", "GitLedger", "read_blobs"),
)
_METHOD_COUNTERS = {
    "bind_accepted_projection": "projection_binds",
    "_verified_bytes": "cas_body_reads",
    "availability": "cas_body_reads",
    "read_blobs": "git_blob_reads",
}


@dataclass
class _Window:
    prefixes: tuple[str, ...]
    counts: Counter[str] = field(default_factory=Counter)
    statements: list[tuple[str, str]] = field(default_factory=list)


_WINDOW: _Window | None = None


def _prefixes(root: Path) -> tuple[str, ...]:
    return tuple(sorted({os.path.normpath(str(root)), os.path.realpath(root)}))


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + os.sep) for prefix in prefixes)


# -- opens and spawns: one audit hook for the process ---------------------------------

_AUDIT_INSTALLED = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    window = _WINDOW
    if window is None:
        return
    if event == "open":
        target = args[0]
        if isinstance(target, int):
            return
        try:
            name = os.fsdecode(target)
        except TypeError:
            return
        if not os.path.isabs(name) or _under(os.path.normpath(name), window.prefixes):
            window.counts["file_opens"] += 1
    elif event == "subprocess.Popen":
        executable, argv = args[0], args[1]
        if isinstance(argv, (str, bytes)):
            program = os.fsdecode(argv).split()[0]
        elif argv:
            program = os.fsdecode(argv[0])
        else:
            program = os.fsdecode(executable or "")
        name = os.path.basename(program)
        window.counts["git_spawns" if name == "git" else "other_spawns"] += 1


def _install_audit_hook() -> None:
    global _AUDIT_INSTALLED
    if not _AUDIT_INSTALLED:
        sys.addaudithook(_audit)
        _AUDIT_INSTALLED = True


# -- function and method counters: rebound per window ---------------------------------


class _Rebinder:
    """Every module global that names a counted function, rebound on demand."""

    def __init__(self) -> None:
        self.originals: dict[int, Callable[..., Any]] = {}
        self.wrappers: dict[int, Callable[..., Any]] = {}
        self.sites: list[tuple[dict[str, Any], str, Callable[..., Any]]] = []
        self.seen: set[str] = set()
        self.methods: list[tuple[type, str, Any, Callable[..., Any]]] = []
        for module_name, name, counter in _FUNCTIONS:
            original = getattr(importlib.import_module(module_name), name)
            self.originals[id(original)] = original
            self.wrappers[id(original)] = _counting(original, counter)
        for module_name, class_name, name in _METHODS:
            owner = getattr(importlib.import_module(module_name), class_name)
            raw = owner.__dict__[name]
            wrapper = (
                _counting_blobs(raw)
                if name == "read_blobs"
                else _counting(raw, _METHOD_COUNTERS[name])
            )
            self.methods.append((owner, name, raw, wrapper))

    def _index_new_modules(self, *, wrapped: bool) -> None:
        by_id = (
            {id(wrapper): self.originals[ident] for ident, wrapper in self.wrappers.items()}
            if wrapped
            else self.originals
        )
        for module_name in list(sys.modules):
            if module_name in self.seen:
                continue
            self.seen.add(module_name)
            module = sys.modules.get(module_name)
            if not isinstance(module, types.ModuleType):
                continue
            namespace = module.__dict__
            for key, value in list(namespace.items()):
                original = by_id.get(id(value))
                if original is not None and (value is original or wrapped):
                    self.sites.append((namespace, key, original))
                    if wrapped:
                        namespace[key] = original

    def install(self) -> None:
        self._index_new_modules(wrapped=False)
        for namespace, key, original in self.sites:
            namespace[key] = self.wrappers[id(original)]
        for owner, name, _raw, wrapper in self.methods:
            setattr(owner, name, wrapper)

    def remove(self) -> None:
        for owner, name, raw, _wrapper in self.methods:
            setattr(owner, name, raw)
        for namespace, key, original in self.sites:
            namespace[key] = original
        # A module first imported inside the window bound a wrapper by name.
        self._index_new_modules(wrapped=True)


def _counting(original: Callable[..., Any], counter: str) -> Callable[..., Any]:
    @functools.wraps(original)
    def counted(*args: Any, **kwargs: Any) -> Any:
        window = _WINDOW
        if window is not None:
            window.counts[counter] += 1
        return original(*args, **kwargs)

    return counted


def _counting_blobs(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def counted(self: Any, oids: Any) -> Any:
        oids = tuple(oids)
        window = _WINDOW
        if window is not None:
            window.counts["git_blob_reads"] += len(dict.fromkeys(oids))
        return original(self, oids)

    return counted


_REBINDER: _Rebinder | None = None


# -- SQLite: traced for the module's lifetime, recorded only inside a window ----------

_ORIGINAL_CONNECT = sqlite3.connect


def _database_path(database: object, *, uri: bool) -> str | None:
    if database is None:
        return None
    try:
        name = os.fsdecode(database)  # type: ignore[arg-type]
    except TypeError:
        return None
    if name in ("", ":memory:"):
        return None
    if uri or name.startswith("file:"):
        parsed = urlparse(name)
        name = unquote(parsed.path)
        if not name or name == ":memory:":
            return None
    return os.path.normpath(os.path.abspath(name))


def _record(database: str, sql: str) -> None:
    window = _WINDOW
    if window is not None and _under(database, window.prefixes):
        window.statements.append((database, sql))


@contextmanager
def trace_sqlite(root: Path) -> Iterator[None]:
    """Trace every SQLite connection opened under ``root`` until the block exits."""

    prefixes = _prefixes(root)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = _ORIGINAL_CONNECT(*args, **kwargs)
        database = _database_path(
            args[0] if args else kwargs.get("database"), uri=bool(kwargs.get("uri"))
        )
        if database is not None and _under(database, prefixes):
            connection.set_trace_callback(functools.partial(_record, database))
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        yield
    finally:
        sqlite3.connect = _ORIGINAL_CONNECT


_LITERALS = re.compile(r"[xX]'[0-9a-fA-F]*'|'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")
_NOT_PLANNED = re.compile(
    r"^\s*(?:--|BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|PRAGMA|CREATE|DROP|ALTER|"
    r"ATTACH|DETACH|ANALYZE|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)
_TRAILING_LIMIT = re.compile(r"\bLIMIT\s+\S+(?:\s+OFFSET\s+\S+)?\s*;?\s*$", re.IGNORECASE)
_PLANS: dict[tuple[str, str], tuple[str, ...] | None] = {}


def _full_scans(connection: sqlite3.Connection, sql: str) -> tuple[str, ...] | None:
    try:
        plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.Error:
        return None
    details = [str(row[3]) for row in plan]
    sorts = any("USE TEMP B-TREE" in detail for detail in details)
    limited = _TRAILING_LIMIT.search(sql) is not None
    scanned = []
    for detail in details:
        if not detail.startswith("SCAN ") or "VIRTUAL TABLE" in detail:
            continue
        table = detail.split(" ")[1]
        if table not in tables:
            continue
        ordered_walk = (
            "USING INDEX" in detail
            or "USING COVERING INDEX" in detail
            or ("USING INTEGER PRIMARY KEY" in detail)
        )
        if limited and not sorts and ordered_walk:
            continue
        scanned.append(table)
    return tuple(scanned)


def _scan_rows(statements: list[tuple[str, str]], unexplained: Counter[str]) -> int:
    connections: dict[str, sqlite3.Connection | None] = {}
    rows: dict[tuple[str, str], int] = {}

    def opened(database: str) -> sqlite3.Connection | None:
        if database not in connections:
            try:
                connection = _ORIGINAL_CONNECT(Path(database).as_uri() + "?mode=ro", uri=True)
                connection.execute("PRAGMA query_only=ON")
            except sqlite3.Error:
                # A build file renamed or removed since: nothing left to plan against.
                connection = None
            connections[database] = connection
        return connections[database]

    total = 0
    try:
        for database, sql in statements:
            if _NOT_PLANNED.match(sql):
                continue
            key = (database, _LITERALS.sub("?", sql))
            if key not in _PLANS:
                connection = opened(database)
                if connection is None:
                    unexplained[key[1][:160]] += 1
                    continue
                _PLANS[key] = _full_scans(connection, sql)
            scanned = _PLANS[key]
            if scanned is None:
                unexplained[key[1][:160]] += 1
                continue
            for table in scanned:
                if (database, table) not in rows:
                    connection = opened(database)
                    rows[(database, table)] = (
                        0
                        if connection is None
                        else int(
                            connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                        )
                    )
                total += rows[(database, table)]
    finally:
        for connection in connections.values():
            if connection is not None:
                connection.close()
    return total


#: Statements whose plan could not be read (another connection's temporary table,
#: an attached schema), by literal-free text; diagnostics only.
UNEXPLAINED: Counter[str] = Counter()


@contextmanager
def counting(root: Path) -> Iterator[Counter[str]]:
    """Count the work done against the instance under ``root`` until the block exits."""

    global _WINDOW, _REBINDER
    assert _WINDOW is None, "measured windows do not nest"
    _install_audit_hook()
    if _REBINDER is None:
        _REBINDER = _Rebinder()
    window = _Window(prefixes=_prefixes(root))
    _REBINDER.install()
    _WINDOW = window
    try:
        yield window.counts
    finally:
        _WINDOW = None
        _REBINDER.remove()
        window.counts["sql_statements"] = len(window.statements)
        window.counts["sql_scan_rows"] = _scan_rows(window.statements, UNEXPLAINED)
        for name in COUNTERS:
            window.counts.setdefault(name, 0)
