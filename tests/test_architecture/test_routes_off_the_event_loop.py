"""No HTTP route runs synchronous work on the daemon's event loop.

Starlette runs a `def` route in its threadpool and an `async def` one directly on
the event loop. Every route here calls the synchronous runtime/service core, so a
route declared `async def` without awaiting anything holds the loop for the whole
call and every other request on the daemon waits behind it. `export_floor` did
exactly that: a 3-15 s floor export stalled health checks, writes and reads alike.

A route is a plain `def`, with one exception: an `async def` route whose only
work on the loop is to await admission and then offload. `export_floor` is that
shape, so exports queued behind an instance's running export wait on the loop
without holding a threadpool worker. The check below admits exactly that shape:
every call in the route is either an awaited `run_in_threadpool(...)` whose
arguments are plain names, or the context expression of an `async with` naming
a module-level `*_admission` helper, and the helpers themselves only take an
asyncio lock.
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


_OFFLOAD = "run_in_threadpool"
_ADMISSION_SUFFIX = "_admission"
# What an admission helper may call: lock bookkeeping on the running loop.
_ADMISSION_CALLS = frozenset({"get_running_loop", "setdefault", "Lock", "Semaphore"})


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _plain(arguments: list[ast.expr]) -> bool:
    """Arguments evaluated on the loop must not call anything."""

    return not any(isinstance(inner, ast.Call) for arg in arguments for inner in ast.walk(arg))


def _executed(node: ast.AST) -> list[ast.AST]:
    """``node`` and what runs when it runs: a nested function's body runs only
    when called, but its decorators and defaults run where it is defined."""

    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        arguments = node.args
        at_definition: list[ast.AST] = [
            *arguments.defaults,
            *(default for default in arguments.kw_defaults if default is not None),
        ]
        if not isinstance(node, ast.Lambda):
            at_definition.extend(node.decorator_list)
        return [item for part in at_definition for item in _executed(part)]
    found: list[ast.AST] = [node]
    for child in ast.iter_child_nodes(node):
        found.extend(_executed(child))
    return found


def _loop_work(node: ast.AsyncFunctionDef) -> list[str]:
    """Calls this async route makes on the loop beyond admission and offload."""

    # The body only: decorators run once, at import. A function defined in the
    # body is not run by being defined -- its calls happen wherever it is
    # called, and calling it on the loop is itself a call this check sees.
    body = [inner for statement in node.body for inner in _executed(statement)]
    allowed: set[int] = set()
    offloads = 0
    for inner in body:
        if (
            isinstance(inner, ast.Await)
            and isinstance(inner.value, ast.Call)
            and _callee(inner.value) == _OFFLOAD
            and _plain([*inner.value.args, *(k.value for k in inner.value.keywords)])
        ):
            allowed.add(id(inner.value))
            offloads += 1
        if isinstance(inner, ast.AsyncWith):
            for item in inner.items:
                expression = item.context_expr
                if (
                    isinstance(expression, ast.Call)
                    and (_callee(expression) or "").endswith(_ADMISSION_SUFFIX)
                    and _plain(list(expression.args))
                    and not expression.keywords
                ):
                    allowed.add(id(expression))
    work = [
        _callee(inner) or ast.unparse(inner)
        for inner in body
        if isinstance(inner, ast.Call) and id(inner) not in allowed
    ]
    if offloads == 0:
        work.append("<no offload>")
    return work


def _admission_helpers_doing_work(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name.endswith(_ADMISSION_SUFFIX):
            extra = sorted(
                {
                    _callee(inner) or "?"
                    for inner in ast.walk(node)
                    if isinstance(inner, ast.Call) and _callee(inner) not in _ADMISSION_CALLS
                }
            )
            if extra:
                found.append(f"{node.name} calls {', '.join(extra)}")
    return found


def _async_routes_doing_loop_work() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and _is_route(node):
                work = _loop_work(node)
                if work:
                    found[f"{path.name}:{node.name}"] = ", ".join(sorted(set(work)))
        for problem in _admission_helpers_doing_work(tree):
            found[f"{path.name}:{problem}"] = "admission helper does more than take a lock"
    return found


def _route(source: str) -> ast.AsyncFunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


def test_the_check_tells_the_shapes_apart() -> None:
    """Prove the check refuses loop work before trusting its silence."""

    admitted = _route(
        "async def r(a):\n"
        "    b = await run_in_threadpool(resolve, a)\n"
        "    async with export_admission(b):\n"
        "        return await run_in_threadpool(work, b)\n"
    )
    assert _loop_work(admitted) == []
    assert _loop_work(_route("async def r(a):\n    return work(a)\n")) == [
        "work",
        "<no offload>",
    ]
    nested = _route("async def r(a):\n    return await run_in_threadpool(work, parse(a))\n")
    assert "parse" in _loop_work(nested)
    deferred = _route(
        "async def r(a):\n"
        "    def go():\n"
        "        return work(a)\n"
        "    async with export_admission(a):\n"
        "        return await run_in_threadpool(go)\n"
    )
    assert _loop_work(deferred) == []
    called_on_loop = _route(
        "async def r(a):\n"
        "    def go():\n"
        "        return work(a)\n"
        "    go()\n"
        "    return await run_in_threadpool(go)\n"
    )
    assert _loop_work(called_on_loop) == ["go"]
    defaulted = _route(
        "async def r(a):\n"
        "    def go(b=parse(a)):\n"
        "        return work(b)\n"
        "    return await run_in_threadpool(go)\n"
    )
    assert _loop_work(defaulted) == ["parse"]
    unadmitted = _route(
        "async def r(a):\n"
        "    async with open_lock(a):\n"
        "        return await run_in_threadpool(work, a)\n"
    )
    assert _loop_work(unadmitted) == ["open_lock"]


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
    offenders = _async_routes_doing_loop_work()
    assert offenders == {}, (
        "these `async def` routes do work on the daemon's event loop, which stalls "
        "every other request; declare them `def`, or await a module-level "
        "`*_admission` helper and then `run_in_threadpool` the work: "
        + "; ".join(f"{name} ({work})" for name, work in sorted(offenders.items()))
    )
