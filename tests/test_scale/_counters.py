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

SQL scans. The window over-counts rather than miss, and never reads the SQL text
to find tables. Every execution is compiled afresh from its trace callback, on
its own connection, as ``EXPLAIN QUERY PLAN`` and as ``EXPLAIN`` of its own SQL
(literals included: a literal can change the plan) with SQLite's authorizer set
(``statement_scan_rows``). The authorizer names, by their real names, every table
the statement and every trigger it would fire read or write (a view's tables
included); the program's cursors (``OpenRead``/``OpenWrite`` by root page,
trigger programs included) add the tables read without naming a column
(``count(*)``, ``EXISTS``). So spelling, quoting, aliases, CTE prefixes and
views change nothing. Each touched table weighs its rows, counted afresh, unless
the plan proves its access bounded:

- some plan step names it (by its real name, ``schema.table`` included; an
  alias proves nothing) and every such step is an index lookup (a ``SEARCH``, not
  through an automatic index), no plan step walks a table the plan does not
  name, and the statement fires no trigger (the plan omits trigger programs); or
- the whole statement is ``SELECT <columns> FROM <that table> LIMIT n
  [OFFSET m]`` with a single-step plan and nothing filtering, sorting,
  collapsing or combining it (``_row_preserving``): it weighs ``LIMIT + OFFSET``
  rows.

So a table a statement only writes (an INSERT target) weighs its rows: the plan
names no step for it. A view weighs nothing itself (its tables are reported);
the schema table weighs nothing (its rows are the schema, which no instance
grows); a table-valued function weighs nothing. ``PRAGMA integrity_check`` and
its kind, ``ANALYZE``, ``VACUUM`` and ``REINDEX`` weigh every table. Python
traces each trigger firing as a repeat of the outer statement, so each repeat is
weighed again: an over-count. Transaction control weighs nothing.

A window fails, never counting zero, when the authorizer cannot be installed,
when it reports no write for an INSERT, REPLACE, UPDATE or DELETE, when a
touched name is no object of its schema, or when a statement cannot be planned:
each is recorded in ``unexplained``.
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
    #: Diagnostics outside the rule: ``insert_target_rows`` is the part of
    #: ``sql_scan_rows`` weighed only because a table was a plain INSERT target
    #: (written, never read, by no trigger), which no plan step can prove bounded.
    diagnostics: Counter[str] = field(default_factory=Counter)


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
#: Statements that touch no table's rows: transaction control.
_TRANSACTION = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
#: Statements that may walk every table: weighed as walking all of them.
_EVERYTHING = frozenset({"ANALYZE", "VACUUM", "REINDEX"})
#: Pragmas that walk every table of their schema.
_SCANNING_PRAGMAS = frozenset(
    {"integrity_check", "quick_check", "foreign_key_check", "cell_size_check", "optimize"}
)
_WRITES = frozenset({"INSERT", "REPLACE", "UPDATE", "DELETE"})
_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)
#: Authorizer actions that name the table they touch, and where its name is.
_TABLE_ACTIONS = {
    sqlite3.SQLITE_READ: 0,
    sqlite3.SQLITE_INSERT: 0,
    sqlite3.SQLITE_UPDATE: 0,
    sqlite3.SQLITE_DELETE: 0,
    sqlite3.SQLITE_CREATE_INDEX: 1,
    sqlite3.SQLITE_CREATE_TEMP_INDEX: 1,
    sqlite3.SQLITE_DROP_TABLE: 0,
    sqlite3.SQLITE_DROP_TEMP_TABLE: 0,
    sqlite3.SQLITE_ALTER_TABLE: 1,
}
_WRITE_ACTIONS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?\s*;?\s*$", re.IGNORECASE)
#: Anything that can make a LIMITed query read more rows than it returns.
_UNBOUNDED = re.compile(
    r"\b(?:WHERE|DISTINCT|ALL|GROUP|HAVING|ORDER|WINDOW|OVER|UNION|INTERSECT|EXCEPT|JOIN|"
    r"FILTER|WITH|VALUES)\b|[(,]",
    re.IGNORECASE,
)
_FRESH = 0


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


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


