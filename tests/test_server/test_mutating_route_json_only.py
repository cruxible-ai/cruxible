"""Guard: no mutating HTTP route binds a form/raw/multipart body.

The Origin gate in ``server/auth.py`` fails closed for state-changing methods
that present no allowed Origin only when the request is NOT ``application/json``.
The remaining safety of the no-Origin path therefore rests on a structural
invariant: every mutating route accepts a JSON body (a Pydantic model) and never
a CORS "simple-request" body type (``Form``/``File``/``UploadFile``, or a manual
``request.form()`` / ``request.body()`` / ``request.stream()`` read accepting
``text/plain`` / ``application/x-www-form-urlencoded`` / ``multipart/form-data``).

A future raw/form mutating route would let a cross-site simple-request POST with
no Origin reach a handler at loopback-ADMIN. This test makes that invariant
machine-checked two ways:

1. Runtime: walk the live app's mutating routes and assert every request-body
   field is a JSON ``Body`` whose annotation is a Pydantic ``BaseModel`` -- never
   a ``Form`` or ``File`` field.
2. Static: AST-scan every ``server/routes/*.py`` module for manual non-JSON body
   reads (``.form()`` / ``.body()`` / ``.stream()``) and for ``Form`` / ``File``
   / ``UploadFile`` usage, so a manual read FastAPI's dependant cannot see is
   still caught.

If a legitimate raw/form route is ever added, it MUST be made to fail closed in
the Origin gate (not silently exempted here).
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.params import File, Form
from fastapi.routing import APIRoute
from pydantic import BaseModel

from cruxible_core.server.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTES_DIR = _REPO_ROOT / "src/cruxible_core/server/routes"

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Names that, if called on a request object, read a non-JSON body. A handler
# taking a bare Request could read the body itself, bypassing FastAPI's dependant
# analysis, so they are caught statically.
_RAW_BODY_READ_ATTRS = frozenset({"form", "body", "stream"})

# Symbols whose presence in a route module signals a non-JSON body binding.
_NON_JSON_BODY_SYMBOLS = frozenset({"Form", "File", "UploadFile"})


def _mutating_routes() -> list[APIRoute]:
    app = create_app()
    routes: list[APIRoute] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods or set()
        if methods & _STATE_CHANGING_METHODS:
            routes.append(route)
    return routes


def test_every_mutating_route_binds_only_json_body() -> None:
    """Each mutating route's body fields are JSON Pydantic models, never Form/File.

    A no-body mutating route (path-param-only POST) is fine: it reads nothing, so
    there is no simple-request body to smuggle. Only an actual body field that is
    not a JSON-model Body would re-open the gate.
    """
    offenders: list[str] = []
    for route in _mutating_routes():
        for field in route.dependant.body_params:
            field_info = field.field_info
            annotation = field_info.annotation
            ok_json_model = (
                not isinstance(field_info, (Form, File))
                and isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
            )
            # A union like `Model | None` is also acceptable (FastAPI still parses
            # JSON); only Form/File or a non-model scalar body is rejected.
            if isinstance(field_info, (Form, File)):
                offenders.append(
                    f"{sorted(route.methods or set())} {route.path}: "
                    f"field {field.name!r} is a {type(field_info).__name__} body"
                )
            elif not ok_json_model and not _is_optional_model(annotation):
                offenders.append(
                    f"{sorted(route.methods or set())} {route.path}: "
                    f"field {field.name!r} body annotation {annotation!r} is not a JSON model"
                )

    assert offenders == [], (
        "Mutating route(s) bind a non-JSON body. The Origin gate's no-Origin "
        "fail-closed rule relies on the mutating surface being JSON-only; a "
        "form/raw body re-opens the cross-site simple-request hole. Make the route "
        f"fail closed in the Origin gate, do not exempt it here: {offenders}"
    )


def _is_optional_model(annotation: object) -> bool:
    """Whether *annotation* is ``Model | None`` for a Pydantic model ``Model``."""
    import typing

    args = typing.get_args(annotation)
    if not args:
        return False
    non_none = [a for a in args if a is not type(None)]
    if not non_none:
        return False
    return all(isinstance(a, type) and issubclass(a, BaseModel) for a in non_none)


def test_no_route_module_reads_a_non_json_body() -> None:
    """Static guard: no route handler reads a form/raw/multipart body manually.

    Catches a handler that takes a bare ``Request`` and calls ``request.form()`` /
    ``request.body()`` / ``request.stream()`` (which FastAPI's dependant analysis
    in the runtime test above cannot see), and any direct ``Form`` / ``File`` /
    ``UploadFile`` usage.
    """
    raw_reads: list[str] = []
    form_symbols: list[str] = []

    for module_path in sorted(_ROUTES_DIR.glob("*.py")):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            # `<something>.form()` / `.body()` / `.stream()`
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_BODY_READ_ATTRS
            ):
                raw_reads.append(f"{module_path.name}:{node.lineno} -> .{node.func.attr}()")
            # Bare `Form(...)` / `File(...)` / `UploadFile(...)` references.
            if isinstance(node, ast.Name) and node.id in _NON_JSON_BODY_SYMBOLS:
                form_symbols.append(f"{module_path.name}:{node.lineno} -> {node.id}")

    assert raw_reads == [], (
        "Route handler reads a non-JSON body manually (.form()/.body()/.stream()). "
        "This bypasses the JSON-only invariant the Origin gate relies on; make the "
        f"route fail closed in the Origin gate first: {raw_reads}"
    )
    assert form_symbols == [], (
        "Route module uses Form/File/UploadFile (non-JSON body binding), which "
        f"re-opens the cross-site simple-request hole: {form_symbols}"
    )
