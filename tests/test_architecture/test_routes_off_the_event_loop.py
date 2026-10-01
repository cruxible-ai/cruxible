"""No HTTP route runs synchronous work on the daemon's event loop.

Starlette runs a `def` route in its threadpool and an `async def` one directly on
the event loop. Every route here calls the synchronous runtime/service core, so a
route declared `async def` without awaiting anything holds the loop for the whole
call and every other request on the daemon waits behind it. `export_floor` did
exactly that: a 3-15 s floor export stalled health checks, writes and reads alike.

A route is a plain `def`, with one exception: an `async def` route whose only
work on the loop is to await admission and then offload. `export_floor` is that
shape, so exports queued behind an instance's running export wait on the loop
without holding a threadpool worker. The check admits exactly that shape, by
binding rather than by name:

- an offload is `await run_in_threadpool(...)` with plain-name arguments, where
  `run_in_threadpool` is the module's one import from `starlette.concurrency`;
- admission is `async with N.admit(...)` with plain-name arguments, where `N` is
  bound once at module level to `KeyedAdmission()` imported from
  `cruxible_core.server.admission`;
- nothing else in the route calls anything on the loop. A function defined in
  the route counts where it is called; its defaults and decorators count where
  it is defined.

`KeyedAdmission.admit` is checked call by call too: it must be an
`asynccontextmanager` whose every call is asyncio lock or dictionary
bookkeeping on receivers bound to exactly those things.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTES = ROOT / "src" / "cruxible_core" / "server" / "routes"
ADMISSION = ROOT / "src" / "cruxible_core" / "server" / "admission.py"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_OFFLOAD = ("starlette.concurrency", "run_in_threadpool")
_ADMISSION_CLASS = ("cruxible_core.server.admission", "KeyedAdmission")


def _is_route(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in _HTTP_METHODS
        for decorator in node.decorator_list
    )


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


def _plain(arguments: list[ast.expr]) -> bool:
    """Arguments evaluated on the loop must not call anything."""

    return not any(isinstance(inner, ast.Call) for arg in arguments for inner in ast.walk(arg))


def _call_arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _target_names(target: ast.expr) -> list[str]:
    """Names an assignment target binds; ``x[k] = v`` and ``x.a = v`` bind none."""

    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _bound_names(node: ast.AST) -> dict[str, list[ast.AST]]:
    """Every binding of every name anywhere under ``node``."""

    bindings: dict[str, list[ast.AST]] = {}
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assign):
            for target in inner.targets:
                for name in _target_names(target):
                    bindings.setdefault(name, []).append(inner)
        elif isinstance(inner, ast.AnnAssign | ast.AugAssign) and isinstance(
            inner.target, ast.Name
        ):
            bindings.setdefault(inner.target.id, []).append(inner)
        elif isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bindings.setdefault(inner.name, []).append(inner)
        elif isinstance(inner, ast.Import | ast.ImportFrom):
            for alias in inner.names:
                bindings.setdefault(alias.asname or alias.name.split(".")[0], []).append(inner)
        elif isinstance(inner, ast.arg):
            bindings.setdefault(inner.arg, []).append(inner)
        elif isinstance(inner, ast.NamedExpr | ast.For | ast.With | ast.AsyncWith):
            for name in ast.walk(inner):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    bindings.setdefault(name.id, []).append(inner)
    return bindings


def _imports_exactly(bindings: list[ast.AST], module: str, name: str) -> bool:
    return (
        len(bindings) == 1
        and isinstance(bindings[0], ast.ImportFrom)
        and bindings[0].module == module
        and any(alias.name == name and alias.asname is None for alias in bindings[0].names)
    )


class _RouteModule:
    """What a route module binds to the offload and to admission."""

    def __init__(self, tree: ast.Module) -> None:
        self.bindings = _bound_names(tree)
        self.offload = _imports_exactly(self.bindings.get(_OFFLOAD[1], []), *_OFFLOAD)
        admission_class = _imports_exactly(
            self.bindings.get(_ADMISSION_CLASS[1], []), *_ADMISSION_CLASS
        )
        self.admissions: set[str] = set()
        for name, bound in self.bindings.items():
            if (
                admission_class
                and len(bound) == 1
                and isinstance(bound[0], ast.Assign)
                and bound[0] in tree.body
                and len(bound[0].targets) == 1
                and isinstance(bound[0].value, ast.Call)
                and isinstance(bound[0].value.func, ast.Name)
                and bound[0].value.func.id == _ADMISSION_CLASS[1]
                and not _call_arguments(bound[0].value)
            ):
                self.admissions.add(name)


def _loop_work(node: ast.AsyncFunctionDef, module: _RouteModule) -> list[str]:
    """Calls this async route makes on the loop beyond admission and offload."""

    local = _bound_names(node)
    body = [inner for statement in node.body for inner in _executed(statement)]
    allowed: set[int] = set()
    offloads = 0
    for inner in body:
        if (
            isinstance(inner, ast.Await)
            and isinstance(inner.value, ast.Call)
            and isinstance(inner.value.func, ast.Name)
            and inner.value.func.id == _OFFLOAD[1]
            and module.offload
            and _OFFLOAD[1] not in local
            and _plain(_call_arguments(inner.value))
        ):
            allowed.add(id(inner.value))
            offloads += 1
        if isinstance(inner, ast.AsyncWith):
            for item in inner.items:
                expression = item.context_expr
                if (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Attribute)
                    and expression.func.attr == "admit"
                    and isinstance(expression.func.value, ast.Name)
                    and expression.func.value.id in module.admissions
                    and expression.func.value.id not in local
                    and _plain(_call_arguments(expression))
                ):
                    allowed.add(id(expression))
    work = [
        ast.unparse(inner.func)
        for inner in body
        if isinstance(inner, ast.Call) and id(inner) not in allowed
    ]
    if offloads == 0:
        work.append("<no offload>")
    return work


# Calls `KeyedAdmission.admit` may make, as (receiver, attribute): receivers
# are checked against their bindings below, never trusted by spelling alone.
_ADMIT_CALLS = frozenset(
    {
        ("asyncio", "get_running_loop"),
        ("asyncio", "Lock"),
        ("self._loops", "get"),
        ("entries", "get"),
        (None, "_Entry"),
    }
)


def _receiver(call: ast.Call) -> tuple[str | None, str]:
    function = call.func
    if isinstance(function, ast.Name):
        return None, function.id
    if isinstance(function, ast.Attribute):
        return ast.unparse(function.value), function.attr
    return None, ast.unparse(function)


def _admission_violations(tree: ast.Module) -> list[str]:
    """Why ``KeyedAdmission.admit`` in ``tree`` may not run on the loop, if it may not."""

    module = _bound_names(tree)
    problems: list[str] = []
    asyncio_bindings = module.get("asyncio", [])
    if not (
        len(asyncio_bindings) == 1
        and isinstance(asyncio_bindings[0], ast.Import)
        and [(alias.name, alias.asname) for alias in asyncio_bindings[0].names]
        == [("asyncio", None)]
    ):
        problems.append("asyncio is not the asyncio module")
    if not _imports_exactly(
        module.get("asynccontextmanager", []), "contextlib", "asynccontextmanager"
    ):
        problems.append("asynccontextmanager is not contextlib's")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    entry = classes.get("_Entry")
    if (
        entry is None
        or len(module.get("_Entry", [])) != 1
        or any(isinstance(inner, ast.Call) for inner in ast.walk(entry))
    ):
        problems.append("_Entry is missing, rebound or calls something")
    admission = classes.get(_ADMISSION_CLASS[1])
    methods = (
        {}
        if admission is None
        else {
            node.name: node
            for node in admission.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
    )
    init = methods.get("__init__")
    loops_bound = init is not None and any(
        isinstance(statement, ast.AnnAssign)
        and ast.unparse(statement.target) == "self._loops"
        and statement.value is not None
        and ast.unparse(statement.value) == "weakref.WeakKeyDictionary()"
        for statement in init.body
    )
    rebinds_loops = any(
        isinstance(inner, ast.Attribute)
        and isinstance(inner.ctx, ast.Store)
        and ast.unparse(inner) == "self._loops"
        for name, method in methods.items()
        if name != "__init__"
        for inner in ast.walk(method)
    )
    if not loops_bound or rebinds_loops:
        problems.append("self._loops is not only a WeakKeyDictionary")
    admit = methods.get("admit")
    if not isinstance(admit, ast.AsyncFunctionDef) or [
        ast.unparse(decorator) for decorator in admit.decorator_list
    ] != ["asynccontextmanager"]:
        problems.append("admit is not an asynccontextmanager")
        return problems
    local = _bound_names(admit)
    for name in ("asyncio", "_Entry"):
        if name in local:
            problems.append(f"admit rebinds {name}")
    # `entries` may only ever hold the loop's own map or a fresh dict.
    for binding in local.get("entries", []):
        value = binding.value if isinstance(binding, ast.Assign) else None
        if value is None or ast.unparse(value) not in {"self._loops.get(loop)", "{}"}:
            problems.append(f"entries is bound by {ast.unparse(binding)!r}")
    for inner in (item for statement in admit.body for item in _executed(statement)):
        if not isinstance(inner, ast.Call):
            continue
        if _receiver(inner) not in _ADMIT_CALLS:
            problems.append(f"admit calls {ast.unparse(inner.func)}")
        elif not _plain(_call_arguments(inner)) and ast.unparse(inner) != "_Entry(asyncio.Lock())":
            problems.append(f"admit nests calls in {ast.unparse(inner)}")
    return problems


def _async_routes_doing_loop_work() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _RouteModule(tree)
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and _is_route(node):
                work = _loop_work(node, module)
                if work:
                    found[f"{path.name}:{node.name}"] = ", ".join(sorted(set(work)))
    return found


_ROUTE_HEADER = (
    "from starlette.concurrency import run_in_threadpool\n"
    "from cruxible_core.server.admission import KeyedAdmission\n"
    "exports = KeyedAdmission()\n"
)


def _check(source: str) -> list[str]:
    tree = ast.parse(source)
    route = next(node for node in reversed(tree.body) if isinstance(node, ast.AsyncFunctionDef))
    return _loop_work(route, _RouteModule(tree))


def test_the_route_check_tells_the_shapes_apart() -> None:
    """Prove the check refuses loop work before trusting its silence."""

    admitted = (
        "async def r(a):\n"
        "    b = await run_in_threadpool(resolve, a)\n"
        "    def go():\n"
        "        return work(b)\n"
        "    async with exports.admit(b):\n"
        "        return await run_in_threadpool(go)\n"
    )
    assert _check(_ROUTE_HEADER + admitted) == []
    assert _check(_ROUTE_HEADER + "async def r(a):\n    return work(a)\n") == [
        "work",
        "<no offload>",
    ]
    nested = "async def r(a):\n    return await run_in_threadpool(work, parse(a))\n"
    assert "parse" in _check(_ROUTE_HEADER + nested)
    called_on_loop = (
        "async def r(a):\n"
        "    def go():\n"
        "        return work(a)\n"
        "    go()\n"
        "    return await run_in_threadpool(go)\n"
    )
    assert _check(_ROUTE_HEADER + called_on_loop) == ["go"]
    defaulted = (
        "async def r(a):\n"
        "    def go(b=parse(a)):\n"
        "        return work(b)\n"
        "    return await run_in_threadpool(go)\n"
    )
    assert _check(_ROUTE_HEADER + defaulted) == ["parse"]


def test_the_route_check_resolves_bindings_not_names() -> None:
    body = (
        "async def r(a):\n    async with {cm}:\n        return await run_in_threadpool(work, a)\n"
    )
    # A synchronous factory that blocks, then hands back a real async lock.
    blocking_factory = (
        _ROUTE_HEADER
        + "def slow_admission(a):\n"
        + "    time.sleep(30)\n"
        + "    return lock\n"
        + body.format(cm="slow_admission(a)")
    )
    assert _check(blocking_factory) == ["slow_admission"]
    # Something that merely looks like admission.
    impostor = _ROUTE_HEADER + "fake = Fake()\n" + body.format(cm="fake.admit(a)")
    assert _check(impostor) == ["fake.admit"]
    # KeyedAdmission from anywhere else.
    elsewhere = (
        "from starlette.concurrency import run_in_threadpool\n"
        "from mine import KeyedAdmission\n"
        "exports = KeyedAdmission()\n" + body.format(cm="exports.admit(a)")
    )
    assert _check(elsewhere) == ["exports.admit"]
    # Admission rebound after it was made.
    rebound = _ROUTE_HEADER + "exports = Fake()\n" + body.format(cm="exports.admit(a)")
    assert _check(rebound) == ["exports.admit"]
    # An offload that is not Starlette's.
    shadowed = (
        _ROUTE_HEADER
        + "def run_in_threadpool(f, *a):\n"
        + "    return f(*a)\n"
        + body.format(cm="exports.admit(a)")
    )
    assert "run_in_threadpool" in _check(shadowed)
    # Loop work smuggled into admission's arguments.
    smuggled = _ROUTE_HEADER + body.format(cm="exports.admit(parse(a))")
    assert "exports.admit" in _check(smuggled)


_ADMISSION_SOURCE = ADMISSION.read_text(encoding="utf-8")


def test_keyed_admission_only_does_bookkeeping() -> None:
    assert _admission_violations(ast.parse(_ADMISSION_SOURCE)) == []


def test_the_admission_check_resolves_receivers() -> None:
    def spoofed(old: str, new: str) -> list[str]:
        assert old in _ADMISSION_SOURCE
        return _admission_violations(ast.parse(_ADMISSION_SOURCE.replace(old, new, 1)))

    # A dict-looking receiver bound to something else.
    assert spoofed("entries = {}", "entries = make_map()")
    # An allowed terminal name on another receiver.
    assert spoofed("entry = entries.get(key)", "entry = other.get(key)")
    # A module that is not asyncio behind the asyncio name.
    assert spoofed("import asyncio\n", "import trio as asyncio\n")
    # A plain blocking call.
    assert spoofed("entry.users += 1", "entry.users += 1\n        time.sleep(30)")
    # A synchronous admit.
    assert spoofed("    @asynccontextmanager\n    async def admit", "    def admit")


def test_no_route_holds_the_event_loop() -> None:
    offenders = _async_routes_doing_loop_work()
    assert offenders == {}, (
        "these `async def` routes do work on the daemon's event loop, which stalls "
        "every other request; declare them `def`, or await a module-level "
        "KeyedAdmission's admit and then run_in_threadpool the work: "
        + "; ".join(f"{name} ({work})" for name, work in sorted(offenders.items()))
    )


def test_the_scan_sees_routes() -> None:
    tree = ast.parse((ROUTES / "playbill.py").read_text(encoding="utf-8"))
    routes = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_route(node)
    }
    assert "export_floor" in routes
