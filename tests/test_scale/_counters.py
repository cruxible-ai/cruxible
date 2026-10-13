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
  ``root`` (one per execution, so ``executemany`` counts every row, and each
  trigger firing counts once);
- ``sql_scan_rows``: for each of those executions, the rows of every table its
  plan walks in full, as the table stands when the statement starts, summed (see
  "SQL scans" below). A table that does not grow with the instance weighs the
  same at both sizes; one that does weighs its size;
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

SQL scans. The window over-counts rather than miss. Every execution is planned
when it starts, afresh, with its own SQL, literals included (a literal can change
the plan: a partial index serves ``other=1``, not ``other=2``), from its trace
callback on its own connection (``EXPLAIN QUERY PLAN``; the callback ignores its
own statements). So the plan sees that connection's temporary tables, attached
schemas and uncommitted schema, and a database renamed or deleted before the
window closes is planned and sized while it still exists. Only the schema's
object list and trigger bodies are reused, keyed by every schema's version.

A plan step walks a table in full when it is a ``SCAN`` of it (a covering index
walk included), or builds an automatic index over it. Its target is resolved to
the real table: an alias (``FROM claims AS c`` plans as ``SCAN c``) through the
statement's own references, ``schema.table`` through that schema, and CTEs,
views and subqueries are skipped (their own scans are separate plan steps).
Foreign-key checks are plan steps too, weighed each execution though SQLite runs
them only per changed row. ``DELETE`` without ``WHERE``, ``CREATE INDEX`` and
``DROP TABLE`` walk their table. The schema table (``sqlite_master``) resolves
and weighs nothing: its rows are the schema, which no instance grows.

Writes (``write_scan_rows``). Python traces each trigger firing as a repeat of
the outer statement, so every trace of a write keeps its own plan's weight and
adds, for a write that names its target first, every statement of every trigger
on that table (and of the triggers those reach), whether or not one fires; and
for a write whose target or shape it does not establish (a ``WITH`` prefix, or
any other form), every table the statement names, in full, plus every trigger
on each. Trigger bodies read ``NEW.x``, ``OLD.x`` and ``RAISE(...)`` as NULL.

