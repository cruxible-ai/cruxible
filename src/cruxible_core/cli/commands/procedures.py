"""CLI commands for governed state-held procedures."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import click
import yaml
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _list_envelope,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureExecutionResult,
    ProcedureRecord,
    ProcedureRun,
    ProcedureStatus,
    ProcedureTransitionResult,
)
from cruxible_core.service import (
    service_get_procedure,
    service_list_procedure_runs,
    service_list_procedures,
    service_promote_procedure,
    service_propose_procedure,
    service_reject_procedure,
    service_retire_procedure,
    service_run_procedure,
)


@click.group("procedure")
def procedure_group() -> None:
    """Manage governed executable procedures.

    Workflows are designed; procedures are learned.
    """


def _load_definition(path: Path) -> ProcedureDefinition:
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise click.BadParameter(f"Could not read procedure definition '{path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise click.BadParameter(
            f"Procedure definition '{path}' is not valid JSON or YAML: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise click.BadParameter("Procedure definition must contain a top-level object")
    try:
        return ProcedureDefinition.model_validate(raw)
    except ValidationError as exc:
        raise click.BadParameter(f"Procedure definition is invalid: {exc}") from exc


def _parse_evidence_refs(raw_refs: tuple[str, ...]) -> list[contracts.EvidenceRef]:
    refs: list[contracts.EvidenceRef] = []
    for raw in raw_refs:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.BadParameter("--evidence-ref must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise click.BadParameter("--evidence-ref must be a JSON object")
        try:
            refs.append(contracts.EvidenceRef.model_validate(payload))
        except ValidationError as exc:
            raise click.BadParameter(f"--evidence-ref is invalid: {exc}") from exc
    return refs


def _parse_run_input(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--input must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise click.BadParameter("--input must be a JSON object")
    return cast(dict[str, Any], payload)


def _procedure_from_result(result: Any) -> ProcedureRecord:
    if isinstance(result, ProcedureRecord):
        return result
    if isinstance(result, ProcedureTransitionResult):
        return result.procedure
    if not isinstance(result, dict) or not isinstance(result.get("procedure"), dict):
        raise click.ClickException("Procedure response is missing its procedure record")
    return ProcedureRecord.model_validate(result["procedure"])


def _transition_receipt_id(result: Any) -> str | None:
    if isinstance(result, ProcedureTransitionResult):
        return result.receipt_id
    if isinstance(result, dict):
        value = result.get("receipt_id")
        return value if isinstance(value, str) else None
    return None


def _procedure_items(result: Any) -> list[ProcedureRecord]:
    return [ProcedureRecord.model_validate(item) for item in result.items]


def _run_items(result: Any) -> list[ProcedureRun]:
    return [ProcedureRun.model_validate(item) for item in result.items]


def _echo_procedure(procedure: ProcedureRecord) -> None:
    click.echo(f"{procedure.procedure_id} [{procedure.status}] v{procedure.version}")
    click.echo(f"  Name: {procedure.definition.name}")
    click.echo(f"  Tier: {procedure.definition.declared_tier}")
    click.echo(
        "  Budget: "
        f"{procedure.definition.budget.wall_clock_s:g}s, "
        f"{procedure.definition.budget.max_provider_calls} provider call(s)"
    )
    if procedure.definition.description:
        click.echo(f"  {procedure.definition.description}")


def _procedure_payload(procedure: ProcedureRecord) -> dict[str, Any]:
    return procedure.model_dump(mode="json", by_alias=True, exclude_none=True)


@procedure_group.command("propose")
@click.argument(
    "definition_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--supersedes", "supersedes_procedure_id", default=None)
@click.option(
    "--evidence-ref",
    "evidence_refs",
    multiple=True,
    help="JSON evidence ref object. Repeat to attach multiple refs.",
)
@handle_errors
def procedure_propose(
    definition_file: Path,
    supersedes_procedure_id: str | None,
    evidence_refs: tuple[str, ...],
) -> None:
    """Propose a procedure definition from a JSON or YAML file."""
    definition = _load_definition(definition_file)
    parsed_evidence = _parse_evidence_refs(evidence_refs)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.propose_procedure(
            instance_id,
            definition=definition.model_dump(mode="json", by_alias=True, exclude_none=True),
            supersedes_procedure_id=supersedes_procedure_id,
            evidence_refs=parsed_evidence,
        ),
        lambda instance: service_propose_procedure(
            instance,
            definition,
            actor_context=None,
            supersedes_procedure_id=supersedes_procedure_id,
            evidence_refs=[ref.model_dump(mode="python") for ref in parsed_evidence],
        ),
        allow_local=False,
        command_name="procedure propose",
    )
    procedure = _procedure_from_result(result)
    click.echo(f"Procedure {procedure.procedure_id} proposed.")
    click.echo(f"  Status: {procedure.status}")
    click.echo(f"  Version: {procedure.version}")
    receipt_id = _transition_receipt_id(result)
    if receipt_id:
        click.echo(f"  Receipt: {receipt_id}")


@procedure_group.command("list")
@click.option(
    "--status",
    type=click.Choice(["pending", "live", "rejected", "retired"]),
    default=None,
    help="Filter by lifecycle status.",
)
@click.option("--limit", default=100, type=click.IntRange(min=1), help="Max procedures to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def procedure_list(
    status: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List governed procedures."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_procedures(
            instance_id,
            status=status,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_procedures(
            instance,
            status=cast(ProcedureStatus | None, status),
            limit=limit,
            offset=offset,
        ),
    )
    procedures = _procedure_items(result)
    if output_json:
        items = [_procedure_payload(procedure) for procedure in procedures]
        _emit_json(
            {
                "items": items,
                **_list_envelope(
                    result,
                    item_count=len(items),
                    limit=limit,
                    offset=offset,
                ),
            }
        )
        return
    for procedure in procedures:
        _echo_procedure(procedure)
    click.echo(f"{len(procedures)} of {result.total} procedure(s) shown.")


@procedure_group.command("show")
@click.argument("procedure_id")
@json_option
@handle_errors
def procedure_show(procedure_id: str, output_json: bool) -> None:
    """Show one procedure definition and lifecycle record."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_procedure(instance_id, procedure_id),
        lambda instance: service_get_procedure(instance, procedure_id),
    )
    procedure = _procedure_from_result(result)
    if output_json:
        _emit_json({"procedure": _procedure_payload(procedure)})
        return
    _echo_procedure(procedure)
    click.echo("  Definition:")
    click.echo(
        yaml.safe_dump(
            procedure.definition.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    )


@procedure_group.command("resolve")
@click.argument("procedure_id")
@click.option(
    "--action",
    type=click.Choice(["promote", "reject"]),
    required=True,
)
@click.option("--expected-version", required=True, type=click.IntRange(min=1))
@click.option("--reason", default=None, help="Required when rejecting.")
@handle_errors
def procedure_resolve(
    procedure_id: str,
    action: str,
    expected_version: int,
    reason: str | None,
) -> None:
    """Promote or reject one pending procedure."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.resolve_procedure(
            instance_id,
            procedure_id,
            action=action,
            expected_version=expected_version,
            reason=reason,
        ),
        lambda instance: (
            service_promote_procedure(
                instance,
                procedure_id,
                expected_version=expected_version,
                actor_context=None,
            )
            if action == "promote"
            else service_reject_procedure(
                instance,
                procedure_id,
                expected_version=expected_version,
                reason=reason or "",
                actor_context=None,
            )
        ),
        allow_local=False,
        command_name="procedure resolve",
    )
    procedure = _procedure_from_result(result)
    click.echo(f"Procedure {procedure.procedure_id} {procedure.status}.")
    click.echo(f"  Version: {procedure.version}")
    receipt_id = _transition_receipt_id(result)
    if receipt_id:
        click.echo(f"  Receipt: {receipt_id}")


@procedure_group.command("retire")
@click.argument("procedure_id")
@click.option("--expected-version", required=True, type=click.IntRange(min=1))
@click.option("--reason", required=True, help="Reason for retirement.")
@handle_errors
def procedure_retire(
    procedure_id: str,
    expected_version: int,
    reason: str,
) -> None:
    """Retire one live procedure."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.retire_procedure(
            instance_id,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
        ),
        lambda instance: service_retire_procedure(
            instance,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
            actor_context=None,
        ),
        allow_local=False,
        command_name="procedure retire",
    )
    procedure = _procedure_from_result(result)
    click.echo(f"Procedure {procedure.procedure_id} retired.")
    click.echo(f"  Version: {procedure.version}")
    receipt_id = _transition_receipt_id(result)
    if receipt_id:
        click.echo(f"  Receipt: {receipt_id}")


