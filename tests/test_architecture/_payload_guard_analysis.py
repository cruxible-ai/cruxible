"""Static analysis behind the Capture = payload guards (D-R25..D-R27).

Two structural checks over core's source, independent of any word list:

dispatch
    A comparison, membership test, ``match`` or dict-key lookup that branches a
    provider/operation/builtin identifier (``operation``, ``operation.name``,
    ``builtin``, ``provider_id``, ``provider.identity.name``, ``entrypoint`` or a
    local alias of one) on a string value. The value is resolved through
    literals and module-level string constants across core. It passes only when
    the value is core's own declared vocabulary (a member of some ``Literal[...]``
    core declares), a builtin name at one of its allow-listed recognition sites,
    or the site is allow-listed with a reason.

decoding
    Capture material -- bytes read from the body store at a commitment digest --
    may be decoded (json/csv/zlib/gzip/yaml/toml/pickle loads, ``.decode()``)
    only at allow-listed sites: the extract implementation and named transport
    functions. Taint follows local assignments, call arguments into core
    functions (by name) and return values, so a decoder one call away is still
    found.

Sites are ``<repo-relative path>::<qualified function>`` (``<module>`` for
module-level code); a ``::*`` site covers a whole module.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

SUBJECT_IDENTIFIERS = frozenset(
    {
        "operation",
        "operation_name",
        "builtin",
        "provider",
        "provider_id",
        "provider_name",
        "entrypoint",
    }
)
BUILTIN_NAMES = frozenset({"workspace.file", "cruxible-builtin"})

DECODER_CALLS = frozenset(
    {
        "json.loads",
        "json.load",
        "csv.reader",
        "csv.DictReader",
        "zlib.decompress",
        "gzip.decompress",
        "gzip.GzipFile",
        "yaml.safe_load",
        "yaml.load",
        "tomllib.loads",
        "pickle.loads",
        "orjson.loads",
    }
)
DECODER_METHODS = frozenset({"decode", "decompress"})


@dataclass
class Function:
    path: str
    qualname: str
    node: ast.AST
    params: tuple[str, ...]

    @property
    def site(self) -> str:
        return f"{self.path}::{self.qualname}"


@dataclass
class Corpus:
    modules: dict[str, ast.Module]
    functions: list[Function] = field(default_factory=list)
    by_name: dict[str, list[Function]] = field(default_factory=lambda: defaultdict(list))
    #: Per module: module-level name -> the expression it is bound to.
    bindings: dict[str, dict[str, ast.expr]] = field(default_factory=dict)
    #: Per module: imported name -> (source module path, name there), or the
    #: module itself (name ``""``) for ``import x as y`` / ``from pkg import module``.
    imports: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    vocabulary: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, modules: Mapping[str, ast.Module]) -> Corpus:
        corpus = cls(modules=dict(modules))
        for path, tree in corpus.modules.items():
            corpus.functions.append(Function(path, "<module>", tree, ()))
            for function in _functions(path, tree):
                corpus.functions.append(function)
                corpus.by_name[function.qualname.rsplit(".", 1)[-1]].append(function)
            bindings: dict[str, ast.expr] = {}
            for statement in tree.body:
                target, value = _assignment(statement)
                if target is not None and value is not None:
                    bindings[target] = value
            corpus.bindings[path] = bindings
            corpus.imports[path] = _imports(path, tree, set(corpus.modules))
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript) and _name_of(node.value) == "Literal":
                    corpus.vocabulary |= {
                        item.value
                        for item in ast.walk(node.slice)
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    }
        corpus.vocabulary -= BUILTIN_NAMES
        return corpus


def _module_path(dotted: str, paths: set[str]) -> str | None:
    """The corpus path of a dotted core module, when it is one."""

    parts = dotted.split(".")
    roots = {
        "cruxible_core": "src/cruxible_core",
        "cruxible_client": "packages/cruxible-client/src/cruxible_client",
    }
    if parts[0] not in roots:
        return None
    base = "/".join([roots[parts[0]], *parts[1:]])
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in paths:
            return candidate
    return None


def _imports(path: str, tree: ast.Module, paths: set[str]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _module_path(alias.name, paths)
                if target is not None:
                    found[alias.asname or alias.name.split(".")[0]] = (target, "")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            source = _module_path(node.module, paths)
            for alias in node.names:
                submodule = _module_path(f"{node.module}.{alias.name}", paths)
                if submodule is not None:
                    found[alias.asname or alias.name] = (submodule, "")
                elif source is not None:
                    found[alias.asname or alias.name] = (source, alias.name)
    return found


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _assignment(statement: ast.stmt) -> tuple[str | None, ast.expr | None]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        return (target.id if isinstance(target, ast.Name) else None), statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value
    return None, None


def _functions(path: str, tree: ast.Module) -> Iterator[Function]:
    def visit(node: ast.AST, prefix: str) -> Iterator[Function]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                arguments = child.args
                params = tuple(
                    item.arg
                    for item in (
                        *arguments.posonlyargs,
                        *arguments.args,
                        *arguments.kwonlyargs,
                    )
                )
                yield Function(path, name, child, params)
                yield from visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _own_nodes(function: Function) -> Iterator[ast.AST]:
    """The function's nodes; nested function bodies are their own functions."""

    stack = list(reversed(function.node.body))  # type: ignore[attr-defined]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _allowed(site: str, sites: Iterable[str]) -> bool:
    path = site.split("::", 1)[0]
    return any(entry == site or entry == f"{path}::*" for entry in sites)


