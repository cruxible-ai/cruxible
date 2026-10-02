"""No HTTP route runs synchronous work on the daemon's event loop.

Starlette runs a `def` route in its threadpool and an `async def` one directly on
the event loop. Every route here calls the synchronous runtime/service core, so a
route declared `async def` without awaiting anything holds the loop for the whole
call and every other request on the daemon waits behind it. `export_floor` did
exactly that: a 3-15 s floor export stalled health checks, writes and reads alike.

A route is a plain `def`, with one exception: an `async def` route whose only
work on the loop is to await admission and then offload. `export_floor` and
`floor_delta` are that shape, so floor renders queued behind an instance's
running one wait on the loop without holding a threadpool worker. The check
admits exactly that shape, by binding rather than by name:

- an offload is `await run_in_threadpool(...)` with plain-name arguments, where
  `run_in_threadpool` is the module's one import from `starlette.concurrency`;
- an offload inside admission goes through its ticket,
  `async with N.admit(...) as t: ... await run_in_threadpool(t.run, f)`, so the
  key stays held until the offloaded call ends even if the route is cancelled;
- admission is `async with N.admit(...)` with plain-name arguments, where `N` is
  either bound once at module level to `KeyedAdmission()` imported from
  `cruxible_core.runtime.admission`, or imported unaliased from that module,
  which binds it once at module level to `KeyedAdmission()` (`FLOOR_ADMISSION`,
  shared with in-process callers on worker threads);
- nothing else in the route calls anything on the loop. A function defined in
  the route counts where it is called; its defaults and decorators count where
  it is defined.

`KeyedAdmission.admit` is checked call by call too, through every method it
reaches on the loop: it must be an `asynccontextmanager` whose every call is
lock, dictionary, deque or future bookkeeping, or a thread-safe hand-off, on
receivers bound to exactly those things; and the one lock it takes is a
`threading.Lock` that only that bookkeeping holds.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTES = ROOT / "src" / "cruxible_core" / "server" / "routes"
ADMISSION = ROOT / "src" / "cruxible_core" / "runtime" / "admission.py"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_OFFLOAD = ("starlette.concurrency", "run_in_threadpool")
_ADMISSION_CLASS = ("cruxible_core.runtime.admission", "KeyedAdmission")


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


def _target_names(target: ast.expr) -> list[ast.Name]:
    """Names an assignment target binds; ``x[k] = v`` and ``x.a = v`` bind none."""

    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _bound_names(node: ast.AST) -> dict[str, list[ast.AST]]:
    """Every binding of every name anywhere under ``node``, in every binding form.

    Assignments record their statement, so a caller can inspect the bound value;
    every other form (for and with targets, walrus, comprehension and match
    captures, except-as, del, imports, definitions, parameters, and global or
    nonlocal declarations) records its node. A check that needs a name bound
    once sees every way it could have been rebound.
    """

    bindings: dict[str, list[ast.AST]] = {}
    recorded: set[int] = set()

    def bind(name: str, where: ast.AST) -> None:
        bindings.setdefault(name, []).append(where)

    for inner in ast.walk(node):
        if isinstance(inner, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
            for target in targets:
                for name in _target_names(target):
                    recorded.add(id(name))
                    bind(name.id, inner)
        elif isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bind(inner.name, inner)
        elif isinstance(inner, ast.Import | ast.ImportFrom):
            for alias in inner.names:
                bind(alias.asname or alias.name.split(".")[0], inner)
        elif isinstance(inner, ast.arg):
            bind(inner.arg, inner)
        elif isinstance(inner, ast.ExceptHandler) and inner.name is not None:
            bind(inner.name, inner)
        elif isinstance(inner, ast.MatchAs | ast.MatchStar) and inner.name is not None:
            bind(inner.name, inner)
        elif isinstance(inner, ast.MatchMapping) and inner.rest is not None:
            bind(inner.rest, inner)
        elif isinstance(inner, ast.Global | ast.Nonlocal):
            for name in inner.names:
                bind(name, inner)
    for inner in ast.walk(node):
        # for/with/walrus/comprehension targets and del: any other stored name.
        if (
            isinstance(inner, ast.Name)
            and isinstance(inner.ctx, ast.Store | ast.Del)
            and id(inner) not in recorded
        ):
            bind(inner.id, inner)
    return bindings


def _imports_exactly(bindings: list[ast.AST], module: str, name: str) -> bool:
    return (
        len(bindings) == 1
        and isinstance(bindings[0], ast.ImportFrom)
        and bindings[0].module == module
        and any(alias.name == name and alias.asname is None for alias in bindings[0].names)
    )


def _module_admissions(tree: ast.Module) -> set[str]:
    """Names ``tree`` binds once, at module level, to ``KeyedAdmission()`` defined there."""

    bindings = _bound_names(tree)
    defined = bindings.get(_ADMISSION_CLASS[1], [])
    if not (len(defined) == 1 and isinstance(defined[0], ast.ClassDef)):
        return set()
    return {
        name
        for name, bound in bindings.items()
        if len(bound) == 1
        and isinstance(bound[0], ast.Assign)
        and bound[0] in tree.body
        and len(bound[0].targets) == 1
        and isinstance(bound[0].value, ast.Call)
        and isinstance(bound[0].value.func, ast.Name)
        and bound[0].value.func.id == _ADMISSION_CLASS[1]
        and not _call_arguments(bound[0].value)
    }


_SHARED_ADMISSIONS = _module_admissions(ast.parse(ADMISSION.read_text(encoding="utf-8")))


class _RouteModule:
    """What a route module binds to the offload and to admission."""

    def __init__(self, tree: ast.Module, shared: set[str] | None = None) -> None:
        shared = _SHARED_ADMISSIONS if shared is None else shared
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
        # A shared admission the admission module itself binds, imported as is.
        for name in shared:
            if _imports_exactly(self.bindings.get(name, []), _ADMISSION_CLASS[0], name):
                self.admissions.add(name)


def _loop_work(node: ast.AsyncFunctionDef, module: _RouteModule) -> list[str]:
    """Calls this async route makes on the loop beyond admission and offload."""

    local = _bound_names(node)
    body = [inner for statement in node.body for inner in _executed(statement)]
    allowed: set[int] = set()
    offloads = 0
    problems: list[str] = []
    # Offloads inside an admission must go through its ticket.
    admitted: dict[int, str | None] = {}
    for inner in body:
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
                    ticket = item.optional_vars
                    name = ticket.id if isinstance(ticket, ast.Name) else None
                    if name is not None and len(local.get(name, [])) != 1:
                        name = None  # rebound: not the ticket any more
                    for statement in inner.body:
                        for nested in _executed(statement):
                            admitted.setdefault(id(nested), name)
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
            if id(inner) in admitted:
                ticket = admitted[id(inner)]
                first = inner.value.args[0] if inner.value.args else None
                if not (
                    ticket is not None
                    and isinstance(first, ast.Attribute)
                    and first.attr == "run"
                    and isinstance(first.value, ast.Name)
                    and first.value.id == ticket
                ):
                    problems.append("<offload not through the admission ticket>")
    work = [
        ast.unparse(inner.func)
        for inner in body
        if isinstance(inner, ast.Call) and id(inner) not in allowed
    ]
    if offloads == 0:
        work.append("<no offload>")
    return work + problems


# What each function on `admit`'s path may call, by spelling: `admit` itself,
# the bookkeeping methods it reaches, the waiter hand-off (run by whichever
# caller releases, the loop included) and `_grant` (scheduled onto the loop).
# Receivers are pinned by their bindings below, never trusted by spelling alone.
_ON_LOOP_CALLS: dict[str, frozenset[str]] = {
    "KeyedAdmission.admit": frozenset(
        {
            "self._check_reentrant",
            "asyncio.get_running_loop",
            "loop.create_future",
            "self._enter",
            "self._withdraw",
            "_Ticket",
            "self._close",
        }
    ),
    "KeyedAdmission._check_reentrant": frozenset({"getattr", "FloorAdmissionMisuse"}),
    "KeyedAdmission._enter": frozenset(
        {"self._entries.get", "_Entry", "_Waiter", "entry.waiters.append"}
    ),
    "KeyedAdmission._pass_on": frozenset({"entry.waiters.popleft", "waiter.wake"}),
    "KeyedAdmission._leave": frozenset({"self._pass_on"}),
    "KeyedAdmission._close": frozenset({"self._pass_on"}),
    "_Ticket.__init__": frozenset(),
    "KeyedAdmission._withdraw": frozenset({"self._pass_on", "entry.waiters.remove"}),
    "_Waiter.__init__": frozenset(),
    "_Waiter.wake": frozenset({"self.event.set", "self.loop.call_soon_threadsafe"}),
    "_Entry.__init__": frozenset({"deque"}),
    "_grant": frozenset({"future.done", "future.set_result"}),
}
# The only values a receiver name may be bound to inside those functions, besides
# being a parameter.
_RECEIVER_VALUES: dict[str, frozenset[str]] = {
    "loop": frozenset({"asyncio.get_running_loop()"}),
    "future": frozenset({"loop.create_future()"}),
    "entry": frozenset({"self._entries.get(key)", "self._entries[key]"}),
    "ticket": frozenset({"_Ticket(self, key)"}),
    "waiter": frozenset(
        {
            "self._enter(key, loop, future, None)",
            "_Waiter(loop, future, event)",
            "entry.waiters.popleft()",
        }
    ),
}
# Attributes written exactly once in the module, in the named method, to exactly this.
_FIELDS: dict[str, tuple[str, str]] = {
    "self._lock": ("KeyedAdmission.__init__", "threading.Lock()"),
    "self._entries": ("KeyedAdmission.__init__", "{}"),
    "self._owned": ("KeyedAdmission.__init__", "threading.local()"),
    "self.waiters": ("_Entry.__init__", "deque()"),
}


def _admission_violations(tree: ast.Module) -> list[str]:
    """Why ``KeyedAdmission.admit`` in ``tree`` may not run on the loop, if it may not."""

    module = _bound_names(tree)
    problems: list[str] = []
    for name in ("asyncio", "threading"):
        bound = module.get(name, [])
        if not (
            len(bound) == 1
            and isinstance(bound[0], ast.Import)
            and [(alias.name, alias.asname) for alias in bound[0].names] == [(name, None)]
        ):
            problems.append(f"{name} is not the {name} module")
    for name, origin in (("asynccontextmanager", "contextlib"), ("deque", "collections")):
        if not _imports_exactly(module.get(name, []), origin, name):
            problems.append(f"{name} is not {origin}'s")
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    defined: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions[node.name] = node
        if isinstance(node, ast.ClassDef):
            for member in ast.walk(node):
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    qualified = f"{node.name}.{member.name}"
                    defined[qualified] = defined.get(qualified, 0) + 1
                    functions[qualified] = member
    for name in ("KeyedAdmission", "_Entry", "_Waiter", "_Ticket", "_grant"):
        if len(module.get(name, [])) != 1:
            problems.append(f"{name} is missing or rebound")
    # A checked method is defined once in its class and never rebound on an instance.
    for qualified in _ON_LOOP_CALLS:
        if "." in qualified and defined.get(qualified) != 1:
            problems.append(f"{qualified} is missing or defined twice")
    for inner in ast.walk(tree):
        if (
            isinstance(inner, ast.Attribute)
            and isinstance(inner.ctx, ast.Store | ast.Del)
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
            and any(qualified.endswith("." + inner.attr) for qualified in _ON_LOOP_CALLS)
        ):
            problems.append(f"self.{inner.attr} is rebound")
    for field_name, (where, value) in _FIELDS.items():
        writes = [
            inner
            for inner in ast.walk(tree)
            if isinstance(inner, ast.Attribute)
            and isinstance(inner.ctx, ast.Store | ast.Del)
            and ast.unparse(inner) == field_name
        ]
        method = functions.get(where)
        if not (
            method is not None
            and len(writes) == 1
            and any(
                isinstance(statement, ast.AnnAssign)
                and statement.target is writes[0]
                and statement.value is not None
                and ast.unparse(statement.value) == value
                for statement in method.body
            )
        ):
            problems.append(f"{field_name} is not only {value}")
    admit = functions.get("KeyedAdmission.admit")
    if not isinstance(admit, ast.AsyncFunctionDef) or [
        ast.unparse(decorator) for decorator in admit.decorator_list
    ] != ["asynccontextmanager"]:
        problems.append("admit is not an asynccontextmanager")
        return problems
    for qualified, allowed in _ON_LOOP_CALLS.items():
        function = functions.get(qualified)
        if function is None:
            problems.append(f"{qualified} is missing")
            continue
        local = _bound_names(function)
        parameters = {arg.arg for arg in ast.walk(function.args) if isinstance(arg, ast.arg)}
        for name in (
            "asyncio",
            "threading",
            "deque",
            "_Entry",
            "_Waiter",
            "_Ticket",
            "_grant",
            "self",
        ):
            bound = [
                binding
                for binding in local.get(name, [])
                if binding is not function and not isinstance(binding, ast.arg)
            ]
            if bound or (name != "self" and name in parameters):
                problems.append(f"{qualified} rebinds {name}")
        for receiver, values in _RECEIVER_VALUES.items():
            for binding in local.get(receiver, []):
                if isinstance(binding, ast.arg) and receiver in parameters:
                    continue
                value = binding.value if isinstance(binding, ast.Assign | ast.AnnAssign) else None
                if value is None or ast.unparse(value) not in values:
                    problems.append(f"{qualified} binds {receiver} by {ast.unparse(binding)!r}")
        executed = [item for statement in function.body for item in _executed(statement)]
        for inner in executed:
            if isinstance(inner, ast.With | ast.AsyncWith) and [
                ast.unparse(item.context_expr) for item in inner.items
            ] != ["self._lock"]:
                problems.append(f"{qualified} enters {ast.unparse(inner.items[0].context_expr)}")
            if not isinstance(inner, ast.Call):
                continue
            spelled = ast.unparse(inner.func)
            if spelled not in allowed:
                problems.append(f"{qualified} calls {spelled}")
            elif not _plain(_call_arguments(inner)):
                problems.append(f"{qualified} nests calls in {ast.unparse(inner)}")
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
    "from cruxible_core.runtime.admission import KeyedAdmission\n"
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
        "    async with exports.admit(b) as ticket:\n"
        "        return await run_in_threadpool(ticket.run, go)\n"
    )
    assert _check(_ROUTE_HEADER + admitted) == []
    # Inside admission, an offload that bypasses the ticket would let the key go
    # while a cancelled route's call still runs.
    unticketed = admitted.replace("run_in_threadpool(ticket.run, go)", "run_in_threadpool(go)")
    assert _check(_ROUTE_HEADER + unticketed) == ["<offload not through the admission ticket>"]
    untaken = admitted.replace(" as ticket:", ":").replace("ticket.run, go", "go")
    assert _check(_ROUTE_HEADER + untaken) == ["<offload not through the admission ticket>"]
    rebound_ticket = admitted.replace(
        "        return await", "        ticket = other\n        return await"
    )
    assert _check(_ROUTE_HEADER + rebound_ticket) == ["<offload not through the admission ticket>"]
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
        "async def r(a):\n"
        "    async with {cm} as t:\n"
        "        return await run_in_threadpool(t.run, work)\n"
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
    # The shared admission the admission module binds, imported as is.
    shared = (
        "from starlette.concurrency import run_in_threadpool\n"
        "from cruxible_core.runtime.admission import FLOOR_ADMISSION\n"
        + body.format(cm="FLOOR_ADMISSION.admit(a)")
    )
    assert _check(shared) == []
    aliased = shared.replace(
        "import FLOOR_ADMISSION\n", "import KeyedAdmission as FLOOR_ADMISSION\n"
    )
    assert _check(aliased) == ["FLOOR_ADMISSION.admit"]
    not_shared = shared.replace("FLOOR_ADMISSION", "KeyedAdmission")
    assert _check(not_shared) == ["KeyedAdmission.admit"]
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
    # The offload or the admission rebound by a form other than assignment.
    for rebinding in (
        "    try:\n        pass\n    except Exception as run_in_threadpool:\n        pass\n",
        "    for run_in_threadpool in ():\n        pass\n",
        "    with open(a) as run_in_threadpool:\n        pass\n",
        "    [run_in_threadpool for run_in_threadpool in ()]\n",
        "    match a:\n        case [*run_in_threadpool]:\n            pass\n",
    ):
        source = _ROUTE_HEADER + body.format(cm="exports.admit(a)").replace(
            "async def r(a):\n", "async def r(a):\n" + rebinding, 1
        )
        assert "run_in_threadpool" in _check(source), rebinding
    module_rebinding = (
        _ROUTE_HEADER
        + "try:\n    pass\nexcept Exception as exports:\n    pass\n"
        + body.format(cm="exports.admit(a)")
    )
    assert _check(module_rebinding) == ["exports.admit"]
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

    # A receiver bound to something other than the bookkeeping it names.
    assert spoofed("entry = self._entries.get(key)", "entry = make_map(key)")
    assert spoofed("waiter = entry.waiters.popleft()", "waiter = other.popleft()")
    # An allowed terminal name on another receiver.
    assert spoofed("entry.waiters.append(waiter)", "other.waiters.append(waiter)")
    # Modules that are not asyncio or threading behind their names.
    assert spoofed("import asyncio\n", "import trio as asyncio\n")
    assert spoofed("import threading\n", "import fake_threading as threading\n")
    # A plain blocking call, in admit or in a method it reaches.
    assert spoofed("        loop = asyncio.get_running_loop()\n", "        time.sleep(30)\n")
    assert spoofed("            entry.waiters.append(waiter)\n", "            time.sleep(30)\n")
    # A synchronous admit.
    assert spoofed("    @asynccontextmanager\n    async def admit", "    def admit")
    # The bookkeeping lock swapped for something else, or a second lock taken.
    assert spoofed("threading.Lock()", "threading.RLock()")
    assert spoofed(
        "        with self._lock:\n            entry = self._entries.get(key)",
        "        with self._slow:\n            entry = self._entries.get(key)",
    )
    # A field written twice, or a method rebound on the instance.
    assert spoofed(
        "        self._entries: dict[str, _Entry] = {}\n",
        "        self._entries: dict[str, _Entry] = {}\n        self._lock = SlowLock()\n",
    )
    assert spoofed(
        "    def active_keys(self) -> int:\n",
        "    def active_keys(self) -> int:\n        self._leave = slow\n",
    )
    # The hand-off swapped for a blocking one.
    assert spoofed("self.event.set()", "self.event.wait()")


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
    assert {"export_floor", "floor_delta"} <= routes
    assert "FLOOR_ADMISSION" in _SHARED_ADMISSIONS
    assert "FLOOR_ADMISSION" in _RouteModule(tree).admissions


def _http_hold_paths(modules: dict[str, ast.Module]) -> list[str]:
    """Early-warning smoke check for statically named callables.

    The request-context check in KeyedAdmission.hold is the runtime guarantee;
    this scan does not model every Python indirection and must stay a smoke check.
    Each function has its own bindings. Nested imports stay in their scope,
    and a nested body is followed only when its callable is referenced.
    """
    functions = {}
    bindings = {}
    routes = []

    def scope_nodes(body):
        found = []

        def visit(node):
            found.append(node)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                # Defaults and decorators execute here; the body has its own scope.
                found.extend(_executed(node))
            elif isinstance(node, ast.ClassDef):
                for expression in (
                    *node.bases,
                    *(keyword.value for keyword in node.keywords),
                    *node.decorator_list,
                ):
                    found.extend(_executed(expression))
            else:
                for child in ast.iter_child_nodes(node):
                    visit(child)

        for statement in body:
            visit(statement)
        return found

    def register_scope(body, parent, prefix, *, class_scope=False, arguments=None):
        nodes = scope_nodes(body)
        names = dict(parent)
        # Parameters shadow enclosing names; defaults can contain other scopes.
        if arguments is not None:
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ):
                if argument is not None:
                    names[argument.arg] = None
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names[node.name] = f"{prefix}.{node.name}"
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified = f"{prefix}.{node.name}"
                functions[qualified] = node
                # A method closes over the class's enclosing scope, not its
                # namespace. Nested functions close over their parent function.
                bindings[qualified] = register_scope(
                    node.body,
                    parent if class_scope else names,
                    qualified,
                    arguments=node.args,
                )
                if _is_route(node):
                    routes.append(qualified)
            elif isinstance(node, ast.ClassDef):
                register_scope(
                    node.body,
                    parent if class_scope else names,
                    f"{prefix}.{node.name}",
                    class_scope=True,
                )
        return names

    for module, tree in modules.items():
        register_scope(tree.body, {}, module)

    def resolve(node, names):
        if isinstance(node, ast.Name):
            return names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = resolve(node.value, names)
            return f"{parent}.{node.attr}" if parent else None
        return None

    problems = []
    for route in routes:
        pending = [(route, [route])]
        visited = set()
        while pending:
            name, path = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            function = functions[name]
            names = bindings[name]
            for node in scope_nodes(function.body):
                target = resolve(node, names)
                if target == "cruxible_core.runtime.admission.FLOOR_ADMISSION.hold":
                    problems.append(" -> ".join([*path, "FLOOR_ADMISSION.hold"]))
                elif target in functions and target not in visited:
                    pending.append((target, [*path, target]))
    return sorted(set(problems))


def test_no_http_path_reaches_blocking_floor_admission() -> None:
    source = ROOT / "src"
    modules = {
        ".".join(path.relative_to(source).with_suffix("").parts): ast.parse(
            path.read_text(encoding="utf-8")
        )
        for path in (source / "cruxible_core").rglob("*.py")
    }
    assert _http_hold_paths(modules) == []


def test_http_hold_check_rejects_a_sync_route_through_host_api() -> None:
    modules = {
        "routes": ast.parse("""