A ``LIMIT``ed walk counts only its ``LIMIT + OFFSET`` rows, and only for a
proved row-preserving query: a single-step plan of ``SELECT <columns> FROM <one
table> LIMIT n [OFFSET m]`` (``_row_preserving``); every other shape weighs the
full table. Row counts are taken afresh at every scanning statement, never
cached. Anything the window cannot resolve (a plan that fails, a target it
cannot name, a statement kind it does not model) is recorded in
``unexplained``, which the guard treats as a failure, never as zero.
"""

from __future__ import annotations

import functools
import importlib
import os
import re
import sqlite3
import sys
import types
import weakref
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
class Window:
    prefixes: tuple[str, ...]
    counts: Counter[str] = field(default_factory=Counter)
    #: Statements whose scans could not be resolved, with why; never counted as zero.
    unexplained: list[str] = field(default_factory=list)


_WINDOW: Window | None = None


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


# -- SQLite: traced for the module's lifetime, planned inside a window -----------------

_ORIGINAL_CONNECT = sqlite3.connect
_PLANNING = False


class TracedConnection(sqlite3.Connection):
    """A connection the trace callback can plan on: it holds a weak reference to it."""

    scale_cache: dict[Any, Any]


class Unexplained(Exception):
    """A statement whose full-scan work this window cannot establish."""


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


def _on_statement(reference: Any, database: str, sql: str) -> None:
    global _PLANNING
    window = _WINDOW
    if window is None or _PLANNING or not _under(database, window.prefixes):
        return
    window.counts["sql_statements"] += 1
    connection = reference()
    if connection is None:
        window.unexplained.append(f"connection gone: {sql[:160]}")
        return
    _PLANNING = True
    try:
        window.counts["sql_scan_rows"] += statement_scan_rows(connection, sql)
        window.counts["sql_scan_rows"] += write_scan_rows(connection, sql)
    except (Unexplained, sqlite3.Error) as exc:
        window.unexplained.append(f"{type(exc).__name__}: {exc} :: {sql[:160]}")
    finally:
        _PLANNING = False


@contextmanager
def trace_sqlite(root: Path) -> Iterator[None]:
    """Trace every SQLite connection opened under ``root`` until the block exits."""

    prefixes = _prefixes(root)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = _database_path(
            args[0] if args else kwargs.get("database"), uri=bool(kwargs.get("uri"))
        )
        if database is None or not _under(database, prefixes):
            return _ORIGINAL_CONNECT(*args, **kwargs)
        if len(args) > 5 or "factory" in kwargs:
            raise AssertionError("the scale guard cannot trace a custom connection factory")
        connection = _ORIGINAL_CONNECT(*args, factory=TracedConnection, **kwargs)
        assert isinstance(connection, TracedConnection)
        connection.scale_cache = {}
        connection.set_trace_callback(
            functools.partial(_on_statement, weakref.ref(connection), database)
        )
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        yield
    finally:
        sqlite3.connect = _ORIGINAL_CONNECT


_LITERALS = re.compile(r"[xX]'[0-9a-fA-F]*'|'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_NO_SCAN = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA", "ATTACH", "DETACH"}
)
_PLANNED = frozenset({"SELECT", "WITH", "INSERT", "REPLACE", "UPDATE", "DELETE", "VALUES"})
_IDENT = r'(?:"(?:[^"]|"")+"|\[[^\]]+\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)'
_REFERENCE = re.compile(
    rf"(?P<ref>{_IDENT}(?:\s*\.\s*{_IDENT})?)\s+(?:AS\s+)?(?P<alias>{_IDENT})", re.IGNORECASE
)
_SUBQUERY_ALIAS = re.compile(rf"\)\s+(?:AS\s+)?(?P<alias>{_IDENT})", re.IGNORECASE)
_CTE = re.compile(
    rf"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*(?P<name>{_IDENT})\s*(?:\([^)]*\)\s*)?AS\s+"
    r"(?:NOT\s+)?(?:MATERIALIZED\s+)?\(",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?\s*;?\s*$", re.IGNORECASE)
#: Anything that can make a LIMITed query read more rows than it returns.
_UNBOUNDED = re.compile(
    r"\b(?:WHERE|DISTINCT|ALL|GROUP|HAVING|ORDER|WINDOW|OVER|UNION|INTERSECT|EXCEPT|JOIN|"
    r"FILTER|WITH|VALUES)\b|[(,]",
    re.IGNORECASE,
)
_TRIGGER_ROW = re.compile(r"\b(?:NEW|OLD)\s*\.\s*" + _IDENT, re.IGNORECASE)
#: RAISE(...) plans only inside a trigger program; it reads nothing, so it reads as NULL.
_RAISE = re.compile(r"\bRAISE\s*\(\s*\w+\s*(?:,\s*'(?:[^']|'')*'\s*)?\)", re.IGNORECASE)
_CREATE_INDEX = re.compile(
    rf"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}(?:\s*\.\s*{_IDENT})?"
    rf"\s+ON\s+(?P<table>{_IDENT})",
    re.IGNORECASE,
)
_CREATE_AS = re.compile(
    r"^CREATE\s+(?:TEMP\w*\s+)?TABLE\b.*?\bAS\s+(?P<select>SELECT\b.*)$", re.IGNORECASE | re.DOTALL
)
_DROP_TABLE = re.compile(
    rf"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)",
    re.IGNORECASE,
)
_DELETE_ALL = re.compile(
    rf"^DELETE\s+FROM\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)\s*;?\s*$", re.IGNORECASE
)
#: Words that can follow a table reference without being its alias.
_KEYWORDS = frozenset(
    """ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE
    BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE
    CROSS CURRENT DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH ELSE END
    ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM
    FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER
    INSERT INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED
    NATURAL NO NOT NOTHING NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION
    PLAN PRAGMA PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE
    RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET TABLE TEMP
    TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE UPDATE USING VACUUM
    VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT""".split()
)


def _bare(identifier: str) -> str:
    identifier = identifier.strip()
    if identifier[:1] in ('"', "[", "`"):
        identifier = identifier[1:-1].replace('""', '"')
    return identifier


def _name(reference: str) -> tuple[str | None, str]:
    """``schema.table`` or ``table``, unquoted."""

    parts = re.split(rf"\s*\.\s*(?={_IDENT}$)", reference.strip(), maxsplit=1)
    if len(parts) == 2:
        return _bare(parts[0]), _bare(parts[1])
    return None, _bare(reference)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _cached(connection: TracedConnection, key: Any, compute: Callable[[], Any]) -> Any:
    cache = connection.scale_cache
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _schema_version(connection: TracedConnection) -> tuple[tuple[str, int], ...]:
    """Every schema's version: a cached plan or object list is good while these hold."""

    return tuple(
        (schema, int(connection.execute(f"PRAGMA {_quote(schema)}.schema_version").fetchone()[0]))
        for schema in _schemas(connection)
    )


