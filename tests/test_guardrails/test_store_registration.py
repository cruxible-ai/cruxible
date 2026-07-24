"""Guardrail: every ``<domain>/store.py`` completes the store registration checklist.

Store modules all follow one pattern, and the pattern is spread across five
files. Adding a store means remembering every seam; the newest store shipped
having missed one of them. This guardrail discovers the store modules from the
filesystem and re-derives each seam from live code, so a new store that skips a
step fails here with the checklist as the message.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any, get_type_hints

from tests.test_storage.test_sqlite_state import DIRECT_SQLITE_IMPORT_ALLOWLIST

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.storage import sqlite as sqlite_backend
from cruxible_core.storage.protocols import UnitOfWorkProtocol

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "cruxible_core"
SQLITE_BACKEND_PATH = SRC_ROOT / "storage" / "sqlite.py"
RUNTIME_INSTANCE_PATH = SRC_ROOT / "runtime" / "instance.py"

UOW_CLASS = "SQLiteUnitOfWork"
BACKEND_CLASS = "SQLiteStorageBackend"
BACKEND_SCHEMA_HOOK = "_initialize_connection"
RUNTIME_CLASS = "CruxibleInstance"

CHECKLIST = """\
Adding src/cruxible_core/<domain>/store.py means completing all of:
  1. define the store type in the module (``<Name>Store`` and/or
     ``<Name>StoreProtocol``);
  2. add the allowlist entry in tests/test_storage/test_sqlite_state.py
     (DIRECT_SQLITE_IMPORT_ALLOWLIST) if the module imports sqlite3 directly;
  3. declare the slot on UnitOfWorkProtocol in storage/protocols.py, typed as
     the store's protocol;
  4. wire the same slot in SQLiteUnitOfWork.__init__ with
     ``connection=self._conn, initialize_schema=False`` so the store joins the
     open transaction instead of opening its own connection;
  5. construct the store in SQLiteStorageBackend._initialize_connection so a
     fresh state.db gets its schema;
  6. expose ``get_<name>_store()`` on both InstanceProtocol and the runtime
     CruxibleInstance, returning ``self._active_uow.<slot>`` when a unit of
     work is active.