from cruxible_core.runtime import host_api
@router.post("/toggle")
def toggle():
    return host_api.toggle()
"""),
        "cruxible_core.runtime.host_api": ast.parse("""
def toggle():
    from cruxible_core.consumers.floor import refresh_floor
    return refresh_floor()
"""),
        "cruxible_core.consumers.floor": ast.parse("""
from cruxible_core.runtime.admission import FLOOR_ADMISSION

def refresh_floor():
    with FLOOR_ADMISSION.hold("instance"):
        return admitted_body()

def admitted_body():
    return 1
"""),
    }
    assert _http_hold_paths(modules) == [
        "routes.toggle -> cruxible_core.runtime.host_api.toggle -> "
        "cruxible_core.consumers.floor.refresh_floor -> FLOOR_ADMISSION.hold"
    ]
    modules["cruxible_core.runtime.host_api"] = ast.parse("""
from cruxible_core.consumers.floor import admitted_body

def toggle():
    return admitted_body()
""")
    assert _http_hold_paths(modules) == []


def test_http_hold_check_rejects_a_statically_named_class_method() -> None:
    modules = {
        "routes": ast.parse("""
from cruxible_core.runtime import host_api
@router.post("/toggle")
def toggle():
    return host_api.Toggle.run()
"""),
        "cruxible_core.runtime.host_api": ast.parse("""