def _schemas(connection: TracedConnection) -> tuple[str, ...]:
    """Schemas in SQLite's lookup order for an unqualified name: temp, main, attached."""

    names = [str(row[1]) for row in connection.execute("PRAGMA database_list")]
    return tuple(sorted(names, key=lambda name: (name != "temp", name != "main")))


def _objects(connection: TracedConnection, version: Any) -> dict[str, dict[str, str]]:
    """``schema -> {name: type}`` of every table, view and trigger."""

    def read() -> dict[str, dict[str, str]]:
        return {
            schema: {
                str(name): str(kind)
                for kind, name in connection.execute(
                    f"SELECT type, name FROM {_quote(schema)}.sqlite_master "
                    "WHERE type IN ('table','view','trigger')"
                )
            }
            for schema in _schemas(connection)
        }

    return _cached(connection, ("objects", version), read)  # type: ignore[no-any-return]


_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)


def _table(
    objects: dict[str, dict[str, str]], schema: str | None, name: str
) -> tuple[str, str] | None:
    if name.lower() in _SCHEMA_TABLES:
        # The schema table itself: its rows are the schema's objects.
        temporary = "temp" in name.lower()
        return ("temp" if temporary else (schema or "main")), "sqlite_master"
    for candidate in (schema,) if schema is not None else tuple(objects):
        if objects.get(candidate, {}).get(name) == "table":
            return candidate, name
    return None


def _non_base(objects: dict[str, dict[str, str]], schema: str | None, name: str) -> bool:
    return any(
        objects.get(candidate, {}).get(name) == "view"
        for candidate in ((schema,) if schema is not None else tuple(objects))
    )


_TOKEN = re.compile(rf"{_IDENT}|\S")


