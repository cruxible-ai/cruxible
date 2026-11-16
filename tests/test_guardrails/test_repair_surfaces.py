"""Every rendered repair is checked against the real surface it names.

The spelling table (``REPAIR_SPELLINGS``) says how each served operation is
spelled on each client; these guards read the real surfaces instead of
trusting it:

(a) controls -- every argument a producer can put on a repair, and the preview
    control of the real command, MCP tool or SDK method, is spelled on each
    client that renders a form: a ``dry_run=True`` repair renders as that
    client's preview and a ``dry_run=False`` one as its commit;
(b) shapes -- a line rendered from representative values (two-element lists
    for list arguments) binds: the CLI line parses with click into the same
    values, the MCP call names the tool's arguments with the schema's shape,
    the SDK call binds to the method's signature;
(c) placeholders -- a line rendered with its required operands missing, run
    unchanged on its own client against a recording daemon, refuses, and no
    request carrying a placeholder leaves the client.

Producer arguments are collected from every ``operation="..."`` producer in
the source; an argument a producer computes (a dict splat) cannot be read
statically and is named in ``COMPUTED_PRODUCERS``. Request-model fields enter
through the real surface's own parameters: the preview control is read from
the command, tool and method themselves.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import shlex
import typing
from collections import defaultdict
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from cruxible_client.contracts.placeholders import (
    PLACEHOLDER_PATTERN,
    RENDERED_PLACEHOLDERS,
    is_placeholder,
)
from cruxible_client.contracts.repair_spelling import (
    REPAIR_SPELLINGS,
    RepairSpelling,
    declared_keys,
    render_repair,
)
from cruxible_client.contracts.repairs import REPAIR_COMMAND_PATHS, RepairOperation
from cruxible_core.cli.main import cli

_ROOT = Path(__file__).resolve().parents[2]
_SURFACES = ("cli", "mcp", "sdk")
_NOT_REPAIR_OPERATIONS = frozenset(
    {
        "get",
        "hand_edit",
        "preview",
        "playbill_authoring_submit",
        "playbill.claim.get",
        "playbill.procedure.run.resolve",
        "playbill.query.run",
    }
)

#: Producers a static read cannot resolve -- a computed operation name, or
#: arguments that are not one literal dict -- named by file and function, with
#: every operation each can produce and the keys it can carry. ``CHANGE`` stands
#: for the change operation a change-control refusal repairs (each found where
#: its change runs). The scan fails on any unresolved producer missing here.
CHANGE = "<change operation>"
COMPUTED_PRODUCERS: Mapping[tuple[str, str], Mapping[str, frozenset[str]]] = {
    ("src/cruxible_core/errors.py", "ChangeRefusedError.__init__"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/change_preview.py", "ChangeMode.refuse_moved_head"): {
        CHANGE: frozenset({"dry_run"})
    },
    # Change control passing its caller's operation through: each caller's own
    # literal operation is scanned where it runs.
    ("src/cruxible_core/service/change_preview.py", "ChangeMode.confirm_head"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/change_preview.py", "StateChange.observe"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/change_preview.py", "_pin"): {CHANGE: frozenset({"dry_run"})},
    ("src/cruxible_core/service/change_preview.py", "change_scope"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/change_preview.py", "state_change_scope"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/authoring/documents.py", "admit_proposal"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/service/discovery/curation.py", "_record_ruling"): {
        CHANGE: frozenset({"dry_run"})
    },
    ("src/cruxible_core/server/errors.py", "_repair_for_error"): {
        "credential.mint": frozenset(
            {"refused_operation", "credential_env", "accepted_credentials", "credential_options"}
        ),
        "credential.claim-bootstrap": frozenset({"instance_id"}),
        "credential.recover-admin": frozenset({"instance_id"}),
    },
    ("src/cruxible_core/service/identity.py", "credential_unbound_refusal"): {
        "credential.mint": frozenset({"unbound_credential_id"})
    },
    ("src/cruxible_core/service/kits.py", "_require_staged"): {
        "cruxible.kit.build": frozenset(),
        "cruxible.kit.add": frozenset(),
    },
    (
        "packages/cruxible-client/src/cruxible_client/contracts/errors.py",
        "ApprovalRequirementUnmet.__init__",
    ): {"cruxible.proposal.approve": frozenset({"proposal_id", "signer_id"})},
    ("src/cruxible_core/service/authoring/landing.py", "_stopped"): {
        "cruxible.proposal.approve": frozenset({"proposal_id", "signer_id"})
    },
    ("src/cruxible_core/service/discovery/get.py", "_body"): {
        "cruxible.get": frozenset({"ref", "detail", "range"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_restating_repair"): {
        "cruxible.authoring.bind": frozenset({"claim_id", "citation_id", "source_id"}),
        "cruxible.authoring.example": frozenset({"example", "claim_id", "capture_digest"}),
        "cruxible.set": frozenset(
            {"subject", "field", "value", "role", "capture_digest", "evidence_contract", "claim_id"}
        ),
    },
    ("src/cruxible_core/service/discovery/next.py", "_evidence_repair"): {
        "cruxible.set": frozenset(
            {"subject", "field", "value", "role", "capture_digest", "evidence_contract", "claim_id"}
        )
    },
    ("src/cruxible_core/service/discovery/next.py", "_contradicting_capture_repair"): {
        "cruxible.set": frozenset(
            {"subject", "field", "value", "role", "capture_digest", "evidence_contract", "claim_id"}
        )
    },
    ("src/cruxible_core/service/discovery/next.py", "_claim_slot_rows"): {
        "cruxible.authoring.example": frozenset({"example", "claim_id", "capture_digest"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_contest_repair"): {
        "cruxible.authoring.example": frozenset({"example", "claim_id", "capture_digest"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_citation_drift_item"): {
        "cruxible.rebind": frozenset({"claim_id", "citation", "section_file", "section"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_evidence_erased_items"): {
        "cruxible.rebind": frozenset({"claim_id", "citation"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_consumer_stalled_items"): {
        # Each consumer's health repair (ConsumerRepair, scanned with the rest).
        "cruxible.floor.export": frozenset({"mode"}),
        "cruxible.line.enable": frozenset({"line"}),
        "cruxible.line.dispatch": frozenset({"line"}),
    },
    ("src/cruxible_core/service/discovery/next.py", "_kit_resume_items"): {
        "cruxible.kit.add": frozenset({"reference"})
    },
    ("src/cruxible_core/service/discovery/next.py", "_projection_marker_invalid_item"): {
        "cruxible.block.depublish": frozenset({"source_id", "block_id"}),
        "cruxible.block.repin": frozenset({"source_id", "block_id"}),
    },
    ("src/cruxible_core/service/discovery/next.py", "_projection_items"): {
        "cruxible.block.repin": frozenset({"source_id", "block_id", "claim", "clear_claims"}),
        "cruxible.block.depublish": frozenset({"source_id", "block_id"}),
    },
    (
        "packages/cruxible-client/src/cruxible_client/authoring/blocks.py",
        "_sync_item_from_read_refusal",
    ): {"cruxible.block.repin": frozenset({"source_id", "block_id", "claim", "clear_claims"})},
    ("packages/cruxible-client/src/cruxible_client/authoring/vocabulary_file.py", "_call"): {
        "cruxible.vocabulary.export": frozenset({"force"}),
        "cruxible.vocabulary.plan": frozenset(),
        "cruxible.vocabulary.apply": frozenset(),
    },
}

#: Operations class (c) cannot execute, with why. Reviewed in the report.
PLACEHOLDER_EXECUTION_EXCEPTIONS: Mapping[tuple[str, str], str] = {}


# -- producers ---------------------------------------------------------------------------


_REPAIR_CALLS = {"RepairOperation", "PlaybillNextRepairV1", "NextRepair", "ConsumerRepair"}
_CHANGE_CALLS = {"change_scope", "state_change_scope", "ChangeMode", "_pin", "ChangeRefusedError"}


def _producers() -> Iterator[tuple[str, str, ast.Call, str | None]]:
    """Every call that names an operation: (file, function, call, callee name)."""

    for base in (_ROOT / "src", _ROOT / "packages" / "cruxible-client" / "src"):
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(_ROOT).as_posix()

            def walk(
                node: ast.AST, names: tuple[str, ...]
            ) -> Iterator[tuple[str, str, ast.Call, str | None]]:
                for child in ast.iter_child_nodes(node):
                    scope = names
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                        scope = (*names, child.name)
                    if isinstance(child, ast.Call) and any(
                        item.arg == "operation" for item in child.keywords
                    ):
                        func = child.func
                        callee = (
                            func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                        )
                        yield relative, ".".join(scope), child, callee
                    yield from walk(child, scope)

            yield from walk(ast.parse(path.read_text(encoding="utf-8")), ())


@cache
def producer_keys() -> Mapping[str, frozenset[str]]:
    """Every argument key a producer in the source can put on each operation's repair."""

    found: dict[str, set[str]] = defaultdict(set)
    unresolved: list[str] = []
    for relative, function, call, callee in _producers():
        kw = {item.arg: item.value for item in call.keywords if item.arg}
        op = kw["operation"]
        literal = isinstance(op, ast.Constant) and isinstance(op.value, str)
        is_repair = callee in _REPAIR_CALLS
        is_change = callee in _CHANGE_CALLS or "dry_run" in kw
        if not (is_repair or is_change):
            continue
        arguments = kw.get("arguments")
        computed = not literal or (
            is_repair
            and arguments is not None
            and not (isinstance(arguments, ast.Dict) and None not in arguments.keys)
        )
        if computed:
            if (relative, function) not in COMPUTED_PRODUCERS:
                unresolved.append(f"{relative}::{function} ({callee})")
            continue
        assert isinstance(op, ast.Constant)
        if is_repair:
            found[op.value]
            if isinstance(arguments, ast.Dict):
                found[op.value].update(
                    key.value
                    for key in arguments.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
        elif "repair" not in kw:
            # A change's refusal repairs it with a preview or a retry.
            found[op.value].add("dry_run")
    assert not unresolved, (
        "producers the scan cannot resolve; name each in COMPUTED_PRODUCERS:\n"
        + "\n".join(unresolved)
    )
    for entries in COMPUTED_PRODUCERS.values():
        for operation, keys in entries.items():
            if operation != CHANGE:
                found[operation].update(keys)
    return {
        operation: frozenset(keys)
        for operation, keys in found.items()
        if operation not in _NOT_REPAIR_OPERATIONS
    }


def test_every_producer_resolves_or_is_named() -> None:
    """Review round 4, finding 3: no producer is skipped silently."""

    keys = producer_keys()
    assert keys["cruxible.vocabulary.export"] >= {"force"}
    stale = [
        f"{path}::{function}"
        for path, function in COMPUTED_PRODUCERS
        if not any(p == path and f == function for p, f, _c, _n in _producers())
    ]
    assert not stale, f"COMPUTED_PRODUCERS names producers that no longer exist: {stale}"


def test_every_renderable_operation_has_a_declared_spelling() -> None:
    """Composer-only operations are checked like the rest (finding 3)."""

    assert set(REPAIR_COMMAND_PATHS) <= set(REPAIR_SPELLINGS)
    assert set(producer_keys()) <= set(REPAIR_SPELLINGS)


def _operations() -> list[str]:
    return sorted(set(REPAIR_SPELLINGS) | set(REPAIR_COMMAND_PATHS) | set(producer_keys()))


# -- real surfaces -----------------------------------------------------------------------


@cache
def _cli_command(path: str) -> click.Command:
    ctx = click.Context(cli)
    command: click.Command = cli
    for name in path.split():
        assert isinstance(command, click.Group), path
        found = command.get_command(ctx, name)
        assert found is not None, f"no CLI command `cruxible {path}`"
        command, ctx = found, click.Context(found, parent=ctx)
    return command


def _cli_path(operation: str) -> str | None:
    spelling = REPAIR_SPELLINGS.get(operation)
    if spelling is not None:
        return spelling.cli
    return REPAIR_COMMAND_PATHS.get(operation)


@cache
def _mcp_tools() -> Mapping[str, Any]:
    import os

    from cruxible_core.mcp.server import create_server

    previous = os.environ.get("CRUXIBLE_MCP_PROFILE")
    os.environ["CRUXIBLE_MCP_PROFILE"] = "full"
    try:
        server = create_server()
    finally:
        if previous is None:
            os.environ.pop("CRUXIBLE_MCP_PROFILE", None)
        else:
            os.environ["CRUXIBLE_MCP_PROFILE"] = previous
    return dict(server._tool_manager._tools)


def _schema_ref(schema: Mapping[str, Any], prop: Mapping[str, Any]) -> Mapping[str, Any] | None:
    ref = prop.get("$ref") or next(
        (item["$ref"] for item in prop.get("anyOf", ()) if "$ref" in item), None
    )
    return None if ref is None else schema["$defs"][ref.rsplit("/", 1)[1]]


def _is_array(prop: Mapping[str, Any]) -> bool:
    if prop.get("type") == "array":
        return True
    return any(item.get("type") == "array" for item in prop.get("anyOf", ()))


def _mcp_has_preview(tool: str) -> bool:
    schema = _mcp_tools()[tool].parameters
    props = schema.get("properties", {})
    if "dry_run" in props:
        return True
    request = props.get("request")
    model = None if request is None else _schema_ref(schema, request)
    return model is not None and "dry_run" in model.get("properties", {})


def _cli_has_preview(path: str) -> click.Option | None:
    for param in _cli_command(path).params:
        if param.name == "dry_run" and isinstance(param, click.Option):
            return param
    return None


# -- SDK resolution ----------------------------------------------------------------------


class _Opaque:
    """A value the SDK line names but this check does not evaluate (SIGNER, a nested call)."""


def _literal(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return _Opaque()


def _mcp_literal(node: ast.expr) -> object:
    """An MCP call's argument: JSON values, so ``true``, ``false`` and ``null`` are names."""

    names = {"true": True, "false": False, "null": None}
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.List):
        return [_mcp_literal(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _mcp_literal(key): _mcp_literal(value)  # type: ignore[arg-type]
            for key, value in zip(node.keys, node.values, strict=True)
        }
    return ast.literal_eval(node)


def _hints(function: Any) -> dict[str, Any]:
    module = inspect.getmodule(function)
    try:
        return typing.get_type_hints(function, vars(module) if module else None)
    except Exception:  # noqa: BLE001 - a forward reference the module cannot resolve
        return {}


@cache
def _sdk_classes() -> Mapping[str, type]:
    """Every class the SDK's authoring modules define, by name, for string annotations."""

    import importlib
    import pkgutil

    import cruxible_client.authoring as package

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for name, value in vars(module).items():
            if inspect.isclass(value):
                found.setdefault(name, value)
    return found


def _returned(member: Any, attribute: str | None = None) -> Any:
    """The class a property or method returns, picking the union member with ``attribute``."""

    function = member.fget if isinstance(member, property) else member
    returned = _hints(function).get("return")
    if returned is None:
        annotation = inspect.signature(function).return_annotation
        names = [part.strip() for part in str(annotation).split("|")]
        returned = typing.Union[
            tuple(_sdk_classes()[name] for name in names if name in _sdk_classes())
        ]  # noqa: UP007
    options = typing.get_args(returned) or (returned,)
    for option in options:
        if inspect.isclass(option) and (attribute is None or hasattr(option, attribute)):
            return option
    return options[0]


def _sdk_call(line: str) -> tuple[Any, list[object], dict[str, object]]:
    """The callable an SDK line ends in, with its literal arguments."""

    import importlib

    from cruxible_client.authoring.sdk import Cruxible

    # A line is self-contained: it may import what it calls, then make one call.
    *setup, last = ast.parse(line, mode="exec").body
    imported: dict[str, Any] = {}
    for statement in setup:
        assert isinstance(statement, ast.ImportFrom) and statement.module, line
        module = importlib.import_module(statement.module)
        for alias in statement.names:
            imported[alias.asname or alias.name] = getattr(module, alias.name)
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), line
    expression = last.value

    def resolve(node: ast.expr, attribute: str | None) -> Any:
        """The class this expression evaluates to (``attribute`` picks among a union)."""

        if isinstance(node, ast.Name):
            assert node.id == "cx", line
            return Cruxible
        if isinstance(node, ast.Attribute):
            owner = resolve(node.value, node.attr)
            member = inspect.getattr_static(owner, node.attr)
            if isinstance(member, property):
                return _returned(member, attribute)
            raise AssertionError(f"{line}: {node.attr} is called, not read")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = resolve(node.func.value, node.func.attr)
            return _returned(inspect.getattr_static(owner, node.func.attr), attribute)
        raise AssertionError(f"{line}: cannot resolve {ast.dump(node)}")

    func = expression.func
    if isinstance(func, ast.Name):
        assert func.id in imported, f"{line}: {func.id} is neither cx nor imported by the line"
        target: Any = imported[func.id]
    else:
        assert isinstance(func, ast.Attribute), line
        owner = resolve(func.value, func.attr)
        target = inspect.getattr_static(owner, func.attr)
        assert callable(target), line
    args = [_literal(item) for item in expression.args]
    kwargs = {item.arg: _literal(item.value) for item in expression.keywords if item.arg}
    return target, args, kwargs


def _sdk_bind(line: str) -> inspect.BoundArguments:
    target, args, kwargs = _sdk_call(line)
    signature = inspect.signature(target)
    takes_self = next(iter(signature.parameters), None) == "self"
    return signature.bind(*((None,) if takes_self else ()), *args, **kwargs)


def _sdk_has_preview(line: str) -> bool:
    target, _args, _kwargs = _sdk_call(line)
    return "dry_run" in inspect.signature(target).parameters


# -- representative values from the real surfaces -------------------------------------------


def _cli_param(spelling: RepairSpelling, key: str) -> click.Parameter | None:
    if spelling.cli is None:
        return None
    command = _cli_command(spelling.cli)
    arguments = [param for param in command.params if isinstance(param, click.Argument)]
    if key in spelling.cli_args:
        index = spelling.cli_args.index(key)
        return arguments[index] if index < len(arguments) else None
    flag = spelling.cli_flags.get(key)
    if flag is None:
        return None
    first = flag.partition("/")[0]
    return next(
        (
            param
            for param in command.params
            if isinstance(param, click.Option) and first in param.opts
        ),
        None,
    )


def _mcp_prop(spelling: RepairSpelling, key: str) -> Mapping[str, Any] | None:
    if spelling.mcp is None or key not in spelling.mcp_args:
        return None
    schema = _mcp_tools()[spelling.mcp].parameters
    name = spelling.mcp_args[key]
    top, _, nested = name.partition(".")
    prop = schema.get("properties", {}).get(top)
    if prop is None or not nested:
        return prop
    model = _schema_ref(schema, prop)
    return None if model is None else model.get("properties", {}).get(nested)


def _value_for(spelling: RepairSpelling, key: str) -> object:
    param = _cli_param(spelling, key)
    prop = _mcp_prop(spelling, key) or {}
    if key == "dry_run":
        return True
    listed = (
        param is not None and (getattr(param, "multiple", False) or param.nargs == -1)
    ) or _is_array(prop)
    if listed:
        return [f"{key}-a", f"{key}-b"]
    if (isinstance(param, click.Option) and param.is_flag) or prop.get("type") == "boolean":
        return True
    if param is None and (prop.get("$ref") or prop.get("type") == "object"):
        return {}
    if param is not None and isinstance(param.type, click.Choice):
        return str(param.type.choices[0])
    if (param is not None and isinstance(param.type, click.types._NumberParamTypeBase)) or (
        prop.get("type") == "integer"
    ):
        return 2
    return f"{key}-value"


def _sample(spelling: RepairSpelling, keys: set[str]) -> dict[str, object]:
    return {key: _value_for(spelling, key) for key in keys}


def _own_form(line: str) -> bool:
    return "has no form for" not in line and not line.startswith(
        ("cruxible.", "credential.", "server.")
    )


# -- (a) controls ------------------------------------------------------------------------


def _surface_keys(spelling: RepairSpelling | None, surface: str) -> frozenset[str]:
    if spelling is None:
        return frozenset()
    keys = declared_keys(spelling, surface)  # type: ignore[arg-type]
    if spelling.composed_first:
        keys |= spelling.composed_keys
    return keys | spelling.context


def _real_preview(spelling: RepairSpelling, surface: str, line: str) -> bool:
    if surface == "cli":
        return spelling.cli is not None and _cli_has_preview(spelling.cli) is not None
    if surface == "mcp":
        return spelling.mcp is not None and _mcp_has_preview(spelling.mcp)
    return _sdk_has_preview(line)


@pytest.mark.parametrize("operation", _operations())
def test_every_argument_a_producer_carries_is_spelled_on_each_client(operation: str) -> None:
    """(a) A client that renders a form spells every operand and control a repair carries."""

    spelling = REPAIR_SPELLINGS.get(operation)
    carried = set(producer_keys().get(operation, ()))
    if spelling is None:
        assert not carried, f"{operation} carries {sorted(carried)} but has no declared spelling"
        return
    required = {key: _value_for(spelling, key) for key in spelling.required}
    for surface in _SURFACES:
        line = render_repair(
            RepairOperation(operation=operation, arguments=required),
            surface=surface,  # type: ignore[arg-type]
        )
        if not _own_form(line):
            continue
        wanted = set(carried)
        if "dry_run" in wanted and not _real_preview(spelling, surface, line):
            # No preview on the real surface: a commit is its only form, and the
            # renderer gives a preview repair no form here (class (a) checks it).
            wanted.discard("dry_run")
        missing = wanted - _surface_keys(spelling, surface)
        assert not missing, f"{operation} on {surface}: producers carry {sorted(missing)}"


@pytest.mark.parametrize("operation", sorted(REPAIR_SPELLINGS))
def test_a_preview_and_a_commit_render_as_the_real_surface_spells_them(operation: str) -> None:
    """(a) The real command, tool or method decides whether there is a preview."""

    spelling = REPAIR_SPELLINGS[operation]
    required = {key: _value_for(spelling, key) for key in spelling.required}
    for surface in _SURFACES:
        for dry_run in (True, False):
            arguments = {**required, "dry_run": dry_run}
            line = render_repair(
                RepairOperation(operation=operation, arguments=arguments),
                surface=surface,  # type: ignore[arg-type]
            )
            own = _own_form(line)
            if surface == "cli" and spelling.cli is not None:
                option = _cli_has_preview(spelling.cli)
                if option is None or not own:
                    if option is not None and dry_run:
                        assert own or "(on the" in line, (operation, line)
                    continue
                assert "dry_run" in spelling.cli_flags, f"{operation}: CLI has a preview"
                tokens = line.split()
                if dry_run:
                    assert "--dry-run" in tokens, (operation, line)
                elif option.secondary_opts:
                    assert "--commit" in tokens, (operation, line)
                else:
                    assert "--dry-run" not in tokens, (operation, line)
            elif surface == "mcp" and spelling.mcp is not None:
                if not _mcp_has_preview(spelling.mcp) or not own:
                    continue
                assert "dry_run" in spelling.mcp_args, f"{operation}: the tool has a preview"
                wanted = (
                    ("dry_run=true", '"dry_run": true')
                    if dry_run
                    else (
                        "dry_run=false",
                        '"dry_run": false',
                    )
                )
                assert any(marker in line for marker in wanted), (operation, line)
            elif surface == "sdk" and spelling.sdk is not None and own:
                if not _sdk_has_preview(line):
                    continue
                target, _a, _k = _sdk_call(line)
                bound = _sdk_bind(line)
                default = inspect.signature(target).parameters["dry_run"].default
                assert bound.arguments.get("dry_run", default) is dry_run, (operation, line)


def test_a_line_evaluation_preview_never_dispatches() -> None:
    """Review round 3, finding 1."""

    evaluate = RepairOperation(
        operation="cruxible.line.evaluate",
        arguments={
            "line": "work",
            "since": "2026-11-15T00:00:00Z",
            "until": "2026-11-16T00:00:00Z",
            "dry_run": True,
        },
    )
    cli_line = render_repair(evaluate, surface="cli")
    assert "--dry-run" in cli_line.split() and "--dispatch" not in cli_line.split()
    assert '"dry_run": true' in render_repair(evaluate, surface="mcp")
    assert "dry_run=True" in render_repair(evaluate, surface="sdk")


# -- (b) shapes --------------------------------------------------------------------------


def _expected_cli(param: click.Parameter, value: object) -> object:
    if value is False and isinstance(param, click.Option) and not param.secondary_opts:
        return None  # a commit is the flag left out
    if isinstance(value, list):
        return tuple(value) if isinstance(param, click.Argument) else value
    if isinstance(value, bool):
        return value
    return str(value)


@pytest.mark.parametrize("operation", sorted(REPAIR_SPELLINGS))
def test_every_spelling_binds_to_the_real_surface(operation: str) -> None:
    """(b) Representative values, lists as two elements, bind to each client's real surface."""

    spelling = REPAIR_SPELLINGS[operation]
    for surface in _SURFACES:
        keys = set(declared_keys(spelling, surface))  # type: ignore[arg-type]
        if not keys:
            continue
        required = _sample(spelling, set(spelling.required) & keys)
        # All arguments at once, then each alone (beside the required ones), so a
        # branch one argument hides (cleared Claims over named ones) is bound too.
        cases = [_sample(spelling, keys)]
        cases.extend({**required, key: _value_for(spelling, key)} for key in sorted(keys))
        if "dry_run" in keys:
            cases.append({**required, "dry_run": False})
        for values in cases:
            _check_shape(spelling, surface, values, operation)


def _check_shape(
    spelling: RepairSpelling, surface: str, values: dict[str, object], operation: str
) -> None:
    line = render_repair(
        RepairOperation(operation=operation, arguments=values),
        surface=surface,  # type: ignore[arg-type]
    )
    assert _own_form(line), (operation, surface, values, line)
    if surface == "cli":
        assert spelling.cli is not None
        command = _cli_command(spelling.cli)
        tokens = shlex.split(line)[1 + len(spelling.cli.split()) :]
        if "<<<" in tokens:
            tokens = tokens[: tokens.index("<<<")]  # the payload travels on stdin
        parsed, extra, _order = command.make_parser(click.Context(command)).parse_args(tokens)
        assert not extra, (operation, line, extra)
        for key, value in values.items():
            if key == spelling.cli_stdin:
                assert "-" in parsed.values() or parsed.get("file") == "-", (operation, line)
                continue
            param = _cli_param(spelling, key)
            assert param is not None and param.name is not None, (operation, key)
            assert parsed.get(param.name) == _expected_cli(param, value), (
                operation,
                key,
                parsed.get(param.name),
                line,
            )
        for param in command.params:
            if param.required and param.name not in parsed:
                raise AssertionError(f"{operation}: {line} misses required {param.name}")
    elif surface == "mcp":
        assert spelling.mcp is not None
        _check_mcp_shape(spelling.mcp, line, operation)
    else:
        bound = _sdk_bind(line)
        target, _a, _k = _sdk_call(line)
        parameters = inspect.signature(target).parameters
        for name, value in bound.arguments.items():
            if parameters[name].kind is inspect.Parameter.VAR_POSITIONAL:
                assert all(not isinstance(item, list) for item in value), (operation, line)


def _check_mcp_shape(tool: str, line: str, operation: str) -> None:
    schema = _mcp_tools()[tool].parameters
    call = ast.parse(line, mode="eval").body
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == tool, line
    kwargs = {item.arg: _mcp_literal(item.value) for item in call.keywords if item.arg}
    props = schema.get("properties", {})
    for name, value in kwargs.items():
        assert name in props, f"{operation}: {tool} takes no {name}"
        if name == "request":
            model = _schema_ref(schema, props[name])
            assert model is not None and isinstance(value, dict), (operation, line)
            for field, item in value.items():
                prop = model.get("properties", {}).get(field)
                assert prop is not None, f"{operation}: request has no {field}"
                assert isinstance(item, list) == _is_array(prop), (operation, field, line)
            continue
        assert isinstance(value, list) == _is_array(props[name]), (operation, name, line)
    missing = set(schema.get("required", ())) - set(kwargs) - {"instance_id"}
    assert not missing, f"{operation}: {tool} also requires {sorted(missing)}"


def test_the_fallback_spellings_take_each_surfaces_argument_shape() -> None:
    """Review round 3, findings 3 and 4."""

    repin = RepairOperation(
        operation="cruxible.block.repin",
        arguments={
            "source_id": "status",
            "block_id": "summary",
            "claim": ["CLM-1", "CLM-2"],
            "dry_run": True,
        },
    )
    assert render_repair(repin, surface="sdk") == (
        'cx.block.repin("status", "summary", claims=["CLM-1", "CLM-2"], dry_run=True)'
    )
    detach = RepairOperation(
        operation="cruxible.block.detach",
        arguments={"paths": ["one.md", "two.md"], "dry_run": True},
    )
    assert render_repair(detach, surface="cli") == "cruxible block detach one.md two.md --dry-run"
    assert render_repair(detach, surface="sdk") == (
        'cx.block.detach("one.md", "two.md", dry_run=True)'
    )


# -- (c) placeholders --------------------------------------------------------------------

#: Bare names an SDK line uses for an object only the caller holds; running the
#: line unchanged raises NameError on exactly these.
SDK_BARE_PLACEHOLDERS = frozenset({"SIGNER", "INPUT"})


#: The caller's own words: the operands whose placeholder a field check refuses.
_CALLER_TEXT = {"because": "A reviewer read the source.", "reason": "Reviewed.", "name": "fix-it"}


def _cases(spelling: RepairSpelling) -> list[dict[str, object]]:
    """Every operand missing; then the caller's words given, everything else missing."""

    given = {key: text for key, text in _CALLER_TEXT.items() if key in spelling.required}
    return [{}, given] if given else [{}]


def _placeholder_lines() -> Iterator[tuple[str, str, str]]:
    for operation, spelling in sorted(REPAIR_SPELLINGS.items()):
        if not spelling.required:
            continue
        for arguments in _cases(spelling):
            for surface in _SURFACES:
                line = render_repair(
                    RepairOperation(operation=operation, arguments=arguments),
                    surface=surface,  # type: ignore[arg-type]
                )
                if _own_form(line) and PLACEHOLDER_PATTERN.search(line):
                    yield operation, surface, line


def _is_preview(line: str, surface: str) -> bool:
    if surface == "cli":
        return "--dry-run" in line.split()
    markers = ("dry_run=true", '"dry_run": true') if surface == "mcp" else ("dry_run=True",)
    return any(marker in line for marker in markers)


@pytest.mark.parametrize("operation", sorted(REPAIR_SPELLINGS))
def test_a_line_left_with_only_content_placeholders_is_a_preview(operation: str) -> None:
    """(c) Every refusing operand given (the caller's words, real identifiers), every
    opaque one missing: nothing refuses that placeholder, so the line only previews."""

    spelling = REPAIR_SPELLINGS[operation]
    opaque = set(spelling.opaque) & set(spelling.required)
    if not opaque:
        return
    values: dict[str, object] = {
        key: _CALLER_TEXT.get(key, _value_for(spelling, key))
        for key in spelling.required
        if key not in opaque
    }
    for surface in _SURFACES:
        if not opaque & declared_keys(spelling, surface):  # type: ignore[arg-type]
            continue  # this client takes no such content operand (a file it reads instead)
        line = render_repair(
            RepairOperation(operation=operation, arguments=values),
            surface=surface,  # type: ignore[arg-type]
        )
        if _own_form(line):
            assert _is_preview(line, surface), (operation, surface, line)


def test_a_value_left_out_of_a_reasoned_set_only_previews() -> None:
    """Review round 4 follow-up: the reviewer's reproducer, on every client."""

    repair = RepairOperation(
        operation="cruxible.set",
        arguments={
            "subject": "dev.item/a",
            "field": "status",
            "because": "The source now reports this status.",
        },
    )
    assert render_repair(repair, surface="cli") == (
        "cruxible set dev.item/a status '<VALUE>' --dry-run "
        "--because 'The source now reports this status.'"
    )
    assert render_repair(repair, surface="mcp") == (
        'cruxible_set(dry_run=true, subject="dev.item/a", field="status", value="<VALUE>", '
        'because="The source now reports this status.")'
    )
    assert render_repair(repair, surface="sdk") == (
        'cx.set("dev.item/a", "status", "<VALUE>", '
        'because="The source now reports this status.", dry_run=True)'
    )


def test_the_renderer_emits_only_the_registered_placeholders() -> None:
    """The field check refuses exactly these tokens, so none may escape the list."""

    emitted = {
        token
        for _operation, _surface, line in _placeholder_lines()
        for token in PLACEHOLDER_PATTERN.findall(line)
    }
    assert emitted <= RENDERED_PLACEHOLDERS, sorted(emitted - RENDERED_PLACEHOLDERS)


def test_every_operation_with_a_required_operand_emits_a_placeholder_somewhere() -> None:
    """A new operation with a required operand is covered by class (c) automatically."""

    covered = {operation for operation, _surface, _line in _placeholder_lines()}
    for operation, spelling in REPAIR_SPELLINGS.items():
        if spelling.required:
            assert operation in covered, f"{operation} renders no placeholder on any client"


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str, Path]]:
    """A real initialized daemon behind a client every surface shares."""

    from tests.test_server.conftest import _playbill_http

    from cruxible_client import CruxibleClient

    served = _playbill_http(tmp_path, monkeypatch)
    test_client, instance_id, reviewer_key = next(served)
    _REFUSED.clear()

    def record(response: httpx.Response) -> None:
        # The daemon refusing a request that carries a placeholder (a lookup or
        # a format check on the operand) is that operand's own refusal.
        request = response.request
        text = f"{request.url.path} {request.content.decode('utf-8', 'replace')}"
        if 400 <= response.status_code < 500 and PLACEHOLDER_PATTERN.search(text):
            _REFUSED.append(response.status_code)

    test_client.event_hooks["response"].append(record)
    client = CruxibleClient(base_url="http://testserver")
    client._client._client = test_client  # type: ignore[attr-defined]  # noqa: SLF001
    try:
        yield client, instance_id, reviewer_key
    finally:
        served.close()