from cruxible_core.runtime.admission import FLOOR_ADMISSION
class Toggle:
    @staticmethod
    def run():
        with FLOOR_ADMISSION.hold("instance"):
            return 1
"""),
    }
    assert _http_hold_paths(modules) == [
        "routes.toggle -> cruxible_core.runtime.host_api.Toggle.run -> FLOOR_ADMISSION.hold"
    ]


def test_http_hold_check_keeps_nested_imports_out_of_the_outer_scope() -> None:
    modules = {
        "routes": ast.parse("""
from cruxible_core.runtime.admission import FLOOR_ADMISSION
@router.post("/toggle")
def toggle():
    def unrelated():
        from harmless import object as FLOOR_ADMISSION
        return FLOOR_ADMISSION
    with FLOOR_ADMISSION.hold("instance"):
        return 1
"""),
    }
    assert _http_hold_paths(modules) == ["routes.toggle -> FLOOR_ADMISSION.hold"]


def test_http_hold_check_resolves_called_nested_functions_in_their_own_scope() -> None:
    source = """
from cruxible_core.runtime.admission import FLOOR_ADMISSION
@router.post("/toggle")
def toggle():
    def unused():
        with FLOOR_ADMISSION.hold("instance"):
            return 1
    def called():
        from harmless import object as FLOOR_ADMISSION
        with FLOOR_ADMISSION.hold("instance"):
            return 1
    return called()
"""
    assert _http_hold_paths({"routes": ast.parse(source)}) == []
    source = source.replace(
        "from harmless import object as FLOOR_ADMISSION",
        "from cruxible_core.runtime.admission import FLOOR_ADMISSION",
    )
    assert _http_hold_paths({"routes": ast.parse(source)}) == [
        "routes.toggle -> routes.toggle.called -> FLOOR_ADMISSION.hold"
    ]


def test_http_hold_check_methods_do_not_inherit_class_namespace_imports() -> None:
    modules = {
        "routes": ast.parse("""
from cruxible_core.runtime import host_api
@router.post("/toggle")
def toggle():
    return host_api.Toggle.run()
"""),
        "cruxible_core.runtime.host_api": ast.parse("""
from cruxible_core.runtime.admission import FLOOR_ADMISSION
class Toggle:
    from harmless import object as FLOOR_ADMISSION
    @classmethod
    def run(cls):
        with FLOOR_ADMISSION.hold("instance"):
            return 1
"""),
    }
    assert _http_hold_paths(modules) == [
        "routes.toggle -> cruxible_core.runtime.host_api.Toggle.run -> FLOOR_ADMISSION.hold"
    ]
