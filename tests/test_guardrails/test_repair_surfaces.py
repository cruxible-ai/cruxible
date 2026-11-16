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

from cruxible_client.contracts.placeholders import PLACEHOLDER_PATTERN, is_placeholder
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

#: Producers whose operation or arguments are computed, so a static read cannot
#: collect their keys; each names the keys it can carry, reviewed by hand.
COMPUTED_PRODUCERS: Mapping[str, frozenset[str]] = {
    # server/errors.py: the credential refusals' context, and the bootstrap
    # refusals' instance (operation from _BOOTSTRAP_REPAIR_OPERATIONS).
    "credential.mint": frozenset(
        {
            "refused_operation",
            "credential_env",
            "accepted_credentials",
            "credential_options",
            "unbound_credential_id",
        }
    ),
    "credential.claim-bootstrap": frozenset({"instance_id"}),
    "credential.recover-admin": frozenset({"instance_id"}),
    # (service/identity.py adds an unbound credential's id, for the reader.)
    # next.py rows build these from dicts the row derives (the composer's keys).
    "cruxible.set": frozenset(
        {"subject", "field", "value", "role", "capture_digest", "evidence_contract", "claim_id"}
    ),
    "cruxible.rebind": frozenset({"claim_id", "citation", "section_file", "section"}),
    "cruxible.block.repin": frozenset({"source_id", "block_id", "claim", "clear_claims"}),
    "cruxible.block.depublish": frozenset({"source_id", "block_id"}),
    "cruxible.proposal.approve": frozenset({"proposal_id", "signer_id"}),
    "cruxible.authoring.example": frozenset({"example", "claim_id", "capture_digest"}),
    "cruxible.kit.add": frozenset({"reference"}),
    "cruxible.get": frozenset({"ref", "detail", "range"}),
}

#: Operations class (c) cannot execute, with why. Reviewed in the report.
PLACEHOLDER_EXECUTION_EXCEPTIONS: Mapping[tuple[str, str], str] = {}


# -- producers ---------------------------------------------------------------------------


_REPAIR_CALLS = {"RepairOperation", "PlaybillNextRepairV1", "NextRepair"}
_CHANGE_CALLS = {"change_scope", "state_change_scope", "ChangeMode", "_pin", "ChangeRefusedError"}


@cache
def producer_keys() -> Mapping[str, frozenset[str]]:
    """Every argument key a producer in the source can put on each operation's repair."""

    found: dict[str, set[str]] = defaultdict(set)
    for base in (_ROOT / "src", _ROOT / "packages" / "cruxible-client" / "src"):
        for path in base.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                kw = {item.arg: item.value for item in node.keywords if item.arg}
                op = kw.get("operation")
                if not (isinstance(op, ast.Constant) and isinstance(op.value, str)):
                    continue
                if name in _REPAIR_CALLS:
                    found[op.value]
                    arguments = kw.get("arguments")
                    if isinstance(arguments, ast.Dict):
                        found[op.value].update(
                            key.value
                            for key in arguments.keys
                            if isinstance(key, ast.Constant) and isinstance(key.value, str)
                        )
                elif name in _CHANGE_CALLS and "repair" not in kw:
                    # A change's refusal repairs it with a preview or a retry.
                    found[op.value].add("dry_run")
    for operation, keys in COMPUTED_PRODUCERS.items():
        found[operation].update(keys)
    return {
        operation: frozenset(keys)
        for operation, keys in found.items()
        if operation not in _NOT_REPAIR_OPERATIONS
    }


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

    from cruxible_client.authoring import examples
    from cruxible_client.authoring.sdk import Cruxible

    expression = ast.parse(line, mode="eval").body
    assert isinstance(expression, ast.Call), line

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
        target: Any = getattr(examples, func.id)
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
        'cx.block.repin("status", "summary", evaluation_time=datetime.now(UTC), '
        'claims=["CLM-1", "CLM-2"], dry_run=True)'
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