#: 4xx answers the daemon gave to requests carrying a placeholder, this case.
_REFUSED: list[int] = []


def _head(instance_id: str) -> str:
    from cruxible_core.runtime.playbill_manager import get_playbill_manager

    return get_playbill_manager().get(instance_id).accepted_coordinate().git_oid


#: A served refusal code (``cruxible.write.unknown_kind``, ``runtime_credential.auth_off``).
_CODE = re.compile(r"\b[a-z_]+\.[a-z_]+(?:\.[a-z_]+)*: ")
#: Request or field validation, as each client words it.
_VALIDATION = (
    "invalid input",
    "invalid arguments",
    "Invalid value",
    "validation failed",
    "Value error",
)
#: A typed client refusal, as the CLI prints one (``Error: ProviderFormatError: ...``).
_TYPED = re.compile(r"^Error: [A-Z][A-Za-z]+(?:Error|Refused|Refusal): ")


def _message_refuses(message: str, tokens: set[str]) -> bool:
    """A validation refusal, a coded refusal, or a refusal naming the placeholder."""

    return (
        any(marker in message for marker in _VALIDATION)
        or bool(_CODE.search(message))
        or any(token in message for token in tokens)
    )


def _cli_refusal(exit_code: int, output: str, tokens: set[str]) -> bool:
    if exit_code == 0:
        return False
    if exit_code == 2 or _REFUSED:
        return True  # a click usage error, or the daemon refused the operand
    errors = [line for line in output.splitlines() if line.startswith("Error:")]
    return any(_TYPED.match(line) or _message_refuses(line, tokens) for line in errors)


