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
- ``sql_vm_steps``: virtual machine instructions SQLite ran for them, measured by
  a progress handler (``SQL work`` below): proportional to the rows every cursor
  visits, range walks, trigger programs, foreign-key checks and index scans
  included;
- ``projection_binds``: calls of ``PlaybillInstance.bind_accepted_projection``;
- ``cas_body_reads``: CAS bodies read and hashed
  (``ContentAddressedBodyStore._verified_bytes`` and ``.availability``);
- ``git_blob_reads``: ledger blobs read through the resident batch reader
  (``GitLedger.read_blobs``, one per distinct object id requested), which
  ``read_tree`` and every blob read go through; the per-process blob cache in
  front of it absorbs repeats.

Only the measuring thread's work counts (the thread that opened the window):
another thread's opens, spawns, calls and SQL never do.

Function counters are installed when a window opens and removed when it closes:
every module global naming an original is rebound to a counting wrapper, then
back. The audit hook (opens and spawns) cannot be removed, so it is installed
once and does nothing outside a window. SQLite connections outlive windows (an
instance holds some open), so ``trace_sqlite`` wraps every connection opened
under the scale root for the module's lifetime; its trace callback records
nothing outside a window.

SQL work. When a window opens, every traced connection gets a progress handler
called every ``STEP_EVERY`` VM instructions (removed when the window closes; a
connection opened inside the window gets one at once); it counts only on the
measuring thread, so ``sql_vm_steps`` is the measured work itself, not an
inference from plans. The trace callback attributes each connection's steps to
the statement it last started there. A statement traced on the measuring thread
on a connection that took no handler is recorded in ``unexplained``, which fails
the window: its work would go uncounted. One instruction is one step whatever it
does inside: a B-tree seek is one step at any table size, which is what lets a
per-key lookup stay flat, but so is ``OP_Count`` (``SELECT count(*) FROM t``
with no ``WHERE`` counts the table's pages inside one step), a sorter's sort
(its input rows are counted as they are fed), ``PRAGMA integrity_check`` and a
SQL function's own work. Those are this counter's blind spots.

Attribution only. When the window closes, its top statements by VM steps
(literals masked) are compiled once more, as ``EXPLAIN QUERY PLAN`` and as
``EXPLAIN`` with SQLite's authorizer set, to name the tables they touch and how
each cursor moved (``Rewind``, ``Next`` after a seek, ...), with the Python call
paths that spent their steps (``PATH_FRAMES`` product frames each, innermost
first) and how often each ran them. Nothing in that attribution decides a count.
"""

from __future__ import annotations

import functools
import importlib
import os
import re
import sqlite3
import sys
import threading
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
    "sql_vm_steps",
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
    #: The measuring thread: only its work counts.
    thread: int = field(default_factory=threading.get_ident)
    counts: Counter[str] = field(default_factory=Counter)
    #: SQL whose work this window could not count; never treated as zero.
    unexplained: list[str] = field(default_factory=list)
    #: VM steps per statement (literals masked).
    statements: Counter[str] = field(default_factory=Counter)
    #: Per masked statement: its connection, database and first SQL.
    sources: dict[str, tuple[Any, str, str]] = field(default_factory=dict)
    #: Per masked statement, per Python call path that ran it: VM steps and executions.
    paths: dict[str, Counter[tuple[str, str]]] = field(default_factory=dict)
    #: The top statements by VM steps, with their tables and cursors (``attribute``).
    attribution: list[dict[str, Any]] = field(default_factory=list)

    def measuring(self) -> bool:
        return threading.get_ident() == self.thread


_WINDOW: Window | None = None


def _prefixes(root: Path) -> tuple[str, ...]:
    return tuple(sorted({os.path.normpath(str(root)), os.path.realpath(root)}))


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + os.sep) for prefix in prefixes)


# -- opens and spawns: one audit hook for the process ---------------------------------

_AUDIT_INSTALLED = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    window = _WINDOW
    if window is None or not window.measuring():
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
        if window is not None and window.measuring():
            window.counts[counter] += 1
        return original(*args, **kwargs)

    return counted


def _counting_blobs(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def counted(self: Any, oids: Any) -> Any:
        oids = tuple(oids)
        window = _WINDOW
        if window is not None and window.measuring():
            window.counts["git_blob_reads"] += len(dict.fromkeys(oids))
        return original(self, oids)

    return counted


_REBINDER: _Rebinder | None = None


# -- SQLite: traced for the module's lifetime, its work measured inside a window --------

_ORIGINAL_CONNECT = sqlite3.connect
#: VM instructions between progress callbacks; every callback counts this many.
STEP_EVERY = 1
#: Statements a window names, with their tables and cursors, when it closes.
ATTRIBUTED = 10


@dataclass
class _ConnectionState:
    database: str
    #: The statement this connection last started (literals masked), and its call path.
    current: str | None = None
    path: str = ""
    #: VM steps since that statement started, on the measuring thread.
    steps: int = 0
    #: Whether this window's progress handler is set on the connection.
    counting: bool = False


class TracedConnection(sqlite3.Connection):
    """A connection under the scale root: weakly referenced, with its counting state."""

    scale_state: _ConnectionState


_CONNECTIONS: weakref.WeakSet[TracedConnection] = weakref.WeakSet()


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


_LITERALS = re.compile(r"[xX]'[0-9a-fA-F]*'|'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")


def _masked(sql: str) -> str:
    return _LITERALS.sub("?", sql.strip())[:240]


def _on_step(state: _ConnectionState) -> int:
    window = _WINDOW
    if window is not None and window.measuring():
        state.steps += STEP_EVERY
    return 0


#: Steps a connection ran in the window before its first traced statement there: a
#: statement started before the window, or a schema load while preparing one.
BEFORE_ANY = "(before the connection's first statement in the window)"


def _flush(state: _ConnectionState, window: Window) -> None:
    if state.steps and _under(state.database, window.prefixes):
        window.counts["sql_vm_steps"] += state.steps
        current = state.current or BEFORE_ANY
        window.statements[current] += state.steps
        window.paths.setdefault(current, Counter())[("steps", state.path)] += state.steps
    state.steps = 0


#: Product frames named per call path, innermost first.
PATH_FRAMES = 4


def _call_path() -> str:
    """The innermost product frames that ran the statement: ``file:line <- file:line ...``."""

    frames = []
    frame = sys._getframe(2)
    while frame is not None and len(frames) < PATH_FRAMES:
        filename = frame.f_code.co_filename
        if "/cruxible_core/" in filename or "/cruxible_client/" in filename:
            frames.append(f"{filename.rsplit('/src/', 1)[-1]}:{frame.f_lineno}")
        frame = frame.f_back  # type: ignore[assignment]
    return " <- ".join(frames) or "?"


def _on_statement(reference: Any, state: _ConnectionState, sql: str) -> None:
    window = _WINDOW
    if window is None or not window.measuring() or not _under(state.database, window.prefixes):
        return
    window.counts["sql_statements"] += 1
    _flush(state, window)
    masked = _masked(sql)
    state.current, state.path = masked, _call_path()
    window.paths.setdefault(masked, Counter())[("runs", state.path)] += 1
    if masked not in window.sources:
        window.sources[masked] = (reference, state.database, sql)
    if not state.counting:
        window.unexplained.append(f"no step counter on its connection :: {masked[:160]}")


def _count_steps(connection: TracedConnection) -> None:
    state = connection.scale_state
    state.steps, state.current, state.path = 0, None, ""
    try:
        connection.set_progress_handler(functools.partial(_on_step, state), STEP_EVERY)
    except sqlite3.Error:
        state.counting = False  # closed, or bound to another thread: it runs nothing here
    else:
        state.counting = True


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
        state = _ConnectionState(database)
        connection.scale_state = state
        connection.set_trace_callback(
            functools.partial(_on_statement, weakref.ref(connection), state)
        )
        _CONNECTIONS.add(connection)
        if _WINDOW is not None:
            _count_steps(connection)
        return connection

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        yield
    finally:
        sqlite3.connect = _ORIGINAL_CONNECT


# -- attribution: which tables and cursors the heaviest statements used ------------------


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


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
_FRESH = 0


class Unexplained(Exception):
    """An attribution that could not be read; it never decides a count."""


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


#: OP_Open** flag: P2 holds a register, not a root page (an object this statement creates).
_P2_IS_REGISTER = 0x10
_OPENS = frozenset({"OpenRead", "OpenWrite", "ReopenIdx"})
#: Cursor operations that walk a table (or its index) from one end.
_WALKS = frozenset({"Rewind", "Last", "Count"})
#: Cursor operations that step to the next entry: after a seek, a range walk.
_ITERATES = frozenset({"Next", "Prev"})
#: Operations that empty or drop a whole table by its root page (operand holding the database).
_WHOLE_TABLE = {"Clear": 2, "Destroy": 3}


@dataclass
class _Cursor:
    """One table (or index of it) a compiled program opens, and how it moves."""

    segment: int  # 0 for the statement's own program, then one per trigger program
    table: tuple[str, str]
    index: str | None
    walks: bool = False
    iterates: bool = False
    moves: set[str] = field(default_factory=set)

    def described(self) -> str:
        where = f"index {self.index}" if self.index else "table"
        program = "statement" if self.segment == 0 else f"trigger program {self.segment}"
        return f"{where} in the {program}: {'/'.join(sorted(self.moves))}"


def _object_at(connection: TracedConnection, schema: str, page: int) -> tuple[str, str, str] | None:
    """``(type, name, table)`` of the schema object rooted at ``page``."""

    found = connection.execute(
        f"SELECT type, name, tbl_name FROM {_quote(schema)}.sqlite_master WHERE rootpage = ?",
        (page,),
    ).fetchone()
    return None if found is None else (str(found[0]), str(found[1]), str(found[2]))


def _program(
    connection: TracedConnection, program: list[Any]
) -> tuple[list[_Cursor], set[tuple[str, str]], bool]:
    """Every cursor the program (and each trigger program in it) opens on a table or
    index, how it moves, the tables emptied or dropped whole, and whether a trigger runs."""

    databases = {
        int(seq): str(name) for seq, name, _file in connection.execute("PRAGMA database_list")
    }

    def located(page: int, database: int) -> tuple[tuple[str, str], str | None]:
        schema = databases.get(int(database))
        found = None if schema is None else _object_at(connection, schema, int(page))
        if schema is None or found is None:
            raise Unexplained(f"root page {page} of database {database} names no object")
        kind, name, table = found
        return (schema, name if kind == "table" else table), (None if kind == "table" else name)

    cursors: dict[tuple[int, int], list[_Cursor]] = {}
    whole: set[tuple[str, str]] = set()
    fires = False
    segment = -1
    for row in program:
        address, opcode, first, second, third, flags = (
            int(row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            int(row[6]),
        )
        if address == 0:
            segment += 1
        if opcode == "Program":
            fires = True
        elif opcode in _OPENS:
            if flags & _P2_IS_REGISTER or int(second) == 1:
                continue  # an object this statement creates, or the schema table
            table, index = located(second, third)
            cursors.setdefault((segment, int(first)), []).append(_Cursor(segment, table, index))
        elif opcode in _WHOLE_TABLE:
            operands = {2: second, 3: third}
            whole.add(located(first, operands[_WHOLE_TABLE[opcode]])[0])
        elif opcode in _WALKS or opcode in _ITERATES:
            for cursor in cursors.get((segment, int(first)), ()):
                cursor.moves.add(opcode)
                if opcode in _WALKS:
                    cursor.walks = True
                else:
                    cursor.iterates = True
    return [cursor for opened in cursors.values() for cursor in opened], whole, fires


def _table_of(
    action: int, first_name: str | None, second_name: str | None, database: str | None
) -> tuple[str, str] | None:
    position = _TABLE_ACTIONS.get(action)
    if position is None:
        return None
    name = (first_name, second_name)[position]
    if action == sqlite3.SQLITE_ALTER_TABLE:
        database = first_name
    if name is None or database is None or name.startswith("sqlite_"):
        return None
    return database, name


def _described(connection: TracedConnection, sql: str) -> dict[str, Any]:
    """The tables one statement touches and how each cursor on them moves."""

    text = sql.strip()
    if text.startswith("--"):
        text = text[2:].strip()
    details, events, program = _compiled(connection, text)
    cursors, whole, fires = _program(connection, program)
    tables = {
        table
        for action, first_name, second_name, database, _source in events
        if (table := _table_of(action, first_name, second_name, database)) is not None
    } | {cursor.table for cursor in cursors}
    return {
        "tables": sorted(".".join(table) for table in tables),
        "cursors": sorted(
            {
                f"{'.'.join(cursor.table)}: {cursor.described()}"
                for cursor in cursors
                if cursor.moves
            }
            | {f"{'.'.join(table)}: emptied or dropped whole" for table in whole}
        ),
        "plan": details,
        "fires_trigger": fires,
    }


def _attribute(window: Window) -> list[dict[str, Any]]:
    found = []
    for masked, steps in window.statements.most_common(ATTRIBUTED):
        paths = window.paths.get(masked, Counter())
        entry: dict[str, Any] = {
            "statement": masked,
            "vm_steps": steps,
            "runs": sum(count for (kind, _), count in paths.items() if kind == "runs"),
            # Where the steps were spent: the heaviest call paths, with their steps and runs.
            "paths": [
                [path, count, paths[("runs", path)]]
                for (kind, path), count in paths.most_common()
                if kind == "steps"
            ][:3],
        }
        if masked not in window.sources:
            found.append(entry)
            continue
        reference, database, sql = window.sources[masked]
        connection = reference()
        opened = None
        try:
            if connection is not None:
                connection.total_changes  # noqa: B018 - raises once closed
            else:
                raise sqlite3.ProgrammingError("closed")
        except sqlite3.ProgrammingError:
            try:
                opened = _ORIGINAL_CONNECT(
                    Path(database).as_uri() + "?mode=ro", uri=True, factory=TracedConnection
                )
                connection = opened
            except sqlite3.Error as exc:
                entry["unavailable"] = f"{type(exc).__name__}: {exc}"
        try:
            if connection is not None and "unavailable" not in entry:
                entry.update(_described(connection, sql))
        except (Unexplained, sqlite3.Error) as exc:
            entry["unavailable"] = f"{type(exc).__name__}: {exc}"
        finally:
            if opened is not None:
                opened.close()
        found.append(entry)
    return found


@contextmanager
def counting(root: Path) -> Iterator[Window]:
    """Count the work the calling thread does against the instance under ``root``."""

    global _WINDOW, _REBINDER
    assert _WINDOW is None, "measured windows do not nest"
    _install_audit_hook()
    if _REBINDER is None:
        _REBINDER = _Rebinder()
    window = Window(prefixes=_prefixes(root))
    _REBINDER.install()
    for connection in list(_CONNECTIONS):
        _count_steps(connection)
    _WINDOW = window
    try:
        yield window
    finally:
        _WINDOW = None
        _REBINDER.remove()
        for connection in list(_CONNECTIONS):
            state = connection.scale_state
            _flush(state, window)
            state.counting, state.current = False, None
            try:
                connection.set_progress_handler(None, 0)
            except sqlite3.Error:
                pass
        for name in COUNTERS:
            window.counts.setdefault(name, 0)
        window.attribution = _attribute(window)
