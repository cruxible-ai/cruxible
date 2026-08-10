"""CLI commands for governed state-held procedures."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import click
import yaml
from pydantic import ValidationError

from cruxible_client import CruxibleClient, contracts
from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _list_envelope,
    _require_instance_id,
    _root_ctx_obj,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import (
    LinkedOutcomeSummary,
    ProcedureDefinition,
    ProcedureExecutionResult,
    ProcedureGetResult,
    ProcedureReading,
    ProcedureReadRecord,
    ProcedureRecord,
    ProcedureRun,
    ProcedureStatus,
    ProcedureTransitionResult,
    procedure_record_from_payload,
)
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure_details,
    service_list_procedure_runs,
    service_list_procedures,
    service_propose_procedure,
    service_record_reading,
    service_reject_procedure,
    service_retire_procedure,
    service_run_procedure,
    service_withdraw_procedure,
)
from cruxible_core.service.procedure_migrations import (
    ProcedureMigrationActorIdentity,
    ProcedureMigrationResult,
    ProcedureMigrationSurface,
    run_procedure_migration,
)
from cruxible_core.temporal import parse_datetime

_RUNTIME_CREDENTIAL_TOKEN = re.compile(r"^crt_(rcred_[0-9a-f]{16})_")


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


def _parse_json_value(raw: str | None, *, option: str) -> Any | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"{option} must be valid JSON") from exc


def _procedure_from_result(result: Any) -> ProcedureRecord:
    """Unwrap a procedure from any local service result or daemon envelope.

    The local paths return models (a record, or a transition result wrapping
    one) and every daemon path returns a JSON envelope; there is no third
    shape, so anything else is a genuine surface mismatch, not a case to
    normalize.
    """
    if isinstance(result, ProcedureRecord):
        return result
    if isinstance(result, ProcedureTransitionResult):
        return result.procedure
    if isinstance(result, ProcedureGetResult):
        return result.procedure
    if not isinstance(result, dict) or not isinstance(result.get("procedure"), dict):
        raise click.ClickException("Procedure response is missing its procedure record")
    return _procedure_from_payload(result["procedure"])


def _transition_receipt_id(result: Any) -> str | None:
    if isinstance(result, ProcedureTransitionResult):
        return result.receipt_id
    if isinstance(result, dict):
        value = result.get("receipt_id")
        return value if isinstance(value, str) else None
    return None


def _transition_warnings(result: Any) -> list[tuple[str, str]]:
    """Return ``(code, message)`` per authoring warning.

    The typed channel is preferred and the string list is the fallback, so a
    daemon that predates `typed_warnings` still prints its warnings -- with the
    code rendered as `?`, which is visibly a missing code rather than a
    silently absent one.
    """
    if isinstance(result, ProcedureTransitionResult):
        if result.typed_warnings:
            return [(warning.code, warning.message) for warning in result.typed_warnings]
        return [("?", message) for message in result.warnings]
    if isinstance(result, dict):
        typed = result.get("typed_warnings")
        if isinstance(typed, list) and typed:
            return [
                (str(item.get("code", "?")), str(item.get("message", "")))
                for item in typed
                if isinstance(item, dict)
            ]
        warnings = result.get("warnings")
        if isinstance(warnings, list) and all(isinstance(item, str) for item in warnings):
            return [("?", message) for message in warnings]
    return []


def _contract_in_schema(result: Any) -> dict[str, Any] | None:
    if isinstance(result, ProcedureGetResult):
        if result.contract_in_schema is None:
            return None
        return result.contract_in_schema.model_dump(mode="json", exclude_none=True)
    if isinstance(result, dict):
        value = result.get("contract_in_schema")
        if isinstance(value, dict):
            return value
    return None


def _control_paths(result: Any) -> dict[str, Any] | None:
    if isinstance(result, ProcedureGetResult):
        if result.control_paths is None:
            return None
        return result.control_paths.model_dump(mode="json")
    if isinstance(result, dict):
        value = result.get("control_paths")
        if isinstance(value, dict):
            return value
    return None


def _echo_control_paths(enumeration: dict[str, Any] | None) -> None:
    """Print the behaviours a reviewer is being asked to authorise.

    A linear definition has exactly one path and printing it says nothing the
    step list did not, so it is skipped; the moment there is a second path the
    reviewer needs the list, because that is the point at which the step list
    stops describing what runs.
    """
    if enumeration is None:
        click.echo("  Control paths: unresolved (the definition's control graph does not resolve)")
        return
    paths = enumeration.get("paths") or []
    if len(paths) < 2 and not enumeration.get("truncated"):
        return
    click.echo(f"  Control paths ({len(paths)}):")
    for path in paths:
        click.echo(f"    {' -> '.join(str(node_id) for node_id in path)}")
    if enumeration.get("truncated"):
        click.echo(
            f"    ... truncated at the {enumeration.get('cap')}-path display cap; more paths exist"
        )


def _procedure_items(result: Any) -> list[ProcedureRecord]:
    return [_procedure_from_payload(item) for item in result.items]


def _procedure_from_payload(payload: Any) -> ProcedureRecord:
    try:
        return procedure_record_from_payload(payload)
    except (TypeError, ValidationError) as exc:
        raise click.ClickException(
            f"Procedure response contains an invalid procedure record: {exc}"
        ) from exc


class _RemoteProcedureMigrationSurface(ProcedureMigrationSurface):
    """Ordinary procedure-client verbs under separately authenticated actors."""

    def __init__(
        self,
        instance_id: str,
        *,
        proposer_client: CruxibleClient,
        reviewer_client: CruxibleClient | None,
    ) -> None:
        self._instance_id = instance_id
        self._proposer_client = proposer_client
        self._reviewer_client = reviewer_client

    def list_procedures(
        self,
        *,
        status: str,
        limit: int,
        offset: int,
    ) -> list[ProcedureRecord]:
        result = self._proposer_client.list_procedures(
            self._instance_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [_procedure_from_payload(item) for item in result.items]

    def propose_procedure(
        self,
        definition: ProcedureDefinition,
        *,
        supersedes_procedure_id: str,
    ) -> ProcedureRecord:
        try:
            result = self._proposer_client.propose_procedure(
                self._instance_id,
                definition=definition.model_dump(mode="json", by_alias=True, exclude_none=True),
                supersedes_procedure_id=supersedes_procedure_id,
            )
        except ClientCoreError as exc:
            raise ConfigError(str(exc)) from exc
        return _procedure_from_result(result)

    def accept_procedure(self, procedure: ProcedureRecord) -> ProcedureRecord:
        if self._reviewer_client is None:
            raise AssertionError("propose-only migration cannot accept a procedure")
        try:
            result = self._reviewer_client.resolve_procedure(
                self._instance_id,
                procedure.procedure_id,
                action="accept",
                expected_version=procedure.version,
            )
        except ClientCoreError as exc:
            raise ConfigError(str(exc)) from exc
        return _procedure_from_result(result)


def _migration_client(token: str) -> CruxibleClient:
    obj = _root_ctx_obj()
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if not server_url and not server_socket:
        raise click.UsageError(
            "cruxible migrate requires server mode so each write is authenticated "
            "by its supplied runtime credential"
        )
    return CruxibleClient(
        base_url=str(server_url) if server_url else None,
        socket_path=str(server_socket) if server_socket else None,
        token=token,
    )


def _credential_id_from_token(token: str, *, option: str) -> str:
    match = _RUNTIME_CREDENTIAL_TOKEN.match(token)
    if match is None:
        raise click.BadParameter(
            "expected a Cruxible runtime credential token",
            param_hint=option,
        )
    return match.group(1)


def _migration_actor_identities(
    proposer_client: CruxibleClient,
    reviewer_client: CruxibleClient,
    *,
    instance_id: str,
    proposer_token: str,
    reviewer_token: str,
) -> tuple[ProcedureMigrationActorIdentity, ProcedureMigrationActorIdentity]:
    """Resolve both authoritative actors, including reviewer auth, before writes."""
    proposer_id = _credential_id_from_token(
        proposer_token,
        option="--proposer-credential",
    )
    reviewer_id = _credential_id_from_token(
        reviewer_token,
        option="--reviewer-credential",
    )
    credential_rows = proposer_client.list_runtime_credentials(instance_id).credentials
    credentials = {row.credential_id: row for row in credential_rows}
    missing = [
        credential_id
        for credential_id in (proposer_id, reviewer_id)
        if credential_id not in credentials
    ]
    if missing:
        raise click.ClickException(
            "Migration credential preflight could not resolve active credential metadata for "
            + ", ".join(missing)
            + "; no writes were attempted"
        )
    proposer = credentials[proposer_id]
    reviewer = credentials[reviewer_id]
    inactive = [row.credential_id for row in (proposer, reviewer) if row.revoked_at is not None]
    if inactive:
        raise click.ClickException(
            "Migration credential preflight found revoked credential(s) "
            + ", ".join(inactive)
            + "; no writes were attempted"
        )
    if proposer.instance_id != instance_id or reviewer.instance_id != instance_id:
        raise click.ClickException(
            "Migration credentials must both be scoped to the selected instance; "
            "no writes were attempted"
        )
    permission_rank = {"read_only": 1, "governed_write": 2, "graph_write": 3, "admin": 4}
    if permission_rank[reviewer.permission_mode] < permission_rank["graph_write"]:
        raise click.ClickException(
            f"Reviewer credential {reviewer.credential_id} requires graph_write permission; "
            "no writes were attempted"
        )

    # Authenticate and scope-check the reviewer before proposing anything.  A
    # forged token that merely embeds a real credential id cannot pass this
    # existing read verb.
    reviewer_client.list_procedures(instance_id, status="pending", limit=1, offset=0)
    return (
        ProcedureMigrationActorIdentity(org_id=proposer.instance_id, actor_id=proposer.label),
        ProcedureMigrationActorIdentity(org_id=reviewer.instance_id, actor_id=reviewer.label),
    )


def _run_items(result: Any) -> list[ProcedureRun]:
    return [ProcedureRun.model_validate(item) for item in result.items]


def _format_linked_outcomes(summary: LinkedOutcomeSummary | None) -> str:
    """Render one line of linked-outcome reading counts, grade by grade.

    Grades stay apart and no grain is summed with another because the model
    exposes no such total: each grain observes a different subject, so a
    combined count would advertise a sample size no subject has.
    """
    if summary is None:
        return "null"
    return "; ".join(
        f"{grain} contract={counts.contract_grade.readings} "
        f"attestation={counts.attestation_grade.readings}"
        for grain, counts in (
            ("procedure_unit", summary.procedure_unit),
            ("node", summary.node),
            ("arm", summary.arm),
        )
    )


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
    if isinstance(procedure, ProcedureReadRecord):
        track_record = procedure.track_record
        last_succeeded_at = (
            track_record.last_succeeded_at.isoformat()
            if track_record.last_succeeded_at is not None
            else "null"
        )
        top_refusal_reason = track_record.top_refusal_reason or "null"
        click.echo(
            "  Track record: "
            f"runs={track_record.runs}, succeeded={track_record.succeeded}, "
            f"failed={track_record.failed}, refused={track_record.refused}, "
            f"budget_exceeded={track_record.budget_exceeded}, "
            f"in_flight={track_record.in_flight}"
        )
        click.echo(
            f"    last_succeeded_at={last_succeeded_at}, "
            f"top_refusal_reason={top_refusal_reason}, "
            f"linked_outcomes={_format_linked_outcomes(track_record.linked_outcomes)}"
        )


def _procedure_payload(procedure: ProcedureRecord) -> dict[str, Any]:
    return procedure.model_dump(mode="json", by_alias=True, exclude_none=True)


def _echo_migration_result(result: ProcedureMigrationResult) -> None:
    click.echo(
        f"Procedure migration {result.mode}: {len(result.items)} live v1 procedure(s); "
        f"propose_only={str(result.propose_only).lower()}"
    )
    for item in result.items:
        successor = item.successor_procedure_id or "not-created"
        click.echo(f"  {item.name}: {item.outcome} ({item.dedupe_disposition})")
        click.echo(f"    Lineage: {item.predecessor_procedure_id} -> {successor}")
        click.echo(
            "    Lift diff: "
            f"graph_format {item.graph_format_before} -> {item.graph_format_after}; "
            f"changed_fields={','.join(item.changed_fields)}; "
            f"steps_changed={str(item.steps_changed).lower()}; "
            "node_local_digests_unchanged="
            f"{str(item.node_local_digests_unchanged).lower()}"
        )
        click.echo(
            "    Definition digest: "
            f"{item.definition_digest_before} -> {item.definition_digest_after or 'not-computed'}"
        )
        click.echo(f"    Dedupe disposition: {item.dedupe_disposition}")
        if item.refusal:
            click.echo(f"    Refusal: {item.refusal}")
    click.echo(f"Reading continuity: {result.reading_continuity}")


@click.command("migrate")
@click.option(
    "--proposer-credential",
    required=True,
    help=(
        "Runtime bearer credential used for ordinary lift proposals; supervised apply "
        "requires admin permission for the identity preflight."
    ),
)
@click.option(
    "--reviewer-credential",
    default=None,
    help="Distinct runtime bearer credential used for ordinary acceptance.",
)
@click.option(
    "--dry-run/--apply",
    "dry_run",
    default=True,
    show_default=True,
    help="Report the convergence plan or execute it through governed lifecycle verbs.",
)
@handle_errors
def migrate_cmd(
    proposer_credential: str,
    reviewer_credential: str | None,
    dry_run: bool,
) -> None:
    """Converge live v1 procedures through supervised v2 re-acceptance."""
    instance_id = _require_instance_id()
    proposer_client = _migration_client(proposer_credential)
    reviewer_client: CruxibleClient | None = None
    try:
        proposer_identity: ProcedureMigrationActorIdentity | None = None
        reviewer_identity: ProcedureMigrationActorIdentity | None = None
        if reviewer_credential is not None:
            reviewer_client = _migration_client(reviewer_credential)
        if not dry_run and reviewer_client is not None:
            assert reviewer_credential is not None
            proposer_identity, reviewer_identity = _migration_actor_identities(
                proposer_client,
                reviewer_client,
                instance_id=instance_id,
                proposer_token=proposer_credential,
                reviewer_token=reviewer_credential,
            )
        surface = _RemoteProcedureMigrationSurface(
            instance_id,
            proposer_client=proposer_client,
            reviewer_client=reviewer_client,
        )
        result = run_procedure_migration(
            surface,
            apply=not dry_run,
            proposer_identity=proposer_identity,
            reviewer_identity=reviewer_identity,
        )
    finally:
        proposer_client.close()
        if reviewer_client is not None:
            reviewer_client.close()
    _echo_migration_result(result)


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
    for code, message in _transition_warnings(result):
        click.echo(f"  Warning [{code}]: {message}")


@procedure_group.command("list")
@click.option(
    "--status",
    type=click.Choice(["pending", "live", "rejected", "retired", "withdrawn"]),
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
        lambda instance: service_get_procedure_details(instance, procedure_id),
    )
    procedure = _procedure_from_result(result)
    if output_json:
        _emit_json(
            {
                "procedure": _procedure_payload(procedure),
                "contract_in_schema": _contract_in_schema(result),
                "control_paths": _control_paths(result),
            }
        )
        return
    _echo_procedure(procedure)
    _echo_contract_in_schema(_contract_in_schema(result))
    _echo_control_paths(_control_paths(result))
    click.echo("  Definition:")
    click.echo(
        yaml.safe_dump(
            procedure.definition.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    )


def _echo_contract_in_schema(schema: dict[str, Any] | None) -> None:
    """Print the resolved input shape a caller must satisfy to run this procedure."""
    if schema is None:
        click.echo("  Input schema: unresolved (contract_in is not defined in the active config)")
        return
    fields = schema.get("fields") or []
    allow_extra = bool(schema.get("allow_extra"))
    description = schema.get("description")
    if isinstance(description, str) and description:
        click.echo(f"  Input contract: {description}")
    if not fields:
        click.echo(
            "  Input: any JSON object" if allow_extra else "  Input: none (empty payload)",
        )
        _echo_input_example(schema)
        return
    click.echo("  Input schema:")
    for field in fields:
        parts = [str(field.get("type"))]
        parts.append("required" if field.get("required") else "optional")
        if field.get("default") is not None:
            parts.append(f"default={field['default']!r}")
        click.echo(f"    {field.get('name')} ({', '.join(parts)})")
        field_description = field.get("description")
        if isinstance(field_description, str) and field_description:
            click.echo(f"      {field_description}")
        json_schema = field.get("json_schema")
        if isinstance(json_schema, dict):
            click.echo(f"      json_schema: {json.dumps(json_schema, sort_keys=True)}")
    if allow_extra:
        click.echo("    (extra fields accepted)")
    _echo_input_example(schema)


def _echo_input_example(schema: dict[str, Any]) -> None:
    """Print the worked payload a caller can paste, when the contract takes one."""
    if "input_example" not in schema:
        return
    example = schema["input_example"]
    if not isinstance(example, dict):
        return
    click.echo("  Input example:")
    click.echo(f"    {json.dumps(example, sort_keys=True)}")


@procedure_group.command("resolve")
@click.argument("procedure_id")
@click.option(
    "--action",
    type=click.Choice(["accept", "reject"]),
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
    """Accept or reject one pending procedure."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.resolve_procedure(
            instance_id,
            procedure_id,
            action=action,
            expected_version=expected_version,
            reason=reason,
        ),
        lambda instance: (
            service_accept_procedure(
                instance,
                procedure_id,
                expected_version=expected_version,
                actor_context=None,
            )
            if action == "accept"
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


@procedure_group.command("withdraw")
@click.argument("procedure_id")
@click.option("--expected-version", required=True, type=click.IntRange(min=1))
@click.option("--reason", default=None, help="Optional note on why it was withdrawn.")
@handle_errors
def procedure_withdraw(
    procedure_id: str,
    expected_version: int,
    reason: str | None,
) -> None:
    """Withdraw your own pending proposal, freeing its name to re-propose."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.withdraw_procedure(
            instance_id,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
        ),
        lambda instance: service_withdraw_procedure(
            instance,
            procedure_id,
            expected_version=expected_version,
            reason=reason,
            actor_context=None,
        ),
        allow_local=False,
        command_name="procedure withdraw",
    )
    procedure = _procedure_from_result(result)
    click.echo(f"Procedure {procedure.procedure_id} withdrawn.")
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
@click.option("--dry-run", is_flag=True, help="Preview the run without landing group proposals.")
@json_option
@handle_errors
def procedure_run(
    procedure_id: str,
    input_json: str,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Run one live procedure through the generic procedure executor."""
    input_payload = _parse_run_input(input_json)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.run_procedure(
            instance_id,
            procedure_id,
            input_payload=input_payload,
            dry_run=dry_run,
        ),
        lambda instance: service_run_procedure(
            instance,
            procedure_id,
            input_payload,
            None,
            dry_run=dry_run,
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


@procedure_group.command("record-reading")
@click.argument("procedure_id")
@click.option(
    "--subject-grain",
    required=True,
    type=click.Choice(["procedure_unit", "node", "arm"]),
)
@click.option("--grade", required=True, type=click.Choice(["contract", "attestation"]))
@click.option(
    "--verdict",
    required=True,
    type=click.Choice(["satisfied", "contradicted", "indeterminate"]),
)
@click.option("--observed-at", required=True, help="Observation time in ISO-8601 format.")
@click.option("--node-id")
@click.option("--from-node-id")
@click.option("--arm-label", type=click.Choice(["on_true", "on_false"]))
@click.option("--measurement-name")
@click.option("--contract-id")
@click.option("--resolution-id")
@click.option("--value", "value_json", help="Observed value as JSON.")
@click.option("--run-id")
@click.option("--episode-ref")
@click.option("--situation-shape", "situation_shape_json", help="Situation shape as JSON.")
@click.option("--evidence-ref", multiple=True, help="EvidenceRef JSON; repeatable.")
@click.option("--note")
@click.option("--idempotency-key")
@json_option
@handle_errors
def procedure_record_reading(
    procedure_id: str,
    subject_grain: str,
    grade: str,
    verdict: str,
    observed_at: str,
    node_id: str | None,
    from_node_id: str | None,
    arm_label: str | None,
    measurement_name: str | None,
    contract_id: str | None,
    resolution_id: str | None,
    value_json: str | None,
    run_id: str | None,
    episode_ref: str | None,
    situation_shape_json: str | None,
    evidence_ref: tuple[str, ...],
    note: str | None,
    idempotency_key: str | None,
    output_json: bool,
) -> None:
    """Record one outcome reading without changing its requested evidence grade."""
    parsed_observed_at = parse_datetime(observed_at)
    if parsed_observed_at is None:
        raise click.BadParameter("--observed-at is required")
    situation_shape = _parse_json_value(situation_shape_json, option="--situation-shape")
    if situation_shape is not None and not isinstance(situation_shape, dict):
        raise click.BadParameter("--situation-shape must be a JSON object")
    evidence_refs = _parse_evidence_refs(evidence_ref)
    value = _parse_json_value(value_json, option="--value")
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.record_procedure_reading(
            instance_id,
            procedure_id,
            subject_grain=subject_grain,
            grade=grade,
            verdict=verdict,
            observed_at=observed_at,
            node_id=node_id,
            from_node_id=from_node_id,
            arm_label=arm_label,
            measurement_name=measurement_name,
            contract_id=contract_id,
            resolution_id=resolution_id,
            value=value,
            run_id=run_id,
            episode_ref=episode_ref,
            situation_shape=situation_shape,
            evidence_refs=[ref.model_dump(mode="python") for ref in evidence_refs],
            note=note,
            idempotency_key=idempotency_key,
        ),
        lambda instance: service_record_reading(
            instance,
            procedure_id,
            subject_grain=subject_grain,  # type: ignore[arg-type]
            grade=grade,  # type: ignore[arg-type]
            verdict=verdict,  # type: ignore[arg-type]
            observed_at=parsed_observed_at,
            actor_context=None,
            node_id=node_id,
            from_node_id=from_node_id,
            arm_label=arm_label,  # type: ignore[arg-type]
            measurement_name=measurement_name,
            contract_id=contract_id,
            resolution_id=resolution_id,
            value=value,
            run_id=run_id,
            episode_ref=episode_ref,
            situation_shape=situation_shape,
            evidence_refs=[ref.model_dump(mode="python") for ref in evidence_refs],
            note=note,
            idempotency_key=idempotency_key,
        ),
        allow_local=False,
        command_name="procedure record-reading",
    )
    payload = result.model_dump(mode="json") if isinstance(result, ProcedureReading) else result
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Procedure reading {payload['reading_id']} recorded as {payload['grade']} grade.")
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


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
    "procedure_record_reading",
    "procedure_resolve",
    "procedure_retire",
    "procedure_run",
    "procedure_runs",
    "procedure_show",
    "procedure_withdraw",
]
