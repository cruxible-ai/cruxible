"""Every served repair is printed in the spelling of the client that shows it.

The CLI line parses against the live command, the MCP call names the tool's own
arguments, and the SDK call is Python naming a ``Cruxible`` method. Every
operation id a producer emits renders on every client: in its own spelling, or
as another client's form that says which client has it.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

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
    for key in ("replace", "auth", "all", "clear_claims"):
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
        spelling = REPAIR_SPELLINGS.get(operation)
        previews = spelling is not None and "dry_run" in {*spelling.cli_flags, *spelling.mcp_args}
        # A preview of an operation that has none has no form anywhere, by design.
        cases = ({}, {"dry_run": False}, *(({"dry_run": True},) if previews else ()))
        for arguments in cases:
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


# -- round 2: placeholders fail closed, controls survive, SDK preflight -----------------


def _all_operations() -> list[str]:
    from cruxible_client.contracts.repairs import REPAIR_COMMAND_PATHS

    return sorted(set(REPAIR_SPELLINGS) | set(REPAIR_COMMAND_PATHS) | _produced_operations())


def _emitted_placeholders() -> set[str]:
    """Every placeholder the renderer emits for an operand a repair does not carry."""

    from cruxible_client.contracts.placeholders import PLACEHOLDER_PATTERN

    found: set[str] = set()
    for operation in _all_operations():
        for arguments in ({}, {"dry_run": True}):
            for surface in ("cli", "mcp", "sdk"):
                line = render_repair(
                    RepairOperation(operation=operation, arguments=arguments),
                    surface=surface,  # type: ignore[arg-type]
                )
                found.update(PLACEHOLDER_PATTERN.findall(line))
    return found


def test_every_placeholder_is_one_refused_form_and_the_cli_quotes_it() -> None:
    """A placeholder never reads as a value: <NAME>, quoted on the CLI so the shell passes it."""

    emitted = _emitted_placeholders()
    assert {"<TEXT>", "<SIGNER_ID>", "<PROPOSAL_ID>", "<CREDENTIAL_ID>", "<KIT>"} <= emitted
    for operation in _all_operations():
        line = render_repair(RepairOperation(operation=operation), surface="cli")
        for token in line.split():
            if "<" in token and not token.startswith("'<") and token != "<<<":
                raise AssertionError(f"{operation}: unquoted placeholder in {line!r}")


@pytest.mark.parametrize(
    "model_path,field",
    [
        ("cruxible_client.contracts.write:WriteRequest", "because"),
        ("cruxible_client.contracts.capture_erasure:CaptureEraseRequest", "because"),
        ("cruxible_client.contracts.line_inbox:InboxDismissRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:ProposalRejectRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:ProposalWithdrawRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:InstanceDecommissionRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:CurationSuppressRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:CurationUnsuppressRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:CurationOverruleRequest", "reason"),
        ("cruxible_core.server.playbill_request_models:CurationAcceptFixedRequest", "reason"),
    ],
)
def test_a_placeholder_reason_is_refused_where_the_reason_is_read(
    model_path: str, field: str
) -> None:
    """Review round 2, finding 2: `because="<TEXT>"` once wrote a Claim; now it refuses."""

    import importlib

    from pydantic import ValidationError

    module, name = model_path.split(":")
    model = getattr(importlib.import_module(module), name)
    errors: list[str] = []
    for text in ("<TEXT>", "<REASON>", " <TEXT> "):
        try:
            model.model_validate({field: text})
        except ValidationError as exc:
            errors.extend(str(item["msg"]) for item in exc.errors() if item["loc"][:1] == (field,))
        else:  # pragma: no cover - decisive guard
            raise AssertionError(f"{name}.{field} accepted {text!r}")
    assert errors and all("placeholder" in message for message in errors), errors
    # Ordinary text, including text that mentions a placeholder, still passes the field.
    for text in ("Reviewer confirmed the fix.", "replace <TEXT> later"):
        try:
            model.model_validate({field: text})
        except ValidationError as exc:
            assert all(item["loc"][:1] != (field,) for item in exc.errors()), exc


def test_identifier_placeholders_fail_their_own_formats() -> None:
    """The rest refuse by format, lookup or file read; none reads as a valid operand."""

    from pydantic import ValidationError

    from cruxible_client.contracts.predictions import SettleRequest
    from cruxible_client.contracts.principals import is_canonical_principal_id
    from cruxible_client.contracts.write import WriteRequest

    with pytest.raises(ValidationError):
        SettleRequest(observation="<CLAIM_ID>")
    with pytest.raises(ValidationError):
        WriteRequest.model_validate(
            {"because": "a real reason", "changes": [{"op": "retire", "target": "<CLAIM_ID>"}]}
        )
    for principal in ("<SIGNER_ID>", "<PRINCIPAL_ID>"):
        assert not is_canonical_principal_id(principal)
    assert not Path("<DIR>").exists() and not Path("<KIT>").exists()


def test_a_suggested_write_run_unchanged_refuses_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI line next prints for a retire, run as printed, never reaches the daemon."""

    import shlex as _shlex

    from click.testing import CliRunner

    claim = "CLM-" + "1" * 32
    line = render_repair(
        RepairOperation(operation="cruxible.claim.retire", arguments={"claim_id": claim}),
        surface="cli",
    )
    assert line == f"cruxible retire {claim} --because '<TEXT>'"

    class Unreachable:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the placeholder write reached the client ({name})")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: Unreachable())
    result = CliRunner().invoke(
        cli,
        ["--server-url", "https://x.invalid", "--instance-id", "inst", *_shlex.split(line)[1:]],
    )
    assert result.exit_code != 0
    assert "placeholder" in result.output


def test_a_composed_repair_keeps_every_control_it_carries() -> None:
    repin = RepairOperation(
        operation="cruxible.block.repin",
        arguments={
            "source_id": "status",
            "block_id": "tiers",
            "dry_run": True,
            "clear_claims": True,
        },
    )
    assert render_repair(repin, surface="cli") == (
        "cruxible block repin status tiers --clear-claims --dry-run"
    )
    assert render_repair(repin, surface="mcp") == (
        'cruxible_block_repin(file="status", block="tiers", dry_run=true, claims=[])'
    )
    assert render_repair(repin, surface="sdk") == (
        'cx.block.repin("status", "tiers", evaluation_time=datetime.now(UTC), claims=[], '
        "dry_run=True)"
    )
    # Without controls, the composer's line is unchanged.
    plain = RepairOperation(
        operation="cruxible.block.repin", arguments={"source_id": "status", "block_id": "tiers"}
    )
    assert render_repair(plain, surface="cli") == "cruxible block repin status tiers"


def test_the_sdk_preflight_repair_runs_preflight() -> None:
    """Review round 2, finding 5: prepare() re-runs preflight; status() only reads it."""

    preflight = RepairOperation(
        operation="cruxible.authoring.preflight", arguments={"intent_id": "AIT-1"}
    )
    assert render_repair(preflight, surface="sdk") == 'cx.resume_intent("AIT-1").prepare()'


def test_next_rows_built_by_the_previous_derivation_are_rebuilt() -> None:
    """Review round 2, finding 4: the rows' commands changed, so the stored state rebuilds.

    The format is part of the schema text; a state built at 15 holds rows whose
    repairs lack their because operand and quoted placeholders.
    """

    from cruxible_core.consumers.next import facts

    assert facts.FORMAT == 16
    assert "CHECK(v=16)" in "".join(facts._SCHEMA)
