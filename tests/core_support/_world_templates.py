"""Session-cached copies of expensive Playbill test worlds.

A fresh signed genesis costs about 17 Git subprocesses and a seeded world about
90 more, and most tests need *a* world of a given shape, not one built from
nothing. So each pytest process builds each shape once, in a private template
directory, and later requests get a copy:

- the template's top-level entries are copied into the requesting directory;
- the few files that name the template's absolute root (the ledger's
  ``user.signingkey``, SQLite progress rows, registry locations) are rewritten
  to the copy's root;
- the caller reopens the copy through the ordinary open path, which replays the
  descriptor, keys, signatures and roots, so a copy that did not reproduce would
  refuse to open rather than serve.

Equivalence rules:

- **Ordinals.** A shape is keyed by its inputs and by its ordinal within the
  current test. The second world a test builds gets a different template from
  the first, so two worlds in one test still have different keys and genesis
  coordinates, as two fresh builds would.
- **Clean builds only.** A test that patches the runtime (an older compiler, a
  poisoned Git environment) before asking for a world wants the patch to shape
  that build. A template therefore records every name its build executed or
  looked up. A request builds fresh, and neither uses nor creates a template,
  when any live ``MonkeyPatch`` has replaced an attribute of such a name, when a
  callable of such a name no longer matches the session baseline (patches made
  without ``monkeypatch``), or when the Git-relevant environment differs from
  the session baseline.
- **Opt out.** ``CRUXIBLE_TEST_FRESH_WORLDS=1`` disables templates entirely and
  reproduces the pre-template suite.
"""

from __future__ import annotations

import gc
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import weakref
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

import pytest

_T = TypeVar("_T")

FRESH_WORLDS_ENV = "CRUXIBLE_TEST_FRESH_WORLDS"
WORLD_STATS_ENV = "CRUXIBLE_TEST_WORLD_STATS"
_MODULE_PREFIXES = ("cruxible_core", "cruxible_client", "tests")
_ENV_PREFIXES = ("GIT_",)
_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TZ",
        "CRUXIBLE_MODE",
        "CRUXIBLE_DEFAULT_READ_ONLY",
        "CRUXIBLE_PROJECTION_PROCESSING_MAX_BYTES",
        "CRUXIBLE_ARTIFACT_CACHE",
    }
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_QUIESCE_SECONDS = 30.0
_CALLABLE_TYPES = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    staticmethod,
    classmethod,
    property,
    type,
)

# Every MonkeyPatch the session creates, so a request can see live patches.
_LIVE_PATCHES: weakref.WeakSet[pytest.MonkeyPatch] = weakref.WeakSet()
if not getattr(pytest.MonkeyPatch.__init__, "_world_templates_tracked", False):
    _original_monkeypatch_init = pytest.MonkeyPatch.__init__

    def _tracked_init(self: pytest.MonkeyPatch) -> None:
        _original_monkeypatch_init(self)
        _LIVE_PATCHES.add(self)

    _tracked_init._world_templates_tracked = True  # type: ignore[attr-defined]
    pytest.MonkeyPatch.__init__ = _tracked_init  # type: ignore[method-assign]


@dataclass(frozen=True)
class Template(Generic[_T]):
    """One clean built world: where it lives, what it made, what names its root."""

    root: Path
    entries: tuple[str, ...]
    rewrites: tuple[str, ...]
    names: frozenset[str]
    value: _T