Offending modules and the step they missed:"""


def _store_modules() -> list[Path]:
    modules = sorted(SRC_ROOT.glob("*/store.py"))
    # Non-vacuity floor: discovery finding fewer modules than the known set
    # means the glob root is wrong, not that stores disappeared.
    assert len(modules) >= 7, f"store discovery found only {len(modules)} modules under {SRC_ROOT}"
    return modules


def _imports_sqlite3(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        (isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
        for node in ast.walk(tree)
    )


def _class_def(path: Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{path} no longer defines {class_name}")


def _function_def(class_node: ast.ClassDef, function_name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node
    raise AssertionError(f"{class_node.name} no longer defines {function_name}()")


def _uow_wiring() -> dict[str, tuple[str, set[str]]]:
    """Map ``SQLiteUnitOfWork`` slot -> (constructed class name, keyword names)."""
    init = _function_def(_class_def(SQLITE_BACKEND_PATH, UOW_CLASS), "__init__")
    wiring: dict[str, tuple[str, set[str]]] = {}
    for node in init.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        value = node.value
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
        ):
            keywords = {kw.arg for kw in value.keywords if kw.arg is not None}
            wiring[target.attr] = (value.func.id, keywords)
    return wiring


def _schema_bootstrap_classes() -> set[str]:
    """Class names constructed in ``SQLiteStorageBackend._initialize_connection``."""
    hook = _function_def(_class_def(SQLITE_BACKEND_PATH, BACKEND_CLASS), BACKEND_SCHEMA_HOOK)
    return {
        node.func.id
        for node in ast.walk(hook)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _runtime_store_accessors() -> dict[str, str]:
    """Map unit-of-work slot -> runtime ``get_<name>_store`` accessor name.

    An accessor qualifies only when it reads ``self._active_uow.<slot>``, which
    is the invariant that keeps a store inside the open transaction.
    """
    runtime_class = _class_def(RUNTIME_INSTANCE_PATH, RUNTIME_CLASS)
    accessors: dict[str, str] = {}
    for node in runtime_class.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("get_"):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Attribute)
                and inner.value.attr == "_active_uow"
            ):
                accessors[inner.attr] = node.name
    return accessors


def _store_types(module: Any) -> set[type]:
    """Store classes/protocols defined (not merely imported) by ``module``."""
    return {
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and obj.__module__ == module.__name__
        and obj.__name__.endswith(("Store", "StoreProtocol"))
    }


def test_every_store_module_completes_the_registration_checklist() -> None:
    slot_types = get_type_hints(UnitOfWorkProtocol)
    wiring = _uow_wiring()
    bootstrap = _schema_bootstrap_classes()
    accessors = _runtime_store_accessors()
    problems: list[str] = []

    for path in _store_modules():
        domain = path.parent.name
        module = importlib.import_module(f"cruxible_core.{domain}.store")
        defined = _store_types(module)
        if not defined:
            problems.append(f"{path}: (1) defines no *Store/*StoreProtocol type")
            continue

        if (
            _imports_sqlite3(path)
            and path.relative_to(REPO_ROOT) not in DIRECT_SQLITE_IMPORT_ALLOWLIST
        ):
            problems.append(f"{path}: (2) imports sqlite3 but is not in the allowlist")

        # A slot belongs to this module when either the protocol it is typed as
        # or the concrete class wired into it is defined by the module. That
        # covers both layouts in the tree: protocol in instance_protocol.py with
        # the implementation in the store module, and protocol in the store
        # module with the SQLite implementation in the backend.
        owned = [
            slot
            for slot, protocol in slot_types.items()
            if protocol in defined
            or (slot in wiring and getattr(sqlite_backend, wiring[slot][0], None) in defined)
        ]
        if not owned:
            problems.append(f"{path}: (3) no UnitOfWorkProtocol slot is typed for this store")
            continue
        slot = owned[0]

        if slot not in wiring:
            problems.append(f"{path}: (4) SQLiteUnitOfWork.__init__ does not set self.{slot}")
            continue
        store_class_name, keywords = wiring[slot]
        missing_keywords = {"connection", "initialize_schema"} - keywords
        if missing_keywords:
            problems.append(
                f"{path}: (4) SQLiteUnitOfWork.__init__ builds {store_class_name} without "
                f"{sorted(missing_keywords)}"
            )

        store_class = getattr(sqlite_backend, store_class_name, None)
        if store_class is None or not issubclass(store_class, slot_types[slot]):
            problems.append(
                f"{path}: (3) {store_class_name} does not implement {slot_types[slot].__name__}"
            )

        if store_class_name not in bootstrap:
            problems.append(
                f"{path}: (5) {store_class_name} is not constructed in "
                f"{BACKEND_CLASS}.{BACKEND_SCHEMA_HOOK}"
            )

        accessor = accessors.get(slot)
        if accessor is None:
            problems.append(
                f"{path}: (6) no runtime {RUNTIME_CLASS} accessor returns self._active_uow.{slot}"
            )
        elif not hasattr(InstanceProtocol, accessor):
            problems.append(f"{path}: (6) InstanceProtocol does not declare {accessor}()")

    assert problems == [], CHECKLIST + "\n" + "\n".join(problems)


def test_direct_sqlite_import_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted path exists and still imports sqlite3 directly."""
    stale = sorted(
        str(path)
        for path in (REPO_ROOT / entry for entry in DIRECT_SQLITE_IMPORT_ALLOWLIST)
        if not path.exists() or not _imports_sqlite3(path)
    )
    assert stale == [], (
        "DIRECT_SQLITE_IMPORT_ALLOWLIST entries that are gone or no longer import "
        f"sqlite3 (drop them): {stale}"
    )