# -- dispatch ----------------------------------------------------------------------


def _is_subject(expression: ast.AST, aliases: set[str]) -> bool:
    if isinstance(expression, ast.Name):
        return expression.id in SUBJECT_IDENTIFIERS or expression.id in aliases
    if isinstance(expression, ast.Attribute):
        if expression.attr in SUBJECT_IDENTIFIERS:
            return True
        if expression.attr == "name":
            inner = expression.value
            if isinstance(inner, ast.Name) and (
                inner.id in aliases or inner.id in SUBJECT_IDENTIFIERS
            ):
                return True
            if isinstance(inner, ast.Attribute) and inner.attr in SUBJECT_IDENTIFIERS:
                return True
            if isinstance(inner, ast.Attribute) and inner.attr in {"identity", "distribution"}:
                return _is_subject(inner.value, aliases)
        return False
    if isinstance(expression, ast.Call) and _name_of(expression.func) == "str":
        return bool(expression.args) and _is_subject(expression.args[0], aliases)
    return False


def _aliases(function: Function) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in _own_nodes(function):
            target, value = _assignment(node) if isinstance(node, ast.stmt) else (None, None)
            if isinstance(node, ast.NamedExpr):
                target, value = node.target.id, node.value
            if target and value is not None and target not in aliases:
                if _is_subject(value, aliases):
                    aliases.add(target)
                    changed = True
    return aliases


#: How far a chain of constant aliases (``B = A``, imports) is followed.
_ALIAS_DEPTH = 8


@dataclass(frozen=True)
class _Scope:
    """Where a name is looked up: a function's locals, then its module, then imports."""

    corpus: Corpus
    path: str
    local: Mapping[str, ast.expr]


def _local_bindings(function: Function) -> dict[str, ast.expr]:
    if function.qualname == "<module>":
        return {}
    found: dict[str, ast.expr] = {}
    for node in _own_nodes(function):
        if isinstance(node, ast.stmt):
            target, value = _assignment(node)
            if target is not None and value is not None:
                found[target] = value
    return found


def _resolve(expression: ast.AST, scope: _Scope, depth: int = 0) -> ast.expr | None:
    """The expression a name or ``module.NAME`` is bound to, following aliases."""

    if depth > _ALIAS_DEPTH:
        return None
    corpus = scope.corpus
    if isinstance(expression, ast.Name):
        bound = scope.local.get(expression.id)
        if bound is not None and bound is not expression:
            return _settle(bound, scope, depth)
        bound = corpus.bindings.get(scope.path, {}).get(expression.id)
        if bound is not None:
            return _settle(bound, _Scope(corpus, scope.path, {}), depth)
        imported = corpus.imports.get(scope.path, {}).get(expression.id)
        if imported is not None and imported[1]:
            source, name = imported
            return _resolve(ast.Name(id=name), _Scope(corpus, source, {}), depth + 1)
        return None
    if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
        imported = corpus.imports.get(scope.path, {}).get(expression.value.id)
        if imported is not None and not imported[1]:
            return _resolve(
                ast.Name(id=expression.attr), _Scope(corpus, imported[0], {}), depth + 1
            )
    return None


def _settle(bound: ast.expr, scope: _Scope, depth: int) -> ast.expr | None:
    if isinstance(bound, (ast.Name, ast.Attribute)):
        return _resolve(bound, scope, depth + 1)
    return bound