@procedure_group.command("run")
@click.argument("procedure_id")
@click.option("--input", "input_json", required=True, help="Procedure input as a JSON object.")
@json_option
@handle_errors
def procedure_run(procedure_id: str, input_json: str, output_json: bool) -> None:
    """Run one live procedure through the generic procedure executor."""
    input_payload = _parse_run_input(input_json)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.run_procedure(
            instance_id,
            procedure_id,
            input_payload=input_payload,
        ),
        lambda instance: service_run_procedure(
            instance,
            procedure_id,
            input_payload,
            None,
        ),
        allow_local=False,
        command_name="procedure run",
    )
    if isinstance(result, ProcedureExecutionResult):
        payload = result.model_dump(mode="json")
    else:
        payload = result
    if output_json:
        _emit_json(payload)
        return
    run = ProcedureRun.model_validate(payload["run"])
    click.echo(f"Procedure {procedure_id} run {run.run_id} {run.verdict}.")
    receipt = payload.get("receipt")
    if isinstance(receipt, dict) and receipt.get("receipt_id"):
        click.echo(f"  Receipt: {receipt['receipt_id']}")
    _emit_json(payload.get("output"), sort_keys=True)


@procedure_group.command("runs")
@click.argument("procedure_id")
@click.option("--limit", default=100, type=click.IntRange(min=1), help="Max runs to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def procedure_runs(
    procedure_id: str,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List runs, including started records with null verdicts."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_procedure_runs(
            instance_id,
            procedure_id,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_procedure_runs(
            instance,
            procedure_id,
            limit=limit,
            offset=offset,
        ),
    )
    runs = _run_items(result)
    if output_json:
        items = [run.model_dump(mode="json") for run in runs]
        _emit_json(
            {
                "items": items,
                **_list_envelope(
                    result,
                    item_count=len(items),
                    limit=limit,
                    offset=offset,
                ),
            }
        )
        return
    for run in runs:
        if run.status == "started":
            verdict = "null (started/unfinalized tombstone)"
        else:
            verdict = str(run.verdict)
        click.echo(f"{run.run_id} status={run.status} verdict={verdict} started={run.started_at}")
    click.echo(f"{len(runs)} of {result.total} run(s) shown.")


__all__ = [
    "procedure_group",
    "procedure_list",
    "procedure_propose",
    "procedure_resolve",
    "procedure_retire",
    "procedure_run",
    "procedure_runs",
    "procedure_show",
]