def _compiled(
    connection: TracedConnection, sql: str
) -> tuple[list[str], list[tuple[int, str | None, str | None, str | None, str | None]], list[Any]]:
    """The statement's query plan and program, and every action SQLite authorizes.

    The statement is compiled afresh twice (a unique comment keeps Python's
    statement cache from handing back an earlier compilation), as ``EXPLAIN
    QUERY PLAN`` and as ``EXPLAIN``, with the authorizer set: SQLite reports
    every table the statement and the triggers it would fire read or write, by
    its real name, with the trigger or view responsible; the program lists every
    cursor it opens, trigger programs included.
    """

    global _FRESH
    _FRESH += 1
    events: list[tuple[int, str | None, str | None, str | None, str | None]] = []

    def authorize(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        events.append((action, first, second, database, source))
        return sqlite3.SQLITE_OK

    try:
        connection.set_authorizer(authorize)
    except (sqlite3.Error, AttributeError) as exc:
        raise Unexplained(f"the authorizer cannot be installed: {exc}") from exc
    text_factory = connection.text_factory
    try:
        text = sql.rstrip().rstrip(";")
        tag = f"\n/* scale-guard {_FRESH} */"
        plan = connection.execute(f"EXPLAIN QUERY PLAN {text}{tag}").fetchall()
        # A program's operands hold the statement's blobs, which are no text.
        connection.text_factory = lambda value: value.decode("utf-8", "replace")
        program = connection.execute(f"EXPLAIN {text}{tag}").fetchall()
    finally:
        connection.text_factory = text_factory
        connection.set_authorizer(None)
    return [str(row[3]) for row in plan], events, program


#: OP_Open** flag: P2 holds a register, not a root page.
_P2_IS_REGISTER = 0x10
_OPENS = frozenset({"OpenRead", "OpenWrite", "ReopenIdx"})


def _opened(connection: TracedConnection, program: list[Any]) -> tuple[set[tuple[str, str]], bool]:
    """The tables whose rows or indexes the program opens, and whether it runs a trigger."""

    databases = {
        int(seq): str(name) for seq, name, _file in connection.execute("PRAGMA database_list")
    }
    opened: set[tuple[str, str]] = set()
    fires = False
    for row in program:
        opcode, page, database, flags = row[1], row[3], row[4], row[6]
        if opcode == "Program":
            fires = True
        if opcode not in _OPENS or int(flags) & _P2_IS_REGISTER or int(page) == 1:
            continue
        schema = databases.get(int(database))
        found = (
            None
            if schema is None
            else connection.execute(
                f"SELECT type, name, tbl_name FROM {_quote(schema)}.sqlite_master "
                "WHERE rootpage = ?",
                (int(page),),
            ).fetchone()
        )
        if found is None:
            raise Unexplained(f"a cursor on root page {page} of database {database} names nothing")
        kind, name, table = (str(value) for value in found)
        assert schema is not None
        opened.add((schema, name if kind == "table" else table))
    return opened, fires


def _object_type(connection: TracedConnection, database: str, name: str) -> str | None:
    row = connection.execute(
        f"SELECT type FROM {_quote(database)}.sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return None if row is None else str(row[0])


def _every_table(connection: TracedConnection) -> set[tuple[str, str]]:
    return {
        (str(schema), str(name))
        for _seq, schema, _file in connection.execute("PRAGMA database_list").fetchall()
        for (name,) in connection.execute(
            f"SELECT name FROM {_quote(str(schema))}.sqlite_master WHERE type = 'table'"
        ).fetchall()
        if str(name) not in _SCHEMA_TABLES
    }


def _rows(connection: TracedConnection, table: tuple[str, str]) -> int:
    """The table's rows now, counted afresh every time: no cache can go stale."""

    schema, name = table
    (rows,) = connection.execute(f"SELECT count(*) FROM {_quote(schema)}.{_quote(name)}").fetchone()
    return int(rows)


def _step_target(detail: str) -> tuple[str, bool, bool] | None:
    """The name a plan step reads, whether it walks it, and whether it is virtual.

    A ``SCAN`` walks; a ``SEARCH`` is an index lookup, except one through an
    automatic index, whose building walks the table. None for a step naming no
    table (a constant row, a subquery).
    """

    if detail.startswith("SCAN "):
        target, walks = detail[len("SCAN ") :], True
    elif detail.startswith("SEARCH "):
        target, walks = detail[len("SEARCH ") :], " USING AUTOMATIC " in detail
    else:
        return None
    if target == "CONSTANT ROW" or target.startswith("("):
        return None
    virtual = " VIRTUAL TABLE" in target
    return target.split(" USING ", 1)[0].split(" VIRTUAL TABLE", 1)[0], walks, virtual


def statement_scan_rows(connection: TracedConnection, sql: str) -> int:
    """Rows of every table one statement may walk in full, on its own connection, now.

    The tables come from SQLite, never from the SQL text: the authorizer's
    reads and writes (the statement's and its triggers'), and the program's
    cursors. Each weighs its rows unless the plan proves the access bounded:
    some plan step names the table (by its real name; an alias proves nothing)
    and every such step is an index lookup, no step walks a table the plan does
    not name, and the statement fires no trigger; or the whole statement is a
    proved row-preserving ``LIMIT`` walk of that table alone.
    """

    text = sql.strip()
    if text.startswith("--"):
        # SQL SQLite runs itself (reloading statistics or the schema), traced as a
        # comment; a trigger traced unexpanded (``-- TRIGGER name``) is no statement.
        text = text[2:].strip()
        if text.upper().startswith("TRIGGER "):
            raise Unexplained("a trigger traced without its statement")
    first = re.match(r"[A-Za-z]+", text)
    keyword = first.group(0).upper() if first is not None else ""
    if keyword in _TRANSACTION:
        return 0
    if keyword in _EVERYTHING:
        return sum(_rows(connection, table) for table in sorted(_every_table(connection)))
    if not keyword:
        raise Unexplained("a statement whose kind cannot be read")
    details, events, program = _compiled(connection, text)
    if any(
        action == sqlite3.SQLITE_PRAGMA and str(first_name).lower() in _SCANNING_PRAGMAS
        for action, first_name, _second, _database, _source in events
    ):
        return sum(_rows(connection, table) for table in sorted(_every_table(connection)))
    touched = _touched(events)
    if keyword in _WRITES and not any(actions & _WRITE_ACTIONS for actions in touched.values()):
        raise Unexplained("SQLite authorized no write for a write statement")
    opened, fires = _opened(connection, program)
    for table in opened - set(touched):
        touched[table] = set()  # read without naming a column (count(*), EXISTS)
    views = {
        (database, source)
        for _action, _first, _second, database, source in events
        if source is not None
        and database is not None
        and _object_type(connection, database, source) == "view"
    }
    triggered = fires or any(
        source is not None and (database, source) not in views
        for _action, _first, _second, database, source in events
    )
    steps = [step for step in map(_step_target, details) if step is not None]
    named = {name.lower(): table for table in touched for name in (table[1], ".".join(table))}
    unnamed_walk = any(walks for name, walks, _virtual in steps if name.lower() not in named)
    total = 0
    for table in sorted(touched):
        own = [
            (walks, virtual) for name, walks, virtual in steps if named.get(name.lower()) == table
        ]
        kind = _object_type(connection, *table)
        if kind is None:
            if own and all(virtual for _walks, virtual in own):
                continue  # a table-valued function, over its own arguments
            if keyword == "CREATE":
                continue  # the object this statement creates: nothing in it yet
            raise Unexplained(f"{'.'.join(table)} is no object of its schema")
        if kind == "view":
            continue  # the tables it reads are reported themselves
        if own and not triggered and not unnamed_walk and not any(w for w, _v in own):
            continue  # every access is an index lookup the plan names
        rows = _rows(connection, table)
        window = _WINDOW
        if (
            window is not None
            and not own
            and not triggered
            and touched[table] == {sqlite3.SQLITE_INSERT}
        ):
            window.diagnostics["insert_target_rows"] += rows
        if (
            len(details) == 1
            and own == [(True, False)]
            and len(touched) == 1
            and touched[table] <= {sqlite3.SQLITE_READ}
            and not triggered
            and _row_preserving(text)
        ):
            limit = _LIMIT.search(text)
            assert limit is not None
            rows = min(rows, int(limit.group(1)) + int(limit.group(2) or 0))
        total += rows
    return total


def _table_of(
    action: int, first_name: str | None, second_name: str | None, database: str | None
) -> tuple[str, str] | None:
    position = _TABLE_ACTIONS.get(action)
    if position is None:
        return None
    name = (first_name, second_name)[position]
    if action == sqlite3.SQLITE_ALTER_TABLE:
        database = first_name
    if name is None or database is None or name in _SCHEMA_TABLES:
        return None
    return database, name


def _touched(
    events: list[tuple[int, str | None, str | None, str | None, str | None]],
) -> dict[tuple[str, str], set[int]]:
    touched: dict[tuple[str, str], set[int]] = {}
    for action, first_name, second_name, database, _source in events:
        table = _table_of(action, first_name, second_name, database)
        if table is not None:
            touched.setdefault(table, set()).add(action)
    return touched


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
