"""Every served repair is printed in the spelling of the client that shows it.

The CLI line parses against the live command, the MCP call names the tool's own
arguments, and the SDK call is Python naming a ``Cruxible`` method. Every
operation id a producer emits renders on every client: in its own spelling, or
as another client's form that says which client has it.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any

import click
import pytest

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.repair_spelling import (
    REPAIR_SPELLINGS,
    RepairSpelling,
    render_repair,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.cli.main import cli

_ROOT = Path(__file__).resolve().parents[2]

#: ``operation=`` literals that name no served repair: consumption records,
#: changeset rationales, refusal-index actions and next's hand-edit marker.
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


def _sample(spelling: RepairSpelling, *, dry_run: bool) -> dict[str, object]:
    keys = {*spelling.cli_args, *spelling.cli_flags, *spelling.mcp_args, *spelling.required}
    values: dict[str, object] = {key: f"sample-{key}" for key in keys}
    if "dry_run" in values:
        values["dry_run"] = dry_run
    for key in ("replace", "auth", "all"):
        if key in values:
            values[key] = True
    return values


def _command(path: str) -> click.Command:
    ctx = click.Context(cli)
    command: click.Command = cli
    for name in path.split():
        assert isinstance(command, click.Group), path
        found = command.get_command(ctx, name)
        assert found is not None, f"no CLI command `cruxible {path}`"
        command, ctx = found, click.Context(found, parent=ctx)
    return command


@pytest.mark.parametrize("operation", sorted(REPAIR_SPELLINGS))
@pytest.mark.parametrize("dry_run", [True, False])
def test_every_cli_spelling_parses_against_the_live_command(operation: str, dry_run: bool) -> None:
    spelling = REPAIR_SPELLINGS[operation]
    if spelling.cli is None:
        pytest.skip("no CLI form")
    for arguments in ({}, _sample(spelling, dry_run=dry_run)):
        line = render_repair(
            RepairOperation(operation=operation, arguments=arguments), surface="cli"
        )
        prefix = f"cruxible {spelling.cli}"
        if spelling.composed_first and not line.startswith(prefix):
            continue  # the next composer's line; its own tests pin it
        assert line.startswith(prefix), line
        tokens = shlex.split(line)[len(prefix.split()) :]
        if "<<<" in tokens:
            continue
        command = _command(spelling.cli)
        # Parsing raises on an unknown flag, a missing required operand or a
        # surplus positional, which is exactly an unrunnable line. A value the
        # parameter's type rejects is a sample or a placeholder to replace.
        try:
            command.make_context(spelling.cli, tokens)
        except click.MissingParameter:
            raise
        except click.BadParameter:
            pass


def _mcp_schemas(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    from cruxible_core.mcp.server import create_server

    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    server = create_server()
    return {name: tool.parameters for name, tool in server._tool_manager._tools.items()}


def _request_fields(schema: dict[str, Any]) -> set[str]:
    request = schema["properties"]["request"]
    ref = request.get("$ref") or next(
        (item["$ref"] for item in request.get("anyOf", ()) if "$ref" in item), None
    )
    assert ref is not None, schema
    model = schema["$defs"][ref.rsplit("/", 1)[1]]
    return set(model.get("properties", {}))


def test_every_mcp_spelling_names_the_tools_own_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = _mcp_schemas(monkeypatch)
    for operation, spelling in sorted(REPAIR_SPELLINGS.items()):
        if spelling.mcp is None:
            continue
        assert spelling.mcp in schemas, f"{operation}: no MCP tool {spelling.mcp}"
        schema = schemas[spelling.mcp]
        properties = set(schema.get("properties", {}))
        for name in spelling.mcp_args.values():
            top, _, nested = name.partition(".")
            assert top in properties, f"{operation}: {spelling.mcp} takes no {top}"
            if nested:
                assert nested in _request_fields(schema), f"{operation}: request has no {nested}"
        # Every argument the tool requires is spelled, so the call can be made.
        spelled = {name.partition(".")[0] for name in spelling.mcp_args.values()}
        missing = set(schema.get("required", ())) - spelled - {"instance_id"}
        assert not missing, f"{operation}: {spelling.mcp} also requires {sorted(missing)}"
        line = render_repair(
            RepairOperation(operation=operation, arguments=_sample(spelling, dry_run=True)),
            surface="mcp",
        )
        assert line.startswith(f"{spelling.mcp}("), line


def _sdk_root(line: str) -> str:
    node = ast.parse(line, mode="eval").body
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id == "cx", line
            return node.attr
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Name):
            return node.id
        else:  # pragma: no cover - decisive guard
            raise AssertionError(line)


def test_every_sdk_spelling_is_python_naming_a_cruxible_method() -> None:
    import cruxible_client.authoring.examples as cruxible_client

    for operation, spelling in sorted(REPAIR_SPELLINGS.items()):
        if spelling.sdk is None:
            continue
        for arguments in ({}, _sample(spelling, dry_run=True)):
            line = render_repair(
                RepairOperation(operation=operation, arguments=arguments), surface="sdk"
            )
            root = _sdk_root(line.replace(".set(...)", ".set()"))
            assert hasattr(Cruxible, root) or hasattr(cruxible_client, root), (operation, line)


def _produced_operations() -> set[str]:
    pattern = re.compile(r'operation="([a-z_.-]+)"')
    found: set[str] = set()
    for root in (_ROOT / "src", _ROOT / "packages" / "cruxible-client" / "src"):
        for path in root.rglob("*.py"):
            found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found - _NOT_REPAIR_OPERATIONS


def test_every_produced_operation_renders_on_every_client() -> None:
    """No client prints a bare operation id and its JSON arguments."""

    produced = _produced_operations()
    assert "cruxible.proposal.readmit" in produced
    for operation in sorted(produced):
        for arguments in ({}, {"dry_run": True}):
            for surface in ("cli", "mcp", "sdk"):
                line = render_repair(
                    RepairOperation(operation=operation, arguments=arguments),
                    surface=surface,  # type: ignore[arg-type]
                )
                assert not line.startswith(operation), (operation, surface, line)


def test_a_form_another_client_has_says_which_client() -> None:
    mint = RepairOperation(
        operation="credential.mint",
        arguments={"principal_id": "agent", "permission_mode": "read_only"},
    )

    cli_line = "cruxible credential mint --principal-id agent --mode read_only"
    assert render_repair(mint, surface="cli") == cli_line
    assert render_repair(mint, surface="mcp") == (
        f"{cli_line}  (on the CLI; the MCP has no form for credential.mint)"
    )


def test_one_repair_in_each_spelling() -> None:
    readmit = RepairOperation(
        operation="cruxible.proposal.readmit", arguments={"proposal_id": "PRP-1", "dry_run": True}
    )

    assert render_repair(readmit, surface="cli") == "cruxible proposal readmit PRP-1 --dry-run"
    assert render_repair(readmit, surface="mcp") == (
        'cruxible_proposal_readmit(dry_run=true, proposal_id="PRP-1")'
    )
    assert render_repair(readmit, surface="sdk") == 'cx.proposal("PRP-1").readmit(dry_run=True)'
    # Context the producer carries for the reader is not an operand.
    orient = RepairOperation(
        operation="cruxible.orient",
        arguments={"section": "principals", "revoked_principal_id": "ops2"},
    )
    assert render_repair(orient, surface="cli") == "cruxible orient --section principals"
    # A block repair names the tool's own arguments (gapc-28).
    repin = RepairOperation(
        operation="cruxible.block.repin", arguments={"source_id": "status", "block_id": "tiers"}
    )
    assert (
        render_repair(repin, surface="mcp") == 'cruxible_block_repin(file="status", block="tiers")'
    )


def test_each_client_prints_a_refusal_repair_in_its_own_spelling() -> None:
    """The SDK as a traceback note, MCP in the tool failure, the CLI as a command."""

    from cruxible_client.errors import ErrorResponse, response_to_error
    from cruxible_core.mcp.server import ToolError, _tool_failure_message

    repair = RepairOperation(
        operation="cruxible.line.enable", arguments={"line": "security.kev-triage"}
    )
    exc = response_to_error(
        409,
        ErrorResponse(
            error_type="CoreError",
            message="cruxible.line.not_enabled: the Line is not enabled",
            error_code="cruxible.line.not_enabled",
            repair=repair,
        ),
    )
    assert exc.__notes__ == ['Repair: cx.line("security.kev-triage").enable()']
    assert "Repair" not in str(exc)

    failure = ToolError(str(exc))
    failure.__cause__ = exc
    assert _tool_failure_message("cruxible_line_run", failure).endswith(
        ' Repair: cruxible_line_enable(line="security.kev-triage")'
    )