def _scope(
    sql: str, objects: dict[str, dict[str, str]]
) -> tuple[dict[str, set[tuple[str, str]]], set[str]]:
    """The statement's aliases of real tables, and the names that are no base table.

    A reference is ``[schema.]name [AS] alias`` wherever it stands (a name that
    is no table, view or CTE never yields an alias); ``) [AS] alias`` names a
    subquery; ``WITH name AS (`` and ``, name AS (`` name CTEs.
    """

    text = _COMMENTS.sub(" ", _LITERALS.sub("0", sql))
    non_base = {_bare(match.group("name")) for match in _CTE.finditer(text)}
    tokens = _TOKEN.findall(text)

    def identifier(index: int) -> str | None:
        if index >= len(tokens):
            return None
        token = tokens[index]
        if not re.fullmatch(_IDENT, token) or token.upper() in _KEYWORDS:
            return None
        return _bare(token)

    def alias_at(index: int) -> str | None:
        if index < len(tokens) and tokens[index].upper() == "AS":
            index += 1
        return identifier(index)

    aliases: dict[str, set[tuple[str, str]]] = {}
    for index, token in enumerate(tokens):
        if token == ")":
            alias = alias_at(index + 1)
            if alias is not None:
                non_base.add(alias)
            continue
        if identifier(index) is None or (index > 0 and tokens[index - 1] == "."):
            continue
        schema, name, after = None, _bare(token), index + 1
        if after + 1 < len(tokens) and tokens[after] == "." and identifier(after + 1) is not None:
            schema, name, after = name, _bare(tokens[after + 1]), after + 2
        alias = alias_at(after)
        if alias is None:
            continue
        cte = schema is None and name in non_base
        table = None if cte else _table(objects, schema, name)
        if table is not None:
            aliases.setdefault(alias, set()).add(table)
        elif cte or _non_base(objects, schema, name):
            non_base.add(alias)
    return aliases, non_base


def _row_preserving(sql: str) -> bool:
    """Whether a LIMIT provably bounds the rows a statement reads.

    Only ``SELECT <column, ...> FROM <one table> LIMIT n [OFFSET m]``: plain
    column names (no call, so no aggregate; no subquery), and no WHERE,
    DISTINCT, GROUP BY, HAVING, ORDER BY, window or compound. Every other shape
    is read in full, whatever its plan looks like.
    """

    text = _COMMENTS.sub(" ", _LITERALS.sub("0", sql)).strip()
    limit = _LIMIT.search(text)
    if limit is None or re.match(r"SELECT\b", text, re.IGNORECASE) is None:
        return False
    parts = re.split(r"\bFROM\b", text[len("SELECT") : limit.start()], flags=re.IGNORECASE)
    if len(parts) != 2:
        return False
    columns, source = parts
    if _UNBOUNDED.search(source) is not None:
        return False
    return (
        re.search(r"\b(?:DISTINCT|ALL|OVER|FILTER|SELECT|CASE)\b|\(", columns, re.IGNORECASE)
        is None
    )


def _scan_targets(
    connection: TracedConnection, sql: str, version: Any
) -> tuple[tuple[tuple[str, str], bool], ...]:
    """Each table a statement's plan walks in full, and whether the walk is bounded.

    Planned afresh for every execution, with its own SQL, literals included: a
    literal can change the plan on an unchanged schema (a partial index serves
    ``other=1`` and not ``other=2``), so no plan is ever reused.
    """

    def plan() -> tuple[tuple[tuple[str, str], bool], ...]:
        rows = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        details = [str(row[3]) for row in rows]
        objects = _objects(connection, version)
        aliases, non_base = _scope(sql, objects)
        bounded = len(details) == 1 and _row_preserving(sql)
        targets: list[tuple[tuple[str, str], bool]] = []
        for detail in details:
            if detail.startswith("SCAN "):
                target = detail[len("SCAN ") :]
            elif detail.startswith("SEARCH ") and " USING AUTOMATIC " in detail:
                # Building the automatic index walks the whole table.
                target = detail[len("SEARCH ") :]
            else:
                continue
            if target == "CONSTANT ROW" or " VIRTUAL TABLE" in target or target.startswith("("):
                continue
            target = target.split(" USING ", 1)[0]
            schema, name = _name(target)
            if schema is None and name in aliases:
                found = aliases[name]
                if len(found) != 1:
                    raise Unexplained(f"alias {name!r} names {sorted(found)}")
                targets.append((next(iter(found)), bounded))
                continue
            if schema is None and name in non_base:
                continue
            table = _table(objects, schema, name)
            if table is not None:
                targets.append((table, bounded))
            elif not _non_base(objects, schema, name):
                raise Unexplained(f"plan step {detail!r} names no table, alias, view or CTE")
        return tuple(targets)

    return plan()


