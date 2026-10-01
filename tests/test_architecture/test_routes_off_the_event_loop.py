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

# Read routes the read-surface cut deletes outright; converting them here would
# only collide with that deletion. Remove each name when its route is gone.
_PENDING_REMOVAL = frozenset(
    {
        "claim_history",
        "dereference_document",
        "discover",
        "document_history",
        "expand",
        "explain",
        "explain_claim",
        "get_claim",
        "get_claim_type",
        "get_document",
        "get_query_definition",
        "get_subject",
        "list_claim_types",
        "list_claims",
        "list_documents",
        "list_policies_in_force",
        "list_principals",
        "list_query_definitions",
        "list_subjects",
        "run_query",
        "search",
        "subject_history",
    }
)


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
    offenders = _async_routes_that_never_await()

    blocking = sorted(
        f"{module}:{name}" for name, module in offenders.items() if name not in _PENDING_REMOVAL
    )
    assert blocking == [], (
        "these routes are `async def` but never await, so their synchronous work "
        "runs on the daemon's event loop and stalls every other request; declare "
        "them `def`: " + ", ".join(blocking)
    )

    stale = sorted(_PENDING_REMOVAL - offenders.keys())
    assert stale == [], (
        "these routes are gone or no longer `async def`; delete them from "
        "_PENDING_REMOVAL: " + ", ".join(stale)
    )