class _Recorder:
    """A daemon that answers the version probe and refuses everything else, recording it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        from cruxible_client import __version__
        from cruxible_client.contracts.authoring.models import (
            AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
        )

        self.requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(
                200,
                json={
                    "version": __version__,
                    "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
                },
            )
        return httpx.Response(
            409, json={"error_type": "CoreError", "message": "recorded, not served"}
        )

    def leaked(self) -> list[str]:
        found = []
        for request in self.requests:
            text = f"{request.url.path} {request.content.decode('utf-8', 'replace')}"
            found.extend(PLACEHOLDER_PATTERN.findall(text))
        return found


def _recording_client(recorder: _Recorder) -> Any:
    from cruxible_client import CruxibleClient

    client = CruxibleClient(base_url="http://placeholder.test")
    client._client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://placeholder.test", transport=httpx.MockTransport(recorder)
    )
    return client


def _placeholder_lines() -> Iterator[tuple[str, str, str]]:
    for operation, spelling in sorted(REPAIR_SPELLINGS.items()):
        if not spelling.required:
            continue
        for surface in _SURFACES:
            line = render_repair(
                RepairOperation(operation=operation),
                surface=surface,  # type: ignore[arg-type]
            )
            if _own_form(line) and PLACEHOLDER_PATTERN.search(line):
                yield operation, surface, line


def test_every_operation_with_a_required_operand_emits_a_placeholder_somewhere() -> None:
    """A new operation with a required operand is covered by class (c) automatically."""

    covered = {operation for operation, _surface, _line in _placeholder_lines()}
    for operation, spelling in REPAIR_SPELLINGS.items():
        if spelling.required and (operation, "*") not in PLACEHOLDER_EXECUTION_EXCEPTIONS:
            assert operation in covered, f"{operation} renders no placeholder on any client"


@pytest.mark.parametrize(
    ("operation", "surface", "line"),
    list(_placeholder_lines()),
    ids=lambda value: value if isinstance(value, str) and " " not in value else None,
)
def test_a_line_run_unchanged_refuses_and_sends_no_placeholder(
    operation: str,
    surface: str,
    line: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Every placeholder line, run as printed on its own client, refuses before acting."""

    reason = PLACEHOLDER_EXECUTION_EXCEPTIONS.get((operation, surface))
    if reason is not None:
        pytest.skip(reason)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    recorder = _Recorder()
    client = _recording_client(recorder)
    refused: bool
    if surface == "cli":
        from click.testing import CliRunner

        monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
        result = CliRunner().invoke(
            cli,
            [
                "--server-url",
                "http://placeholder.test",
                "--instance-id",
                "inst_placeholder",
                "--no-autostart",
                *shlex.split(line.split(" <<< ")[0])[1:],
            ],
            input="",
        )
        refused = result.exit_code != 0
        detail = result.output
    elif surface == "mcp":
        monkeypatch.setattr("cruxible_core.mcp.handlers._get_client", lambda: client)
        monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(tmp_path))
        call = ast.parse(line, mode="eval").body
        assert isinstance(call, ast.Call)
        tool = _mcp_tools()[call.func.id]  # type: ignore[attr-defined]
        kwargs = {item.arg: _mcp_literal(item.value) for item in call.keywords if item.arg}
        if "instance_id" in tool.parameters.get("properties", {}):
            kwargs.setdefault("instance_id", "inst_placeholder")
        try:
            asyncio.run(tool.run(kwargs))
        except Exception as exc:  # noqa: BLE001 - any refusal is the expected outcome
            refused, detail = True, repr(exc)
        else:
            refused, detail = False, "returned"
    else:
        from cruxible_client.authoring import examples
        from cruxible_client.authoring.sdk import Cruxible

        cx = Cruxible.__new__(Cruxible)
        try:
            cx.__init__(  # type: ignore[misc]
                client=client,
                instance_id="inst_placeholder",
                workspace=tmp_path,
                access_profile=None,
                clock=None,
            )
        except Exception:  # noqa: BLE001 - a bare handle is enough to call the method
            pass
        try:
            eval(line, {"cx": cx, "authoring_example": examples.authoring_example})  # noqa: S307
        except Exception as exc:  # noqa: BLE001 - any refusal is the expected outcome
            refused, detail = True, repr(exc)
        else:
            refused, detail = False, "returned"
    assert refused, f"{operation} on {surface} acted on its placeholders: {line} -> {detail}"
    assert not recorder.leaked(), f"{operation} on {surface} sent {recorder.leaked()}: {line}"


def test_a_placeholder_never_leaves_the_client() -> None:
    """The one transport check every client shares; ordinary text passes."""

    from cruxible_client.transport.http import PlaceholderRefused, refuse_placeholders

    with pytest.raises(PlaceholderRefused, match="<NAME> is a placeholder"):
        refuse_placeholders("/api/v1/inst/files/propose", {"proposal_name": "<NAME>"})
    with pytest.raises(PlaceholderRefused):
        refuse_placeholders("/api/v1/inst/proposals/%3CPROPOSAL_ID%3E/readmit", None)
    refuse_placeholders("/api/v1/inst/write", {"because": "fix <TEXT> later", "n": [1, "a"]})
    assert is_placeholder(" <TEXT> ") and not is_placeholder("<text>")


def test_a_proposal_name_placeholder_is_refused_by_the_request() -> None:
    """Review round 3, finding 2: the name a suggestion leaves is not a name."""

    from pydantic import ValidationError

    from cruxible_core.server.playbill_request_models import FileProposeRequest

    with pytest.raises(ValidationError, match="placeholder"):
        FileProposeRequest.model_validate({"file": "status", "proposal_name": "<NAME>"})