def _rows(connection: TracedConnection, table: tuple[str, str]) -> int:
    """The table's rows now, counted afresh every time: no cache can go stale."""

    if table[1] == "sqlite_master":
        # The schema table's rows are the database's schema, which no instance
        # grows; weighing them would only dilute the growth the rule compares.
        return 0
    schema, name = table
    (rows,) = connection.execute(f"SELECT count(*) FROM {_quote(schema)}.{_quote(name)}").fetchone()
    return int(rows)


def _body(name: str, sql: str) -> tuple[str, ...]:
    body = re.search(r"\bBEGIN\b(.*)\bEND\s*;?\s*$", sql, re.IGNORECASE | re.DOTALL)
    if body is None:
        raise Unexplained(f"trigger {name!r} has no BEGIN ... END body")
    return tuple(
        _RAISE.sub("NULL", _TRIGGER_ROW.sub("NULL", statement)).strip()
        for statement in _split_statements(body.group(1))
        if statement.strip()
    )


def _triggers(
    connection: TracedConnection, version: Any, *, name: str | None = None, table: str | None = None
) -> tuple[str, ...]:
    """The body statements of one named trigger, or of every trigger on one table."""

    def read() -> tuple[str, ...]:
        column, value = ("name", name) if name is not None else ("tbl_name", table)
        found = [
            (str(row[0]), str(row[1]))
            for schema in _schemas(connection)
            for row in connection.execute(
                f"SELECT name, sql FROM {_quote(schema)}.sqlite_master "
                f"WHERE type='trigger' AND {column}=?",
                (value,),
            )
        ]
        if name is not None and not found:
            raise Unexplained(f"trigger {name!r} is not in any schema")
        return tuple(statement for trigger, sql in found for statement in _body(trigger, sql))

    return _cached(connection, ("triggers", version, name, table), read)  # type: ignore[no-any-return]


_DML_TARGET = re.compile(
    rf"^(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
    rf"\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)",
    re.IGNORECASE,
)


_WRITES = frozenset({"INSERT", "REPLACE", "UPDATE", "DELETE"})


def write_scan_rows(connection: TracedConnection, sql: str) -> int:
    """What a write may walk beyond its own plan: its triggers, or everything it names.

    Python traces each fired trigger program, and each statement in it, as a
    repeat of the outer statement's expanded SQL, so a trace cannot tell an
    execution from a trigger firing. Every trace of a write therefore weighs,
    on top of its own plan (``_on_statement``):

    - for ``INSERT``/``REPLACE``/``UPDATE``/``DELETE`` that names its target
      first, every statement of every trigger on that table, whether or not
      one fires, and of the triggers those statements reach in turn;
    - for a write whose target and shape it does not establish (a ``WITH``
      prefix, or any other form), every table the statement names, in full,
      plus every trigger on each of them.

    An over-count, never a miss.
    """

    return _write_scan_rows(connection, sql, frozenset())


def _named_tables(sql: str, objects: dict[str, dict[str, str]]) -> set[tuple[str, str]]:
    """Every real table a statement names anywhere, CTE-shadowed or not."""

    tokens = _TOKEN.findall(_COMMENTS.sub(" ", _LITERALS.sub("0", sql)))
    named: set[tuple[str, str]] = set()
    for index, token in enumerate(tokens):
        if not re.fullmatch(_IDENT, token) or (index > 0 and tokens[index - 1] == "."):
            continue
        schema, name = None, _bare(token)
        if (
            index + 2 < len(tokens)
            and tokens[index + 1] == "."
            and re.fullmatch(_IDENT, tokens[index + 2])
        ):
            schema, name = name, _bare(tokens[index + 2])
        table = _table(objects, schema, name)
        if table is not None and table[1] != "sqlite_master":
            named.add(table)
    return named


