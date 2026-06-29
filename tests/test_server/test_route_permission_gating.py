"""Route-gating guard: every mutating HTTP route reaches a permission check.

The MCP surface enforces set-equality between registered tools and the permission
map (``validate_tool_permissions``); the HTTP surface historically relied on
convention — each route handler delegates to a ``runtime.api`` facade that calls
``check_permission``. This test makes that convention machine-checked so a future
unguarded mutating route fails CI rather than shipping silently.

Strategy (static, mirrors the MCP set-equality intent):

1. Parse ``runtime/api.py`` once and record which facade functions call
   ``check_permission``.
2. Parse every ``server/routes/*.py`` module and record which module-local
   helper functions call ``check_permission`` (e.g. ``_authorize_*`` wrappers).
3. For every route registered on the real app, find its handler and walk the
   handler body (AST) to collect the facades / helpers it calls.
4. A route is "gated" if any reachable facade or local helper performs a
   permission check.
5. Classify routes as mutating vs read-only and assert every mutating route is
   gated. A handful of routes are gated by a *different* mechanism (the
   auth-middleware one-time bootstrap-secret gate); they are listed explicitly so
   adding a new ungated route can never silently land in that bucket.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cruxible_core.server.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTES_DIR = _REPO_ROOT / "src/cruxible_core/server/routes"
_RUNTIME_API_PATH = _REPO_ROOT / "src/cruxible_core/runtime/api.py"

# Routes that are NOT gated by an in-handler check_permission but ARE
# access-controlled by the auth middleware's one-time bootstrap-secret gate
# (see server/auth.py). Listed explicitly and by (method, path) so a brand-new
# ungated route cannot quietly inherit this exemption.
_BOOTSTRAP_GATED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/runtime/instances"),
        ("POST", "/api/v1/{instance_id}/runtime/bootstrap/claim"),
    }
)

# POST routes that are read-only despite using POST (request body carries query
# inputs, not a mutation). They still go through check_permission at a READ_ONLY
# tier, so they are not exempt from gating — this set only affects the
# mutating/read-only classification used for the stricter mutating assertion.
_READ_ONLY_POST_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/validate",
        "/api/v1/{instance_id}/queries/run",
        "/api/v1/{instance_id}/queries/run-inline",
        "/api/v1/{instance_id}/wiki/render",
        "/api/v1/{instance_id}/evaluate",
        "/api/v1/{instance_id}/lint",
        "/api/v1/{instance_id}/source-evidence/dereference",
    }
)

# Non-API infrastructure routes that never touch instance state.
_INFRA_PATHS: frozenset[str] = frozenset({"/health", "/version", "/ui", "/ui/{path:path}"})

_CHECK_PERMISSION = "check_permission"


def _functions_calling_check_permission(tree: ast.AST) -> set[str]:
    """Return names of module-level functions whose body calls check_permission."""
    gated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _body_calls_check_permission(node):
                gated.add(node.name)
    return gated


def _body_calls_check_permission(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _is_check_permission_call(node.func):
            return True
    return False


def _is_check_permission_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == _CHECK_PERMISSION
    if isinstance(func, ast.Attribute):
        return func.attr == _CHECK_PERMISSION
    return False


def _called_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[set[str], set[str]]:
    """Return (api.<name> targets, bare local helper names) called in *func*."""
    api_targets: set[str] = set()
    local_calls: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "api"
        ):
            api_targets.add(target.attr)
        elif isinstance(target, ast.Name):
            local_calls.add(target.id)
    return api_targets, local_calls


def _module_handlers(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _route_is_gated(
    handler: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    api_gated_fns: set[str],
    local_gated_fns: set[str],
    module_handlers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    """Whether *handler* reaches a check_permission via an api facade or local helper."""
    api_targets, local_calls = _called_names(handler)
    if api_targets & api_gated_fns:
        return True
    # A local helper that itself calls check_permission (e.g. _authorize_*).
    if local_calls & local_gated_fns:
        return True
    # The handler may call a local helper that delegates to a gated api facade.
    for name in local_calls:
        helper = module_handlers.get(name)
        if helper is None:
            continue
        helper_api_targets, _ = _called_names(helper)
        if helper_api_targets & api_gated_fns:
            return True
    return False


def _is_mutating(method: str, path: str) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    return path not in _READ_ONLY_POST_PATHS


def _enumerate_api_routes() -> list[tuple[str, str, str]]:
    """Return (method, path, endpoint_name) for every /api/v1 (or infra) route."""
    app = create_app()
    routes: list[tuple[str, str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        methods = getattr(route, "methods", None)
        if path is None or endpoint is None or methods is None:
            continue
        if not (path.startswith("/api/v1") or path in _INFRA_PATHS):
            continue
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.append((method, path, endpoint.__name__))
    return routes


def _load_gating_index() -> tuple[set[str], dict[str, set[str]], dict[str, dict]]:
    """Parse api.py + every route module once for the gating lookup."""
    api_tree = ast.parse(_RUNTIME_API_PATH.read_text(), filename=str(_RUNTIME_API_PATH))
    api_gated_fns = _functions_calling_check_permission(api_tree)

    module_local_gated: dict[str, set[str]] = {}
    module_handlers: dict[str, dict] = {}
    for module_path in sorted(_ROUTES_DIR.glob("*.py")):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        handlers = _module_handlers(tree)
        module_handlers[module_path.stem] = handlers
        module_local_gated[module_path.stem] = _functions_calling_check_permission(tree)
    return api_gated_fns, module_local_gated, module_handlers


def _endpoint_module(endpoint_name: str, module_handlers: dict[str, dict]) -> str | None:
    for module_name, handlers in module_handlers.items():
        if endpoint_name in handlers:
            return module_name
    return None


def test_every_api_route_resolves_to_a_handler_module() -> None:
    """Sanity: every enumerated API route maps to a parsed route-module handler.

    The infra routes (health/version/ui) live in app.py, not routes/*, so they
    are excluded; everything else must be found, otherwise the gating analysis
    below would silently skip a route.
    """
    _, _, module_handlers = _load_gating_index()
    missing: list[str] = []
    for method, path, endpoint_name in _enumerate_api_routes():
        if path in _INFRA_PATHS:
            continue
        if _endpoint_module(endpoint_name, module_handlers) is None:
            missing.append(f"{method} {path} -> {endpoint_name}")
    assert missing == [], f"Routes with no resolvable handler module: {missing}"


def test_every_mutating_http_route_passes_through_check_permission() -> None:
    """Each mutating HTTP route must reach a check_permission'd facade.

    A future route that mutates state without a permission gate (and is not an
    explicitly documented bootstrap-gated route) fails here.
    """
    api_gated_fns, module_local_gated, module_handlers = _load_gating_index()

    ungated: list[str] = []
    for method, path, endpoint_name in _enumerate_api_routes():
        if path in _INFRA_PATHS:
            continue
        if not _is_mutating(method, path):
            continue
        if (method, path) in _BOOTSTRAP_GATED_ROUTES:
            continue
        module_name = _endpoint_module(endpoint_name, module_handlers)
        assert module_name is not None, f"{method} {path}: handler module not found"
        handler = module_handlers[module_name][endpoint_name]
        gated = _route_is_gated(
            handler,
            api_gated_fns=api_gated_fns,
            local_gated_fns=module_local_gated[module_name],
            module_handlers=module_handlers[module_name],
        )
        if not gated:
            ungated.append(f"{method} {path} -> {endpoint_name}")

    assert ungated == [], (
        "Mutating HTTP routes that do not reach a check_permission'd facade "
        f"(add the permission check, or document it as bootstrap-gated): {ungated}"
    )


def test_bootstrap_gated_routes_still_exist() -> None:
    """The documented bootstrap-gated exemptions must correspond to real routes.

    Prevents the exemption set from rotting into a silent always-pass escape
    hatch after a route is renamed or removed.
    """
    live = {(method, path) for method, path, _ in _enumerate_api_routes()}
    stale = {route for route in _BOOTSTRAP_GATED_ROUTES if route not in live}
    assert stale == set(), f"Bootstrap-gated exemptions no longer match a live route: {stale}"


def test_read_only_post_classification_matches_live_routes() -> None:
    """Every read-only-POST entry must match a real POST route (no stale entries)."""
    live_post_paths = {path for method, path, _ in _enumerate_api_routes() if method == "POST"}
    stale = _READ_ONLY_POST_PATHS - live_post_paths
    assert stale == set(), f"Stale read-only-POST classification entries: {stale}"
