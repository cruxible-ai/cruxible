"""No HTTP route runs synchronous work on the daemon's event loop.

Starlette runs a `def` route in its threadpool and an `async def` one directly on
the event loop. Every route here calls the synchronous runtime/service core, so a
route declared `async def` without awaiting anything holds the loop for the whole
call and every other request on the daemon waits behind it. `export_floor` did
exactly that: a 3-15 s floor export stalled health checks, writes and reads alike.

A route may be `async def` only if it actually awaits.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTES = ROOT / "src" / "cruxible_core" / "server" / "routes"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _is_route(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in _HTTP_METHODS
        for decorator in node.decorator_list
    )


def _awaits(node: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(inner, ast.Await | ast.AsyncFor | ast.AsyncWith) for inner in ast.walk(node)
    )


def _async_routes_that_never_await() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and _is_route(node) and not _awaits(node):
                found[node.name] = path.name
    return found


def test_the_scan_sees_routes() -> None:
    """Prove the scan can find a route at all before trusting its silence."""

    tree = ast.parse((ROUTES / "playbill.py").read_text(encoding="utf-8"))
    routes = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_route(node)
    }
    assert "export_floor" in routes


def test_no_route_holds_the_event_loop() -> None:
    blocking = sorted(
        f"{module}:{name}" for name, module in _async_routes_that_never_await().items()
    )
    assert blocking == [], (
        "these routes are `async def` but never await, so their synchronous work "
        "runs on the daemon's event loop and stalls every other request; declare "
        "them `def`: " + ", ".join(blocking)
    )