def _write_scan_rows(connection: TracedConnection, sql: str, seen: frozenset[str]) -> int:
    text = sql.strip()
    first = re.match(r"[A-Za-z]+", text)
    keyword = first.group(0).upper() if first is not None else ""
    version = _schema_version(connection)
    target = _DML_TARGET.match(text)
    if target is not None:
        tables = {_name(target.group("table"))[1]}
        total = 0
    elif keyword in _WRITES or keyword == "WITH":
        named = _named_tables(text, _objects(connection, version))
        tables = {name for _schema, name in named}
        total = sum(_rows(connection, table) for table in sorted(named))
    else:
        return 0
    for table in sorted(tables - seen):
        for body in _triggers(connection, version, table=table):
            total += statement_scan_rows(connection, body)
            total += _write_scan_rows(connection, body, seen | {table})
    return total


def _split_statements(text: str) -> list[str]:
    statements, current, quote = [], [], None
    for character in text:
        if quote is not None:
            if character == quote:
                quote = None
        elif character in ("'", '"'):
            quote = character
        elif character == ";":
            statements.append("".join(current))
            current = []
            continue
        current.append(character)
    statements.append("".join(current))
    return statements


def statement_scan_rows(connection: TracedConnection, sql: str) -> int:
    """Rows of every table one statement walks in full, on its own connection, now."""

    version = _schema_version(connection)
    text = sql.strip()
    if text.startswith("--"):
        name = text[2:].strip()
        if not name.upper().startswith("TRIGGER "):
            raise Unexplained("a traced comment that is no trigger firing")
        return sum(
            statement_scan_rows(connection, body)
            for body in _triggers(connection, version, name=_bare(name[len("TRIGGER ") :]))
        )
    first = re.match(r"[A-Za-z]+", text)
    keyword = first.group(0).upper() if first is not None else ""
    if keyword in _NO_SCAN:
        return 0
    objects = _objects(connection, version)
    if keyword == "CREATE":
        index = _CREATE_INDEX.match(text)
        if index is not None:
            table = _table(objects, None, _bare(index.group("table")))
            return 0 if table is None else _rows(connection, table)
        selected = _CREATE_AS.match(text)
        if selected is not None:
            return statement_scan_rows(connection, selected.group("select"))
        return 0
    if keyword == "DROP":
        dropped = _DROP_TABLE.match(text)
        if dropped is None:
            return 0
        table = _table(objects, *_name(dropped.group("table")))
        return 0 if table is None else _rows(connection, table)
    if keyword == "DELETE":
        everything = _DELETE_ALL.match(text)
        if everything is not None:
            table = _table(objects, *_name(everything.group("table")))
            if table is None:
                raise Unexplained(f"DELETE names no table: {everything.group('table')}")
            return _rows(connection, table)
    if keyword == "ALTER":
        altered = re.match(
            rf"^ALTER\s+TABLE\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)\s+DROP\b",
            text,
            re.IGNORECASE,
        )
        if altered is None:
            return 0  # renames and added columns change the schema only
        table = _table(objects, *_name(altered.group("table")))
        return 0 if table is None else _rows(connection, table)
    if keyword not in _PLANNED:
        raise Unexplained(f"statement kind {keyword!r} is not modelled")
    total = 0
    for table, bounded in _scan_targets(connection, text, version):
        rows = _rows(connection, table)
        if bounded:
            limit = _LIMIT.search(text)
            assert limit is not None
            rows = min(rows, int(limit.group(1)) + int(limit.group(2) or 0))
        total += rows
    return total


@contextmanager
def counting(root: Path) -> Iterator[Window]:
    """Count the work done against the instance under ``root`` until the block exits."""

    global _WINDOW, _REBINDER
    assert _WINDOW is None, "measured windows do not nest"
    _install_audit_hook()
    if _REBINDER is None:
        _REBINDER = _Rebinder()
    window = Window(prefixes=_prefixes(root))
    _REBINDER.install()
    _WINDOW = window
    try:
        yield window
    finally:
        _WINDOW = None
        _REBINDER.remove()
        for name in COUNTERS:
            window.counts.setdefault(name, 0)