def _values(expression: ast.AST, scope: _Scope) -> set[str]:
    if isinstance(expression, ast.Constant):
        return {expression.value} if isinstance(expression.value, str) else set()
    if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_values(item, scope) for item in expression.elts))
    if (
        isinstance(expression, ast.Call)
        and _name_of(expression.func) in {"frozenset", "set", "tuple"}
        and len(expression.args) == 1
    ):
        return _values(expression.args[0], scope)
    bound = _resolve(expression, scope)
    return set() if bound is None else _values(bound, scope)


def _dict_keys(expression: ast.AST, scope: _Scope) -> set[str]:
    if isinstance(expression, ast.Dict):
        return {
            key.value
            for key in expression.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    bound = _resolve(expression, scope)
    return _dict_keys(bound, scope) if isinstance(bound, ast.Dict) else set()


def _branch_values(function: Function, corpus: Corpus) -> Iterator[tuple[int, str]]:
    aliases = _aliases(function)
    scope = _Scope(corpus, function.path, _local_bindings(function))
    for node in _own_nodes(function):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_is_subject(side, aliases) for side in sides):
                for side in sides:
                    if not _is_subject(side, aliases):
                        for value in _values(side, scope):
                            yield node.lineno, value
        elif isinstance(node, ast.Match) and _is_subject(node.subject, aliases):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    if isinstance(pattern, ast.MatchValue):
                        for value in _values(pattern.value, scope):
                            yield node.lineno, value
        elif isinstance(node, ast.Subscript) and _is_subject(node.slice, aliases):
            for value in _dict_keys(node.value, scope):
                yield node.lineno, value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop", "setdefault"}
            and node.args
            and _is_subject(node.args[0], aliases)
        ):
            for value in _dict_keys(node.func.value, scope):
                yield node.lineno, value


def dispatch_violations(
    corpus: Corpus,
    *,
    builtin_sites: Mapping[str, Iterable[str]],
    dispatch_sites: Iterable[str],
) -> list[str]:
    dispatch_sites = tuple(dispatch_sites)
    found: list[str] = []
    for function in corpus.functions:
        for line, value in _branch_values(function, corpus):
            if value in BUILTIN_NAMES:
                if _allowed(function.site, builtin_sites.get(value, ())):
                    continue
                reason = "builtin name outside its recognition sites"
            elif value in corpus.vocabulary or _allowed(function.site, dispatch_sites):
                continue
            else:
                reason = "not a core-declared vocabulary member"
            found.append(f"{function.site}:{line}: branches on {value!r} ({reason})")
    return found


# -- decoding ----------------------------------------------------------------------