def _sdk_refusal(exc: BaseException, tokens: set[str]) -> bool:
    from pydantic import ValidationError

    from cruxible_client.errors import ConfigError, DataValidationError

    if isinstance(exc, NameError):
        return exc.name in SDK_BARE_PLACEHOLDERS
    if isinstance(exc, ValidationError | DataValidationError | ConfigError) or _REFUSED:
        return True
    code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
    if isinstance(code, str) and "." in code:
        return True
    if isinstance(exc, TypeError):
        return False  # a call that does not bind is a broken line, never protection
    from cruxible_client.errors import CoreError

    if isinstance(exc, CoreError) and type(exc) is not CoreError:
        return True  # a typed client refusal (ProviderFormatError, ...)
    return _message_refuses(str(exc), tokens)


@pytest.mark.parametrize(
    ("operation", "surface", "line"),
    list(_placeholder_lines()),
    ids=lambda value: value if isinstance(value, str) and " " not in value else None,
)
def test_a_line_run_unchanged_refuses_specifically_and_changes_nothing(
    operation: str,
    surface: str,
    line: str,
    daemon: tuple[Any, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Every placeholder line, run as printed against a real daemon, refuses by
    a field's own validation, a coded refusal, or a documented bare name."""

    client, instance_id, reviewer_key = daemon
    tokens = set(PLACEHOLDER_PATTERN.findall(line))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    before = _head(instance_id)
    if surface == "cli":
        from click.testing import CliRunner

        monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
        command, _, here = line.partition(" <<< ")
        result = CliRunner().invoke(
            cli,
            [
                "--server-url",
                "http://testserver",
                "--instance-id",
                instance_id,
                "--no-autostart",
                *shlex.split(command)[1:],
            ],
            input=shlex.split(here)[0] if here else "",
        )
        refused = _cli_refusal(result.exit_code, result.output, tokens)
        detail = result.output
    elif surface == "mcp":
        monkeypatch.setattr("cruxible_core.mcp.handlers._get_client", lambda: client)
        monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
        # An approver with a key, so an approval reaches its operands.
        monkeypatch.setenv("CRUXIBLE_MCP_KEY_DIR", str(reviewer_key.parent))
        call = ast.parse(line, mode="eval").body
        assert isinstance(call, ast.Call)
        tool = _mcp_tools()[call.func.id]  # type: ignore[attr-defined]
        kwargs = {item.arg: _mcp_literal(item.value) for item in call.keywords if item.arg}
        if "instance_id" in tool.parameters.get("properties", {}):
            kwargs.setdefault("instance_id", instance_id)
        try:
            asyncio.run(tool.run(kwargs))
        except Exception as exc:  # noqa: BLE001 - classified below
            detail = str(exc)
            cause = exc.__cause__
            refused = (
                bool(_REFUSED)
                or _message_refuses(detail, tokens)
                or (cause is not None and _sdk_refusal(cause, tokens))
            )
        else:
            refused, detail = False, "returned"
    else:
        from cruxible_client.authoring.sdk import Cruxible

        cx = Cruxible._from_client(client, instance_id=instance_id, workspace=workspace)  # noqa: SLF001
        try:
            exec(line, {"cx": cx})  # noqa: S102 - the line as printed, beside only cx
        except Exception as exc:  # noqa: BLE001 - classified below
            refused, detail = _sdk_refusal(exc, tokens), repr(exc)
        else:
            refused, detail = False, "returned"
    if not refused and _is_preview(line, surface):
        refused = True  # a preview stores nothing; the head check below proves it
    assert refused, f"{operation} on {surface}: no specific refusal for {line}: {detail}"
    assert _head(instance_id) == before, f"{operation} on {surface} changed accepted state"


def test_opaque_content_that_reads_like_a_placeholder_is_sent() -> None:
    """Review round 4, finding 1: a Claim value of exactly <TEXT> is data, and is sent."""

    from cruxible_client import CruxibleClient
    from cruxible_client.contracts.write import WriteRequest

    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(409, json={"error_type": "CoreError", "message": "recorded"})

    client = CruxibleClient(base_url="http://opaque.test")
    client._client._client = httpx.Client(  # type: ignore[attr-defined]  # noqa: SLF001
        base_url="http://opaque.test", transport=httpx.MockTransport(answer)
    )
    request = WriteRequest.model_validate(
        {
            "because": "The source names this field literally.",
            "changes": [
                {"op": "set", "subject": "dev.item/a", "field": "status", "value": "<TEXT>"}
            ],
        }
    )
    with pytest.raises(Exception, match="recorded"):
        client.write("inst", request=request)
    assert sent and b'"<TEXT>"' in sent[-1].content


def test_a_reason_or_name_placeholder_is_refused_by_its_field() -> None:
    """The one field check: only the rendered tokens, only on the caller's own words."""

    from pydantic import ValidationError

    from cruxible_core.server.playbill_request_models import FileProposeRequest

    with pytest.raises(ValidationError, match="placeholder"):
        FileProposeRequest.model_validate({"file": "status", "proposal_name": "<NAME>"})
    assert is_placeholder("<TEXT>") and not is_placeholder(" <TEXT> ")
    assert not is_placeholder("<HTML>") and not is_placeholder("<P>")
