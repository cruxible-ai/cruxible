"""Guardrail: every instance mutation passes the shared principal boundary.

An unbound credential and a configured principal ID that names no active
principal are refused on writes. That refusal lives in one place in the
facade (``_actor_id`` and the helpers built on it), so it holds only while
every mutating facade function reaches it. This test enumerates the facade
functions whose permission tier is a write tier and every mutating HTTP route,
and fails when a new write skips the boundary. A write that genuinely should
not be attributed to a principal must be named in ``EXEMPT`` with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cruxible_core.runtime.permissions import PERMISSION_REQUIREMENTS, PermissionMode

REPO_ROOT = Path(__file__).resolve().parents[2]
FACADES = {
    "playbill_api": REPO_ROOT / "src/cruxible_core/runtime/playbill_api.py",
    "host_api": REPO_ROOT / "src/cruxible_core/runtime/host_api.py",
}
ROUTES = (
    REPO_ROOT / "src/cruxible_core/server/routes/playbill.py",
    REPO_ROOT / "src/cruxible_core/server/routes/hosted_instances.py",
)
BOUNDARY = frozenset(
    {"_actor_id", "_write_actor_context", "_curation_actor", "_authoring_coordinator"}
)

EXEMPT: dict[str, str] = {
    # Reads whose tier is a disclosure control, not a mutation.
    "playbill_read_capture": "reads retained Capture bytes; mutates nothing",
    # Daemon-wide operator levers the runtime bootstrap secret drives before,
    # or independently of, any principal; none changes governed state.
    "create_playbill_host": "allocates an empty host before any principal exists",
    "set_playbill_floor_delivery": "chooses a workspace writer under local attachment authority",
    "playbill_host_workspace_detach": "releases a worktree registration; the operator lever",
    "server_restart": "daemon lifecycle",
    "server_stop": "daemon lifecycle",
}


_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _functions(path: Path) -> dict[str, _Function]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _called_names(function: _Function) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _reaches_boundary(name: str, functions: dict[str, _Function], seen: set[str]) -> bool:
    if name in seen or name not in functions:
        return False
    seen.add(name)
    called = _called_names(functions[name])
    return bool(called & BOUNDARY) or any(
        _reaches_boundary(callee, functions, seen) for callee in called if callee in functions
    )


def _permission_tools(function: _Function) -> list[str]:
    return [
        node.args[0].value
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check_permission"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]


def _write_facade_functions() -> dict[str, bool]:
    """Every public facade function at a write tier -> whether it reaches the boundary."""

    found: dict[str, bool] = {}
    for path in FACADES.values():
        functions = _functions(path)
        for name, function in functions.items():
            if name.startswith("_"):
                continue
            tiers = [PERMISSION_REQUIREMENTS[tool] for tool in _permission_tools(function)]
            if any(tier >= PermissionMode.GOVERNED_WRITE for tier in tiers):
                found[name] = _reaches_boundary(name, functions, set())
    return found


def test_every_instance_write_passes_the_principal_boundary() -> None:
    writes = _write_facade_functions()

    unguarded = sorted(name for name, guarded in writes.items() if not guarded)
    assert unguarded == sorted(EXEMPT), (
        "a write-tier facade function skips the principal boundary; call _actor_id / "
        "_require_writer before any side effect, or name it in EXEMPT with a reason: "
        f"{sorted(set(unguarded) - set(EXEMPT))}; stale exemptions: "
        f"{sorted(set(EXEMPT) - set(unguarded))}"
    )


def test_every_mutating_route_delegates_to_an_enumerated_facade_function() -> None:
    """A POST/PUT/PATCH/DELETE route may not mutate outside the facade the test above sees."""

    known = {
        name for path in FACADES.values() for name in _functions(path) if not name.startswith("_")
    }
    strays: list[str] = []
    checked = 0
    for path in ROUTES:
        for function in _functions(path).values():
            verbs = {
                decorator.func.attr
                for decorator in function.decorator_list
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            }
            if not verbs & {"post", "put", "patch", "delete"}:
                continue
            delegated = {
                node.func.attr
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in FACADES
            }
            checked += 1
            if not delegated or not delegated <= known:
                strays.append(f"{path.name}:{function.name}")
    assert checked > 50, "the route scan found almost no mutating routes; it is broken"
    assert strays == []