def _commitment_names(function: Function) -> set[str]:
    """Local names bound to a Capture's commitment (``envelope.commitment.digest`` ...)."""

    names: set[str] = set()
    grew = True
    while grew:
        grew = False
        for node in _own_nodes(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if _refers_to_commitment(node.value, names):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                new = {name for target in targets for name in _targets(target)} - names
                if new:
                    names |= new
                    grew = True
    return names


def _refers_to_commitment(expression: ast.AST, names: set[str]) -> bool:
    return any(
        (isinstance(node, ast.Attribute) and node.attr == "commitment")
        or (isinstance(node, ast.Name) and node.id in names)
        for node in ast.walk(expression)
    )


def _material_reads(function: Function) -> list[ast.Call]:
    """Body-store reads addressed by a Capture commitment: the Capture's bytes."""

    names = _commitment_names(function)
    return [
        node
        for node in _own_nodes(function)
        if isinstance(node, ast.Call)
        and _name_of(node.func) in {"read", "read_body"}
        and any(
            _refers_to_commitment(argument, names)
            for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        )
    ]


def _targets(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from _targets(item)


def _carries(expression: ast.AST, names: set[str], material: set[int]) -> bool:
    """Whether ``expression`` holds Capture bytes: a tainted name or a material call."""

    return any(
        id(node) in material or (isinstance(node, ast.Name) and node.id in names)
        for node in ast.walk(expression)
    )


def _tainted(function: Function, seeds: set[str], material: set[int]) -> set[str]:
    names = set(seeds)
    grew = True
    while grew:
        grew = False
        for node in _own_nodes(function):
            pairs: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(node, ast.Assign):
                pairs = [(target, node.value) for target in node.targets]
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
                pairs = [(node.target, node.value)]
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                pairs = [(node.optional_vars, node.context_expr)]
            for target, value in pairs:
                if _carries(value, names, material):
                    new = set(_targets(target)) - names
                    if new:
                        names |= new
                        grew = True
    return names


def _decoded(function: Function, names: set[str], material: set[int]) -> list[int]:
    lines = []
    for node in _own_nodes(function):
        if not isinstance(node, ast.Call):
            continue
        operand = _sink(node)
        if operand is not None and _carries(operand, names, material):
            lines.append(node.lineno)
    return lines


def _sink(node: ast.Call) -> ast.AST | None:
    """The decoded operand of a decoder call, when ``node`` is one."""

    if ast.unparse(node.func) in DECODER_CALLS and node.args:
        return node.args[0]
    if isinstance(node.func, ast.Attribute) and node.func.attr in DECODER_METHODS:
        return node.args[0] if node.func.attr == "decompress" and node.args else node.func.value
    return None


def material_read_sites(corpus: Corpus) -> list[str]:
    return [
        f"{function.site}:{node.lineno}"
        for function in corpus.functions
        for node in _material_reads(function)
    ]


def _callee(corpus: Corpus, node: ast.Call) -> Function | None:
    """The one core function a call names, when the name is unambiguous."""

    targets = corpus.by_name.get(_name_of(node.func) or "", [])
    return targets[0] if len(targets) == 1 else None


def _material_calls(function: Function, corpus: Corpus, returning: set[str]) -> set[int]:
    """Direct material reads and calls to functions that return Capture bytes."""

    found = {id(node) for node in _material_reads(function)}
    for node in _own_nodes(function):
        if isinstance(node, ast.Call):
            callee = _callee(corpus, node)
            if callee is not None and callee.site in returning:
                found.add(id(node))
    return found


def _returning(corpus: Corpus) -> set[str]:
    """Sites of functions whose return value is Capture bytes (to a fixpoint)."""

    returning: set[str] = set()
    changed = True
    while changed:
        changed = False
        for function in corpus.functions:
            if function.qualname == "<module>" or function.site in returning:
                continue
            material = _material_calls(function, corpus, returning)
            names = _tainted(function, set(), material)
            if any(
                isinstance(node, ast.Return)
                and node.value is not None
                and _carries(node.value, names, material)
                for node in _own_nodes(function)
            ):
                returning.add(function.site)
                changed = True
    return returning


def decoding_violations(
    corpus: Corpus, *, read_sites: Iterable[str], decode_sites: Iterable[str]
) -> list[str]:
    """Capture bytes are read only by declared readers and decoded only by extract.

    Every body-store read addressed by a commitment digest must be at a declared
    reader site. Capture bytes are a read's result, a call to a function that
    returns one (a reader's callers hold Capture bytes too), or a name bound to
    either. Within a function -- and one call deep, into a core function with an
    unambiguous name, whether the bytes are passed by name or inline -- they may
    reach a decoder only at a declared decoding site.
    """

    read_sites, decode_sites = tuple(read_sites), tuple(decode_sites)
    returning = _returning(corpus)
    found: list[str] = []
    for function in corpus.functions:
        reads = _material_reads(function)
        if reads and not _allowed(function.site, read_sites):
            found += [
                f"{function.site}:{node.lineno}: reads Capture material outside the readers"
                for node in reads
            ]
        material = _material_calls(function, corpus, returning)
        if not material:
            continue
        names = _tainted(function, set(), material)
        if not _allowed(function.site, decode_sites):
            found += [
                f"{function.site}:{line}: decodes Capture material"
                for line in _decoded(function, names, material)
            ]
        for node in _own_nodes(function):
            if not isinstance(node, ast.Call) or id(node) in material:
                continue
            target = _callee(corpus, node)
            if target is None or _allowed(target.site, decode_sites):
                continue
            params = list(target.params)
            if params and params[0] in {"self", "cls"}:
                params = params[1:]
            carried = {
                params[index]
                for index, argument in enumerate(node.args)
                if index < len(params) and _carries(argument, names, material)
            } | {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg is not None
                and keyword.arg in target.params
                and _carries(keyword.value, names, material)
            }
            if carried:
                inner = _tainted(target, carried, set())
                found += [
                    f"{target.site}:{line}: decodes Capture material passed by {function.site}"
                    for line in _decoded(target, inner, set())
                ]
    return found
