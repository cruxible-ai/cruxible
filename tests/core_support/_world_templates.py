"""Session-cached copies of expensive Cruxible test worlds.

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
- **Inputs key the world.** Callers put every immutable input a build reads
  into its shape (timestamps, the compiler coordinate, the seeded policies, the
  runtime version), so a constant reassigned without any patch machinery
  builds its own world instead of reusing one built from other inputs.
- **Clean builds only.** A test that patches the runtime (an older compiler, a
  poisoned Git environment, a stricter init) before asking for a world wants
  the patch to shape that build, and a build can reach patched code from any
  thread (the HTTP host initializes on the TestClient's). So the rule does not
  try to work out what a build touched: while any ``MonkeyPatch`` holds a live
  attribute patch or non-environment item patch, while any ``unittest.mock``
  patch is active (other than the session's own isolation seams in
  ``_ISOLATION_SEAMS``), or while something has changed the Git-relevant
  environment, or a module-level callable no longer matches the session
  baseline, every request builds fresh and no template is built or used.
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
from unittest import mock

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

# Attribute patches every test carries from `tests/conftest.py`'s autouse
# isolation fixtures. They redirect workspace-binding discovery, which no world
# build reaches (every build passes its paths explicitly).
_ISOLATION_SEAMS = frozenset(
    {
        ("cruxible_client.authoring.context", "_workspace_binding"),
        ("cruxible_client.authoring.blocks", "_workspace_binding"),
    }
)

# pytest patches its own objects (the config's temp-path factory, say).
_PYTEST_INTERNALS = ("_pytest.", "pytest.")

# Every MonkeyPatch the session creates, so a request can see live patches.
_LIVE_PATCHES: weakref.WeakSet[pytest.MonkeyPatch] = weakref.WeakSet()
if not getattr(pytest.MonkeyPatch.__init__, "_world_templates_tracked", False):
    _original_monkeypatch_init = pytest.MonkeyPatch.__init__

    def _tracked_init(self: pytest.MonkeyPatch) -> None:
        _original_monkeypatch_init(self)
        _LIVE_PATCHES.add(self)

    _tracked_init._world_templates_tracked = True  # type: ignore[attr-defined]
    pytest.MonkeyPatch.__init__ = _tracked_init  # type: ignore[method-assign]

# Every unittest.mock patch currently entered (decorator, context manager or
# start/stop), by patcher identity, described by its target.
_LIVE_MOCKS: dict[int, str] = {}
_mock_patch: Any = getattr(mock, "_patch")
_mock_patch_dict: Any = getattr(mock, "_patch_dict")
if not getattr(_mock_patch.__enter__, "_world_templates_tracked", False):
    _original_patch_enter = _mock_patch.__enter__
    _original_patch_exit = _mock_patch.__exit__
    _original_dict_patch = _mock_patch_dict._patch_dict
    _original_dict_unpatch = _mock_patch_dict._unpatch_dict

    def _tracked_patch_enter(self: Any) -> Any:
        result = _original_patch_enter(self)
        _LIVE_MOCKS[id(self)] = f"mock {_owner(self.target)}.{self.attribute}"
        return result

    def _tracked_patch_exit(self: Any, *exc_info: Any) -> Any:
        _LIVE_MOCKS.pop(id(self), None)
        return _original_patch_exit(self, *exc_info)

    def _tracked_dict_patch(self: Any) -> Any:
        result = _original_dict_patch(self)
        if self.in_dict is not os.environ:  # the environment check covers os.environ
            _LIVE_MOCKS[id(self)] = "mock dict"
        return result

    def _tracked_dict_unpatch(self: Any) -> Any:
        _LIVE_MOCKS.pop(id(self), None)
        return _original_dict_unpatch(self)

    _tracked_patch_enter._world_templates_tracked = True  # type: ignore[attr-defined]
    _mock_patch.__enter__ = _tracked_patch_enter
    _mock_patch.__exit__ = _tracked_patch_exit
    _mock_patch_dict._patch_dict = _tracked_dict_patch
    _mock_patch_dict._unpatch_dict = _tracked_dict_unpatch


@dataclass(frozen=True)
class Template(Generic[_T]):
    """One clean built world: where it lives, what it made, what names its root."""

    root: Path
    entries: tuple[str, ...]
    rewrites: tuple[str, ...]
    value: _T


class WorldTemplates:
    """A per-process cache of built worlds keyed by shape and per-test ordinal."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._templates: dict[Hashable, Template[Any]] = {}
        self._ordinals: dict[Hashable, int] = {}
        self._ordinal_test: str | None = None
        self._environment: dict[str, str] | None = None
        self._callables: list[tuple[str, Any, str, Any]] | None = None
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
            reason = self._patched()
            if reason is not None:
                self._fallback(reason)
                return None
            cached = self._templates.get(key)
            if cached is not None:
                return cached
            if self._root is None:
                self._root = Path(tempfile.mkdtemp(prefix="crux-world-templates-"))
            root = self._root / f"t{next(self._names):03d}"
            root.mkdir(parents=True)
            root = Path(os.path.realpath(root))
            value = _quiescent_build(build, root)
            reason = "build left a thread running" if value is _UNSETTLED else self._patched()
            if reason is not None:
                # Something changed while it built: never share it.
                shutil.rmtree(root, ignore_errors=True)
                self._fallback(reason)
                return None
            built = Template(
                root=root,
                entries=tuple(sorted(item.name for item in root.iterdir())),
                rewrites=_files_naming(root),
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

    def _patched(self) -> str | None:
        """Name the first live patch or runtime difference, if there is one."""

        if _environment() != self._environment:
            return "environment"
        for patch in tuple(_LIVE_PATCHES):
            for target, name, _old in patch._setattr:
                owner = _owner(target)
                if (owner, name) in _ISOLATION_SEAMS or owner.startswith(_PYTEST_INTERNALS):
                    continue
                return f"monkeypatch {owner}.{name}"
            for mapping, key, _old in patch._setitem:
                if mapping is not os.environ:
                    return f"monkeypatch item {key!r}"
                if isinstance(key, str) and _relevant_env(key):
                    return f"monkeypatch env {key}"
        live_mocks = tuple(_LIVE_MOCKS.values())
        if live_mocks:
            return live_mocks[0]
        for owner, namespace, name, value in self._callables or ():
            if namespace.get(name, _MISSING) is not value and (owner, name) not in _ISOLATION_SEAMS:
                return f"replaced callable {owner}.{name}"
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


def _callables() -> list[tuple[str, Any, str, Any]]:
    found: list[tuple[str, Any, str, Any]] = []
    for module_name, module in tuple(sys.modules.items()):
        if module is None or not module_name.startswith(_MODULE_PREFIXES):
            continue
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, value in tuple(namespace.items()):
            if not isinstance(value, _CALLABLE_TYPES):
                continue
            found.append((module_name, namespace, name, value))
            if isinstance(value, type) and value.__module__ == module_name:
                owner = f"{module_name}.{value.__qualname__}"
                for member, member_value in tuple(value.__dict__.items()):
                    if isinstance(member_value, _CALLABLE_TYPES):
                        # The mappingproxy tracks the live class dictionary.
                        found.append((owner, value.__dict__, member, member_value))
    return found


_UNSETTLED = object()


def _quiescent_build(build: Callable[[Path], _T], root: Path) -> Any:
    """Run ``build`` and wait for every thread it started.

    ``_UNSETTLED`` when a thread it started is still running and could write
    into the template after it is copied.
    """

    before = set(threading.enumerate())
    value = build(root)
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=_QUIESCE_SECONDS)
        if thread.is_alive():
            return _UNSETTLED
    _quiesce(root)
    return value


def _owner(target: object) -> str:
    """The module name a patch target belongs to (the module, or its class's)."""

    if isinstance(target, types.ModuleType):
        return target.__name__
    owner = target if isinstance(target, type) else type(target)
    return f"{owner.__module__}.{owner.__qualname__}"


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