class WorldTemplates:
    """A per-process cache of built worlds keyed by shape and per-test ordinal."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._templates: dict[Hashable, Template[Any]] = {}
        self._ordinals: dict[Hashable, int] = {}
        self._ordinal_test: str | None = None
        self._environment: dict[str, str] | None = None
        self._callables: list[tuple[Any, str, Any]] | None = None
        self._lock = threading.RLock()
        self._names = itertools.count()
        self.builds = 0
        self.copies = 0
        self.fallbacks = 0
        self.fallback_reasons: list[str] = []

    def configure(self, root: Path) -> None:
        """Place templates under a session directory and take the clean baseline."""

        with self._lock:
            self._root = root
            self._environment = _environment()
            self._callables = _callables()

    @staticmethod
    def enabled() -> bool:
        return os.environ.get(FRESH_WORLDS_ENV, "").strip().lower() not in {"1", "true", "yes"}

    def ordinal(self, shape: Hashable) -> int:
        """Count the worlds of ``shape`` the current test has asked for."""

        test = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
        with self._lock:
            if test != self._ordinal_test:
                self._ordinal_test = test
                self._ordinals.clear()
            ordinal = self._ordinals.get(shape, 0)
            self._ordinals[shape] = ordinal + 1
            return ordinal

    def template(self, shape: Hashable, build: Callable[[Path], _T]) -> Template[_T] | None:
        """Return the clean template for the next ``shape`` world of this test.

        ``None`` tells the caller to build fresh: templates are disabled, or the
        runtime the build depends on is patched.
        """

        if not self.enabled():
            return None
        ordinal = self.ordinal(shape)
        with self._lock:
            self._ensure_baseline()
            key = (shape, ordinal)
            cached = self._templates.get(key)
            if cached is not None:
                reason = self._patched(cached.names)
                if reason is not None:
                    self._fallback(reason)
                    return None
                return cached
            if _environment() != self._environment:
                self._fallback("environment")
                return None
            if self._root is None:
                self._root = Path(tempfile.mkdtemp(prefix="crux-world-templates-"))
            root = self._root / f"t{next(self._names):03d}"
            root.mkdir(parents=True)
            root = Path(os.path.realpath(root))
            traced = _traced(build, root)
            reason = "build left a thread running" if traced is None else self._patched(traced[1])
            if reason is not None:
                # Built under a patch that may have shaped it: never share it.
                shutil.rmtree(root, ignore_errors=True)
                self._fallback(reason)
                return None
            assert traced is not None
            value, names = traced
            built = Template(
                root=root,
                entries=tuple(sorted(item.name for item in root.iterdir())),
                rewrites=_files_naming(root),
                names=names,
                value=value,
            )
            self._templates[key] = built
            self.builds += 1
            return built

    def report(self, path: Path) -> None:
        """Append this process's build, copy and fallback counts to ``path``."""

        line = json.dumps(
            {
                "worker": os.environ.get("PYTEST_XDIST_WORKER", "main"),
                "builds": self.builds,
                "copies": self.copies,
                "fallbacks": self.fallback_reasons,
            },
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _ensure_baseline(self) -> None:
        if self._environment is None:
            self._environment = _environment()
        if self._callables is None:
            self._callables = _callables()

    def _patched(self, names: frozenset[str]) -> str | None:
        """Name the first live difference a build over ``names`` would see, if any."""

        if _environment() != self._environment:
            return "environment"
        for patch in tuple(_LIVE_PATCHES):
            for target, name, _old in patch._setattr:
                if name in names:
                    return f"monkeypatch {type(target).__name__}.{name}"
            for mapping, key, _old in patch._setitem:
                if mapping is os.environ and isinstance(key, str) and _relevant_env(key):
                    return f"monkeypatch env {key}"
        for namespace, name, value in self._callables or ():
            if name in names and namespace.get(name, _MISSING) is not value:
                return f"replaced callable {name}"
        return None

    def _fallback(self, reason: str) -> None:
        self.fallbacks += 1
        test = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
        self.fallback_reasons.append(f"{test}: {reason}")


_MISSING = object()


def copy_template(template: Template[Any], destination: Path) -> Path | None:
    """Copy ``template`` into ``destination`` and return the resolved copy root.

    ``None`` when a fresh build would not have produced the same layout there:
    the destination is missing or relative, or already holds one of the
    template's entries (other than an existing workspace directory).
    """

    if not destination.is_absolute() or not destination.is_dir():
        return None
    for name in template.entries:
        existing = destination / name
        if os.path.lexists(existing) and not (
            name == "workspace" and existing.is_dir() and not existing.is_symlink()
        ):
            return None
    resolved = Path(os.path.realpath(destination))
    for name in template.entries:
        source = template.root / name
        target = destination / name
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
    for relative in template.rewrites:
        _rewrite(resolved / relative, str(template.root), str(resolved))
    return resolved


def _relevant_env(name: str) -> bool:
    return name.startswith(_ENV_PREFIXES) or name in _ENV_NAMES


def _environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if _relevant_env(name)}


def _callables() -> list[tuple[Any, str, Any]]:
    found: list[tuple[Any, str, Any]] = []
    for module_name, module in tuple(sys.modules.items()):
        if module is None or not module_name.startswith(_MODULE_PREFIXES):
            continue
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, value in tuple(namespace.items()):
            if not isinstance(value, _CALLABLE_TYPES):
                continue
            found.append((namespace, name, value))
            if isinstance(value, type) and value.__module__ == module_name:
                for member, member_value in tuple(value.__dict__.items()):
                    if isinstance(member_value, _CALLABLE_TYPES):
                        # The mappingproxy tracks the live class dictionary.
                        found.append((value.__dict__, member, member_value))
    return found


def _traced(build: Callable[[Path], _T], root: Path) -> tuple[_T, frozenset[str]] | None:
    """Run ``build``; return its value and every name its code called or looked up.

    ``None`` when the build left a thread running that could still write into the
    template after it is copied.
    """

    codes: set[types.CodeType] = set()
    builtins: set[str] = set()

    def profile(frame: types.FrameType, event: str, arg: Any) -> None:
        if event == "call":
            codes.add(frame.f_code)
        elif event == "c_call":
            name = getattr(arg, "__name__", None)
            if isinstance(name, str):
                builtins.add(name)

    before = set(threading.enumerate())
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        value = build(root)
    finally:
        sys.setprofile(previous)
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=_QUIESCE_SECONDS)
        if thread.is_alive():
            return None
    _quiesce(root)
    names = set(builtins)
    for code in codes:
        names.add(code.co_name)
        names.update(code.co_names)
    return value, frozenset(names)


def _quiesce(root: Path) -> None:
    """Close what the build left open so the template's files hold still.

    The built instance is unreachable once its value is captured, but its SQLite
    connections close only when it is collected, and the last close checkpoints
    and removes the WAL. Collect now, then checkpoint any WAL left behind, so no
    copy races a checkpoint.
    """

    gc.collect()
    for wal in sorted(root.rglob("*-wal")):
        database = wal.with_name(wal.name.removesuffix("-wal"))
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()


def _files_naming(root: Path) -> tuple[str, ...]:
    needle = str(root).encode()
    found: list[str] = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and needle in path.read_bytes():
                found.append(str(path.relative_to(root)))
    return tuple(sorted(found))


def _rewrite(path: Path, old: str, new: str) -> None:
    with path.open("rb") as handle:
        header = handle.read(len(_SQLITE_HEADER))
    if header == _SQLITE_HEADER:
        _rewrite_sqlite(path, old, new)
    else:
        path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))


def _rewrite_sqlite(path: Path, old: str, new: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall():
                column = row[1]
                connection.execute(
                    f'UPDATE "{table}" SET "{column}"=replace("{column}",?,?) '
                    f'WHERE typeof("{column}")=\'text\' AND instr("{column}",?)>0',
                    (old, new, old),
                )
        connection.execute("COMMIT")
    finally:
        connection.close()


TEMPLATES = WorldTemplates()
