"""Regenerate the ratified Playbill v1 served-surface inventory.

What the pin covers, and what it did not until 2026-09-09. Each HTTP route's
request and response digest is taken over the RESOLVED JSON schema, so a field
added, renamed, retyped or removed inside a request body moves the pin. Before
that the digest was taken over FastAPI's `{"$ref": ...}`, which hashed a
component name: adding `git_object_format` to the init request moved nothing,
and 78 routes shared 49 request digests because the digest space was model
names rather than schemas. A model that refers to itself is inlined until it
repeats and then left as a stable marker, so a recursive body still digests
deterministically.

Reviewers should expect request-body deltas to appear here now. A patch that
touches a served request model moves this snapshot even when no route, verb or
tool was added, and that movement is the freeze working rather than noise.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import re
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parents[1]
FACADE = REPO_ROOT / "src/cruxible_core/runtime/playbill_api.py"
MCP_HANDLERS = REPO_ROOT / "src/cruxible_core/mcp/handlers.py"
SNAPSHOT = REPO_ROOT / "tests/goldens/playbill/served-surface-dp0b-v1.json"
_COMPONENT_PREFIX = "#/components/schemas/"

FORMAT = "playbill-served-surface-v1"
# Attribution is by role: a public artifact never carries a person's name.
RATIFIED_BY = "maintainer"
_REGISTER_ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def surface_digest(surface: Mapping[str, object]) -> str:
    """Return the exact digest committed by a succession block."""
    return "sha256:" + hashlib.sha256(_canonical_json(surface).encode()).hexdigest()


def _resolve_schema(
    schema: object,
    components: Mapping[str, object],
    seen: tuple[str, ...] = (),
) -> object:
    """Inline every `$ref` so the digest is over the SCHEMA, not the model name.

    FastAPI puts `{"$ref": "#/components/schemas/Foo"}` where a request or
    response body goes, and digesting that hashed a component NAME. Renaming,
    adding, retyping or removing any field inside a request model moved no pin
    at all: 78 pinned routes shared 49 distinct request digests, because the
    digest space was model names. The MCP lane was pinned properly the whole
    time -- its tool schemas are inlined already -- so the same artifact was
    honest about one transport and silent about the other.

    A model that refers to itself is inlined until it repeats, then left as a
    marker naming the cycle: the marker is stable, so a recursive model still
    digests deterministically and still moves when anything inside it moves.
    """

    if isinstance(schema, list):
        return [_resolve_schema(item, components, seen) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith(_COMPONENT_PREFIX):
        name = reference[len(_COMPONENT_PREFIX) :]
        if name in seen:
            return {"$recursive_ref": name}
        target = components.get(name)
        if target is None:
            return dict(schema)
        resolved = _resolve_schema(target, components, (*seen, name))
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        if not siblings or not isinstance(resolved, dict):
            return resolved
        inlined = {key: _resolve_schema(value, components, seen) for key, value in siblings.items()}
        return {**resolved, **inlined}
    return {key: _resolve_schema(value, components, seen) for key, value in schema.items()}


def _schema_digest(schema: object, components: Mapping[str, object] | None = None) -> str | None:
    if schema is None:
        return None
    resolved = schema if components is None else _resolve_schema(schema, components)
    return "sha256:" + hashlib.sha256(_canonical_json(resolved).encode()).hexdigest()


def _facade_operations(path: Path = FACADE) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("playbill_")
    )


def _mcp_facade_operations(path: Path = MCP_HANDLERS) -> list[str]:
    """Pin the exact facade breadth the MCP lane reaches.

    Tool count alone does not bound the MCP surface: an existing handler that
    starts calling one more facade operation widens what MCP can reach without
    adding a tool. Pinning the operations it calls makes that a pin movement.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "playbill_api"
            and node.func.attr.startswith("playbill_")
        }
    )


def _direct_facade_attributes(node: ast.AST) -> set[str]:
    """Facade verbs named anywhere under `node` as `playbill_api.X` / `host_api.X`.

    References, not only calls: a handler that hands the facade verb to the
    dispatcher as a callable reaches it exactly as much as one that calls it,
    and reads as a bare attribute in the tree.
    """

    return {
        item.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id in {"playbill_api", "host_api"}
    }


def _shim_facade_operations(tree: ast.Module) -> dict[str, set[str]]:
    """Facade verbs each in-module adapter CLASS reaches, by class name.

    `handlers.py` reaches the facade two ways. Most handlers name
    `playbill_api.<verb>` in their own body. The rest hand a local adapter
    object -- `_LocalFloorClient`, `_LocalAttestationClient`,
    `_LocalSourceContextClient` -- to shared client-side code, and the verbs are
    named inside that class's methods instead. A body-only walk records the
    second shape as reaching NOTHING, which is the shape three mutating tools
    take, so the published join failed OPEN exactly where an overlay would rely
    on it most.
    """

    return {
        node.name: _direct_facade_attributes(node)
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }


def _handler_facade_operations(path: Path = MCP_HANDLERS) -> dict[str, list[str]]:
    """Which facade verbs each MCP handler reaches, by handler name.

    The flat `mcp_facade_operations` list says the MCP lane can reach a verb; it
    does not say WHICH tool reaches it, so recovering the join meant trusting
    the `cruxible_<verb>` / `handle_<verb>` naming convention, which nothing
    guarantees. An overlay that decides per-verb whether a tenant may reach a
    verb over MCP needs the join to be a fact in the artifact rather than a
    spelling it infers.

    The join is a REACHABILITY closure over the module, not a body-only read.
    A handler reaches a verb three ways, and all three count the same to an
    overlay deciding what a tenant may reach: it names `playbill_api.<verb>`
    itself; it constructs a local adapter class whose methods name the verb
    (the object is the handler's road to the facade, so naming the class is
    naming the road); or it delegates to a sibling handler that does either.
    A body-only walk published `[]` for the second and third shapes, which is
    the shape three mutating tools take, so the join failed OPEN exactly where
    an overlay would rely on it most.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    shims = _shim_facade_operations(tree)
    direct: dict[str, set[str]] = {}
    delegates: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reached = set(_direct_facade_attributes(node))
        named = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
        for shim in named & set(shims):
            reached |= shims[shim]
        direct[node.name] = reached
        delegates[node.name] = named - {node.name}

    operations: dict[str, list[str]] = {}
    for name in direct:
        reached: set[str] = set()
        pending = [name]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            reached |= direct.get(current, set())
            pending.extend(delegates.get(current, set()) & set(direct))
        if reached:
            operations[name] = sorted(reached)
    return operations


def _type_name(annotation: object) -> str | None:
    if annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is not None:
        args = ",".join(filter(None, (_type_name(arg) for arg in get_args(annotation))))
        if origin.__module__ == "builtins":
            origin_name = origin.__qualname__
        else:
            origin_name = f"{origin.__module__}.{origin.__qualname__}"
        return f"{origin_name}[{args}]"
    module = getattr(annotation, "__module__", None)
    qualname = getattr(annotation, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return qualname if module == "builtins" else f"{module}.{qualname}"
    return str(annotation)


def _called_attributes(function: Callable[..., object], owner: str) -> list[str]:
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(function)))
    tree = ast.parse(source)
    return sorted(
        {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == owner
        }
    )


def _http_surface() -> list[dict[str, object]]:
    from fastapi.routing import APIRoute

    from cruxible_core.server.app import create_app

    app = create_app()
    openapi = app.openapi()
    components = openapi.get("components", {}).get("schemas", {})
    result: list[dict[str, object]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = inspect.unwrap(route.endpoint)
        if endpoint.__module__ != "cruxible_core.server.routes.playbill":
            continue
        body_field = route.body_field
        request_annotation = None if body_field is None else body_field.field_info.annotation
        for method in sorted(route.methods or ()):
            operation = openapi["paths"][route.path][method.lower()]
            request_schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            response_schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            result.append(
                {
                    "method": method,
                    "path": route.path,
                    "request_model": _type_name(request_annotation),
                    "request_schema_digest": _schema_digest(request_schema, components),
                    "response_model": _type_name(route.response_model),
                    "response_schema_digest": _schema_digest(response_schema, components),
                    "delegate": f"{endpoint.__module__}.{endpoint.__qualname__}",
                    "facade_operations": _called_attributes(endpoint, "playbill_api"),
                }
            )
    return sorted(result, key=lambda row: (str(row["path"]), str(row["method"])))


def _mcp_surface() -> list[dict[str, object]]:
    from mcp.server.fastmcp import FastMCP

    from cruxible_core.mcp.tools import register_tools
    from cruxible_core.runtime.permissions import TOOL_PERMISSIONS

    server = FastMCP("playbill-v1-served-surface")
    register_tools(server)
    tools = getattr(server, "_tool_manager").list_tools()
    handler_operations = _handler_facade_operations()
    result: list[dict[str, object]] = []
    for tool in tools:
        function = inspect.unwrap(tool.fn)
        handler_calls = _called_attributes(function, "handlers")
        delegate = (
            f"cruxible_core.mcp.handlers.{handler_calls[0]}"
            if len(handler_calls) == 1
            else f"{function.__module__}.{function.__name__}"
        )
        result.append(
            {
                "name": tool.name,
                "permission": TOOL_PERMISSIONS[tool.name].name,
                "delegate": delegate,
                # The published verb-to-tool join, per tool, so an overlay reads
                # it instead of inferring it from the two names matching.
                "facade_operations": sorted(
                    {
                        operation
                        for handler in handler_calls
                        for operation in handler_operations.get(handler, ())
                    }
                ),
                "input_schema_digest": _schema_digest(tool.parameters),
                "output_schema_digest": _schema_digest(tool.output_schema),
            }
        )
    return sorted(result, key=lambda row: str(row["name"]))


def _cli_client_calls(callback: Callable[..., object]) -> list[str]:
    from cruxible_client import CruxibleClient

    public_methods = {
        name
        for name, value in vars(CruxibleClient).items()
        if callable(value) and not name.startswith("_")
    }
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(callback)))
    tree = ast.parse(source)
    return sorted(
        {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in public_methods
        }
    )


def _cli_surface() -> list[dict[str, object]]:
    import click

    from cruxible_core.cli.main import cli

    result: list[dict[str, object]] = []

    def visit(command: click.Command, path: tuple[str, ...]) -> None:
        loader = getattr(command, "_load", None)
        loaded = loader() if callable(loader) else command
        if isinstance(loaded, click.Group):
            for name, child in sorted(loaded.commands.items()):
                visit(child, (*path, name))
            return
        callback = loaded.callback
        if callback is None:
            raise RuntimeError(f"CLI leaf {' '.join(path)!r} has no callback")
        unwrapped = inspect.unwrap(callback)
        result.append(
            {
                "command": " ".join(path),
                "delegate": f"{unwrapped.__module__}.{unwrapped.__qualname__}",
                "client_operations": _cli_client_calls(unwrapped),
            }
        )

    visit(cli, ())
    return sorted(result, key=lambda row: str(row["command"]))


def generate_served_surface() -> dict[str, object]:
    """Discover every frozen v1 public entry and its delegation metadata."""
    return {
        "facade_verbs": _facade_operations(),
        "http_routes": _http_surface(),
        "mcp_tools": _mcp_surface(),
        "mcp_facade_operations": _mcp_facade_operations(),
        "cli_leaves": _cli_surface(),
    }


def verify_served_surface_snapshot(
    snapshot: Mapping[str, object],
    *,
    live_surface: Mapping[str, object] | None = None,
) -> None:
    """Refuse malformed succession evidence or any drift from the live surface."""
    if snapshot.get("format") != FORMAT:
        raise ValueError(f"served-surface format must be {FORMAT!r}")
    succession = snapshot.get("succession")
    if not isinstance(succession, Mapping) or set(succession) != {
        "register_entry",
        "ratified_by",
        "surface_digest",
    }:
        raise ValueError("served-surface succession block has the wrong shape")
    register_entry = succession.get("register_entry")
    if not isinstance(register_entry, str) or _REGISTER_ENTRY_RE.fullmatch(register_entry) is None:
        raise ValueError("served-surface succession register_entry is invalid")
    if succession.get("ratified_by") != RATIFIED_BY:
        raise ValueError(f"served-surface succession must be ratified by {RATIFIED_BY}")
    surface = snapshot.get("surface")
    if not isinstance(surface, Mapping):
        raise ValueError("served-surface snapshot has no surface object")
    if succession.get("surface_digest") != surface_digest(surface):
        raise ValueError("served-surface succession digest does not match its surface")
    expected = generate_served_surface() if live_surface is None else live_surface
    if surface != expected:
        raise ValueError("live Playbill served surface differs from the ratified v1 inventory")


def _render(snapshot: Mapping[str, object]) -> bytes:
    return (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode()


def update_snapshot(path: Path = SNAPSHOT, *, succession: str | None) -> bool:
    """Write a moved surface only when a ratified register entry is supplied."""
    live_surface = generate_served_surface()
    existing: Mapping[str, object] | None = None
    if path.exists():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, Mapping):
            existing = parsed
    if existing is not None and existing.get("surface") == live_surface:
        verify_served_surface_snapshot(existing, live_surface=live_surface)
        return False
    if succession is None:
        raise ValueError("served-surface movement requires --succession <register-entry-id>")
    if _REGISTER_ENTRY_RE.fullmatch(succession) is None:
        raise ValueError("--succession must be a stable lower-case register-entry id")
    snapshot = {
        "format": FORMAT,
        "succession": {
            "register_entry": succession,
            "ratified_by": RATIFIED_BY,
            "surface_digest": surface_digest(live_surface),
        },
        "surface": live_surface,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_render(snapshot))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--succession", help="Ratified register-entry id authorizing movement.")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    args = parser.parse_args(argv)
    try:
        changed = update_snapshot(args.snapshot, succession=args.succession)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    state = "Wrote" if changed else "Verified"
    try:
        display_path = args.snapshot.relative_to(REPO_ROOT)
    except ValueError:
        display_path = args.snapshot
    print(f"{state} {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
