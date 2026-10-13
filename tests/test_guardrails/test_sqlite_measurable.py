"""Product SQLite stays measurable by the scale guard (``tests/test_scale``).

The scale guard counts SQL work through a tracing ``sqlite3.connect`` wrapper and
callbacks it owns on every connection that wrapper returns. It measures honest
product code; deliberate evasion is out of scope, and the realistic ways to slip
past it by accident are kept out of ``src`` here. Product code must not:

- import ``connect`` (or ``*``) from ``sqlite3``, import or reach
  ``sqlite3.dbapi2`` at all, or use ``sqlite3.connect`` other than by calling it
  (binding it to another name or passing it along keeps the untraced original);
- use ``sqlite3.Connection`` or ``sqlite3.Cursor`` methods through the base class,
  or construct a ``sqlite3.Connection`` itself;
- call ``set_progress_handler``, ``set_trace_callback`` or ``set_authorizer``.

A legitimate exception goes in ``ALLOWED`` with the reason it was reviewed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (ROOT / "src", ROOT / "packages" / "cruxible-client" / "src")
_SETTERS = frozenset({"set_progress_handler", "set_trace_callback", "set_authorizer"})
_CLASSES = frozenset({"Connection", "Cursor"})

#: Reviewed exceptions: (path relative to the repository, rule) -> why it is allowed.
ALLOWED: dict[tuple[str, str], str] = {}


def findings(source: str) -> list[tuple[int, str]]:
    """Every way ``source`` reaches SQLite around the scale guard: (line, rule)."""

    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    modules: dict[str, str] = {}  # local name -> "sqlite3" or "dbapi2"
    classes: set[str] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    modules[alias.asname or "sqlite3"] = "sqlite3"
                elif alias.name == "sqlite3.dbapi2":
                    found.append((node.lineno, "imports sqlite3.dbapi2"))
                    if alias.asname:
                        modules[alias.asname] = "dbapi2"
                    else:
                        modules["sqlite3"] = "sqlite3"
        elif isinstance(node, ast.ImportFrom) and node.module in ("sqlite3", "sqlite3.dbapi2"):
            for alias in node.names:
                if alias.name in ("connect", "*"):
                    found.append((node.lineno, f"imports {alias.name} from {node.module}"))
                elif alias.name == "dbapi2":
                    found.append((node.lineno, "imports sqlite3.dbapi2"))
                    modules[alias.asname or "dbapi2"] = "dbapi2"
                elif alias.name in _CLASSES:
                    classes.add(alias.asname or alias.name)
            if node.module == "sqlite3.dbapi2":
                found.append((node.lineno, "imports sqlite3.dbapi2"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = node.value
            called = isinstance(parents.get(node), ast.Call) and parents[node].func is node
            if node.attr in _SETTERS:
                found.append((node.lineno, f"calls {node.attr}"))
            if isinstance(owner, ast.Name) and owner.id in modules:
                kind = modules[owner.id]
                if node.attr == "dbapi2" or kind == "dbapi2":
                    found.append((node.lineno, "reaches sqlite3.dbapi2"))
                elif node.attr == "connect" and not called:
                    found.append((node.lineno, "binds sqlite3.connect to another name"))
                elif node.attr in _CLASSES and called:
                    found.append((node.lineno, f"constructs sqlite3.{node.attr} directly"))
            base = (
                isinstance(owner, ast.Attribute)
                and owner.attr in _CLASSES
                and isinstance(owner.value, ast.Name)
                and owner.value.id in modules
            ) or (isinstance(owner, ast.Name) and owner.id in classes)
            if base:
                found.append((node.lineno, f"uses {node.attr} through the sqlite3 base class"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in classes:
                found.append((node.lineno, f"constructs sqlite3.{node.func.id} directly"))
    return sorted(set(found))


def test_product_sqlite_goes_through_the_measurable_entry_points() -> None:
    unexpected = []
    for source_root in SOURCES:
        for path in sorted(source_root.rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            for line, rule in findings(path.read_text(encoding="utf-8")):
                if (relative, rule) not in ALLOWED:
                    unexpected.append(f"{relative}:{line}: {rule}")
    assert not unexpected, "\n".join(unexpected)


def test_every_bypass_shape_is_found() -> None:
    source = """
import sqlite3
import sqlite3 as db
import sqlite3.dbapi2 as raw
from sqlite3 import connect
from sqlite3 import Connection, Cursor as C
from sqlite3.dbapi2 import connect as dbconnect
from sqlite3 import dbapi2

opened = sqlite3.connect("ok.db")
aliased = db.connect("ok.db")
kept = sqlite3.connect
handed = factory(open_with=db.connect)
raw.connect("x.db")
sqlite3.dbapi2.connect("x.db")
sqlite3.Connection.set_progress_handler(opened, None, 0)
sqlite3.Connection.execute(opened, "SELECT 1")
C.fetchall(cursor)
Connection.set_trace_callback(opened, None)
built = sqlite3.Connection("x.db")
also = Connection("x.db")
opened.set_authorizer(None)
opened.set_progress_handler(None, 0)
"""
    rules = {(line - 1, rule) for line, rule in findings(source)}
    assert rules == {
        (3, "imports sqlite3.dbapi2"),
        (4, "imports connect from sqlite3"),
        (6, "imports connect from sqlite3.dbapi2"),
        (6, "imports sqlite3.dbapi2"),
        (7, "imports sqlite3.dbapi2"),
        (11, "binds sqlite3.connect to another name"),
        (12, "binds sqlite3.connect to another name"),
        (13, "reaches sqlite3.dbapi2"),
        (14, "reaches sqlite3.dbapi2"),
        (15, "calls set_progress_handler"),
        (15, "uses set_progress_handler through the sqlite3 base class"),
        (16, "uses execute through the sqlite3 base class"),
        (17, "uses fetchall through the sqlite3 base class"),
        (18, "calls set_trace_callback"),
        (18, "uses set_trace_callback through the sqlite3 base class"),
        (19, "constructs sqlite3.Connection directly"),
        (20, "constructs sqlite3.Connection directly"),
        (21, "calls set_authorizer"),
        (22, "calls set_progress_handler"),
    }, sorted(rules)
