"""CLI commands for init, validate, workflows, and snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click
from pydantic import BaseModel, ValidationError

from cruxible_client import CruxibleClient, contracts
from cruxible_client.errors import AuthenticationError as ClientAuthenticationError
from cruxible_core.cli.commands import _common
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _dispatch_cli_instance,
    _emit_json,
    _get_client,
    _operation_context,
    _print_active_instance_change,
    _print_active_instance_unchanged,
    _print_apply_previews,
    _resolve_decision_record_id,
    _resolve_workflow_input,
    decision_record_option,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    apply_preview_reference_from_receipt,
    service_apply_workflow,
    service_clone_snapshot,
    service_create_snapshot,
    service_init,
    service_list_snapshots,
    service_lock,
    service_plan,
    service_propose_workflow,
    service_run,
    service_test,
    service_validate,
)
from cruxible_core.temporal import format_datetime


@dataclass(frozen=True)
class _KitLockResult:
    lock_path: Path
    lock_digest: str
    providers_locked: int
    artifacts_locked: int


def _ensure_kit_dir_is_local_only() -> None:
    ctx = click.get_current_context(silent=True)
    root_params = ctx.find_root().params if ctx is not None else {}
    if (
        root_params.get("server_url")
        or root_params.get("server_socket")
        or root_params.get("instance_id")
    ):
        raise click.UsageError(
            "--kit-dir is a pure local lock mode and cannot be combined with "
            "server mode or --instance-id."
        )


def _lock_kit_dir(kit_dir: Path, *, force: bool) -> _KitLockResult:
    kit_root = kit_dir.resolve()
    config_path = kit_root / "config.yaml"
    if not config_path.exists():
        raise click.UsageError(f"--kit-dir must contain config.yaml: {config_path}")

    # Preserve the CLI's working import order before importing compiler machinery.
    import cruxible_core.runtime  # noqa: F401
    from cruxible_core.workflow.compiler import LOCK_FILE_NAME, build_kit_root_lock, write_lock

    # Kit-root locks pin the kit LAYER only (no manifest composition): a
    # composed lock would embed base-kit providers and machine-absolute
    # artifact URIs, which is wrong for a committed, distributable kit dir.
    lock = build_kit_root_lock(kit_root, force=force)

    lock_path = kit_root / LOCK_FILE_NAME
    write_lock(lock, lock_path)
    if lock.lock_digest is None:
        raise AssertionError("build_lock returned a lock without a digest")
    return _KitLockResult(
        lock_path=lock_path,
        lock_digest=lock.lock_digest,
        providers_locked=len(lock.providers),
        artifacts_locked=len(lock.artifacts),
    )


def _write_preview_file(
    preview_path: Path,
    *,
    workflow: str,
    input_payload: dict[str, Any],
    apply_digest: str,
    head_snapshot_id: str | None,
    apply_previews: dict[str, Any],
) -> None:
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(
        json.dumps(
            {
                "kind": "workflow_preview",
                "version": 1,
                "workflow": workflow,
                "input": input_payload,
                "apply_digest": apply_digest,
                "head_snapshot_id": head_snapshot_id,
                "apply_previews": apply_previews,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _load_preview_file(preview_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(preview_path.read_text())
    except OSError as exc:
        raise click.UsageError(f"Could not read preview file '{preview_path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Preview file '{preview_path}' is not valid JSON") from exc

    if not isinstance(raw, dict):
        raise click.UsageError(f"Preview file '{preview_path}' must contain a JSON object")
    if raw.get("kind") != "workflow_preview":
        raise click.UsageError(f"Preview file '{preview_path}' has unsupported kind")
    if raw.get("version") != 1:
        raise click.UsageError(f"Preview file '{preview_path}' has unsupported version")

    workflow = raw.get("workflow")
    input_payload = raw.get("input")
    apply_digest = raw.get("apply_digest")
    head_snapshot_id = raw.get("head_snapshot_id")

    if not isinstance(workflow, str) or not workflow:
        raise click.UsageError(f"Preview file '{preview_path}' is missing workflow")
    if not isinstance(input_payload, dict):
        raise click.UsageError(f"Preview file '{preview_path}' has invalid input payload")
    if not isinstance(apply_digest, str) or not apply_digest:
        raise click.UsageError(f"Preview file '{preview_path}' is missing apply_digest")
    if head_snapshot_id is not None and not isinstance(head_snapshot_id, str):
        raise click.UsageError(f"Preview file '{preview_path}' has invalid head_snapshot_id")

    return {
        "workflow": workflow,
        "input": input_payload,
        "apply_digest": apply_digest,
        "head_snapshot_id": head_snapshot_id,
    }


def _load_latest_preview_from_client(
    client: Any,
    instance_id: str,
    workflow_name: str,
) -> dict[str, Any]:
    result = client.list(
        instance_id,
        resource_type="receipts",
        query_name=workflow_name,
        operation_type="workflow",
        limit=50,
    )
    for item in result.items:
        receipt_id = item.get("receipt_id")
        if not isinstance(receipt_id, str):
            continue
        try:
            receipt = Receipt.model_validate(client.receipt(instance_id, receipt_id))
        except ValidationError:
            continue
        reference = apply_preview_reference_from_receipt(receipt)
        if reference is not None:
            return {
                "workflow": workflow_name,
                "input": reference.input_payload,
                "apply_digest": reference.apply_digest,
                "head_snapshot_id": reference.head_snapshot_id,
                "receipt_id": reference.receipt_id,
                "created_at": format_datetime(reference.created_at),
                "apply_previews": reference.apply_previews,
            }
    raise click.UsageError(
        f"No stored canonical preview found for workflow '{workflow_name}'. "
        "Run the workflow first, or pass --preview-file/--apply-digest explicitly."
    )


def _is_interactive_apply() -> bool:
    return click.get_text_stream("stdin").isatty()


def _print_preview_reference(reference: dict[str, Any]) -> None:
    click.echo(f"Using preview receipt: {reference['receipt_id']}")
    if reference.get("created_at"):
        click.echo(f"Preview time: {reference['created_at']}")
    click.echo(f"Apply digest: {reference['apply_digest']}")
    head_snapshot_id = reference.get("head_snapshot_id")
    if head_snapshot_id:
        click.echo(f"Head snapshot: {head_snapshot_id}")
    _print_apply_previews(reference.get("apply_previews") or {})


@click.command()
@click.option("--config", "config_path", default=None, help="Path to config YAML file.")
@click.option(
    "--kit",
    "kits",
    multiple=True,
    help=(
        "Kit alias or ref to materialize; repeatable. Order is composition order: "
        "a standalone base kit first, overlay kits after."
    ),
)
@click.option(
    "--root-dir",
    default=None,
    help="Workspace root for config/artifact provenance (defaults to current directory).",
)
@click.option("--data-dir", default=None, help="Directory for data files.")
@click.option(
    "--bootstrap",
    is_flag=True,
    help="Use hosted kit init authorized by the runtime bootstrap bearer.",
)
@click.option(
    "--activate/--no-activate",
    default=True,
    help="Make a new server instance the active CLI context instance.",
)
@handle_errors
def init(
    config_path: str | None,
    kits: tuple[str, ...],
    root_dir: str | None,
    data_dir: str | None,
    bootstrap: bool,
    activate: bool,
) -> None:
    """Initialize a new instance or governed server-backed workspace."""
    client = _common._get_client()
    effective_root_dir = root_dir
    if client is not None and effective_root_dir is None:
        effective_root_dir = str(Path.cwd())
    kit_args = " ".join(f"--kit {value}" for value in kits)

    if bootstrap:
        if client is None:
            raise click.UsageError("--bootstrap requires server mode.")
        if not kits:
            raise click.UsageError("--bootstrap requires --kit.")
        if config_path is not None or data_dir is not None or root_dir is not None:
            raise click.UsageError(
                "--bootstrap uses hosted kit init and accepts only --kit and --activate."
            )

    def _remote_init(client: CruxibleClient) -> contracts.InitResult:
        init_kwargs: dict[str, Any] = {
            "root_dir": effective_root_dir or str(Path.cwd()),
            "config_yaml": (
                _common._read_validation_yaml_or_error(config_path)
                if config_path is not None
                else None
            ),
            "data_dir": data_dir,
        }
        if kits:
            init_kwargs["kits"] = list(kits)
        try:
            return client.init(**init_kwargs)
        except ClientAuthenticationError as exc:
            if kits:
                raise click.UsageError(
                    "Server auth rejected plain init. For first auth-enabled kit bootstrap, "
                    "set CRUXIBLE_SERVER_BEARER_TOKEN to the bootstrap secret and run "
                    f"`cruxible init {kit_args} --bootstrap`, then claim the admin token "
                    "with `cruxible credential claim-bootstrap`."
                ) from exc
            raise

    if bootstrap:
        assert client is not None
        assert kits
        try:
            hosted_result = client.init_hosted_instance(source_type="kit", kit_refs=list(kits))
        except ClientAuthenticationError as exc:
            raise click.ClickException(
                "Server auth rejected hosted bootstrap init. If the bootstrap secret "
                "was already claimed, use the admin token printed by "
                "`cruxible credential claim-bootstrap`: set "
                "CRUXIBLE_SERVER_BEARER_TOKEN to that ADMIN token and use normal "
                "commands, or mint more credentials with `cruxible credential mint`. "
                "If the bearer is missing or wrong, set CRUXIBLE_SERVER_BEARER_TOKEN "
                "to the BOOTSTRAP secret and retry "
                f"`cruxible init {kit_args} --bootstrap`."
            ) from exc
        click.echo(f"Instance {hosted_result.status}.")
        click.echo(f"Instance ID: {hosted_result.instance_id}")
        click.echo(f"Source: {hosted_result.source_type} {hosted_result.source_ref}")
        if activate:
            _print_active_instance_change(_activate_server_instance(hosted_result.instance_id))
        else:
            _print_active_instance_unchanged()
        for warning in hosted_result.warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")
        return

    result = _dispatch_cli(
        _remote_init,
        lambda: service_init(
            Path(effective_root_dir) if effective_root_dir is not None else Path.cwd(),
            config_path=config_path,
            data_dir=data_dir,
            kits=list(kits),
        ),
        allow_local=False,
        command_name="init",
    )
    if isinstance(result, contracts.InitResult):
        click.echo(f"Instance {result.status}.")
        click.echo(f"Instance ID: {result.instance_id}")
        if activate:
            _print_active_instance_change(_activate_server_instance(result.instance_id))
        else:
            _print_active_instance_unchanged()
        for warning in result.warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")
        return

    root = Path(effective_root_dir) if effective_root_dir is not None else Path.cwd()
    click.echo(f"Initialized .cruxible/ in {root}")
    for warning in result.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")


@click.command()
@click.option("--config", "config_path", required=True, help="Path to config YAML file.")
@handle_errors
def validate(config_path: str) -> None:
    """Validate a config YAML file without creating an instance."""
    result = _dispatch_cli(
        lambda client: client.validate(
            config_yaml=_common._read_validation_yaml_or_error(config_path)
        ),
        lambda: service_validate(config_path=config_path),
    )
    if isinstance(result, contracts.ValidateResult):
        click.echo(f"Config '{result.name}' is valid.")
        click.echo(
            f"  {len(result.entity_types)} entity types, "
            f"{len(result.relationships)} relationships, "
            f"{len(result.named_queries)} queries"
        )
        for warning in result.warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")
        return

    config = result.config
    click.echo(f"Config '{config.name}' is valid.")
    click.echo(
        f"  {len(config.entity_types)} entity types, "
        f"{len(config.relationships)} relationships, "
        f"{len(config.named_queries)} queries"
    )
    for warning in result.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")


@click.command("lock")
@click.option(
    "--force",
    is_flag=True,
    help="Accept live canonical artifact hashes when regenerating the lock.",
)
@click.option(
    "--kit-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Build a kit-root cruxible.lock.yaml from KIT_DIR/config.yaml without an instance.",
)
@handle_errors
def lock_cmd(force: bool, kit_dir: Path | None) -> None:
    """Generate a workflow lock file for the current instance config."""
    if kit_dir is not None:
        _ensure_kit_dir_is_local_only()
        kit_result = _lock_kit_dir(kit_dir, force=force)
        click.echo(f"Wrote lock file to {kit_result.lock_path}")
        click.echo(
            f"  digest={kit_result.lock_digest} providers={kit_result.providers_locked} "
            f"artifacts={kit_result.artifacts_locked}"
        )
        return

    remote = _get_client() is not None
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.workflow_lock(instance_id, force=force),
        lambda instance: service_lock(instance, force=force),
    )
    if remote:
        click.echo("Workflow lock updated on server.")
    else:
        click.echo(f"Wrote lock file to {result.lock_path}")
    click.echo(
        f"  digest={result.config_digest} providers={result.providers_locked} "
        f"artifacts={result.artifacts_locked}"
    )


@click.command("plan")
@click.option("--workflow", "workflow_name", required=True, help="Workflow name from config.")
@click.option("--input", "input_text", default=None, help="Inline JSON or YAML workflow input.")
@click.option(
    "--input-file",
    default=None,
    type=click.Path(exists=True),
    help="JSON or YAML file providing workflow input.",
)
@handle_errors
def plan_cmd(workflow_name: str, input_text: str | None, input_file: str | None) -> None:
    """Compile a workflow plan for the current instance."""
    payload = _resolve_workflow_input(input_text=input_text, input_file=input_file)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.workflow_plan(
            instance_id,
            workflow_name=workflow_name,
            input_payload=payload,
        ),
        lambda instance: service_plan(instance, workflow_name, payload),
    )
    if isinstance(result, contracts.WorkflowPlanResult):
        click.echo(json.dumps(result.plan, indent=2, sort_keys=True))
        return
    click.echo(result.plan.model_dump_json(indent=2))


@click.command("run")
@click.option("--workflow", "workflow_name", required=True, help="Workflow name from config.")
@click.option("--input", "input_text", default=None, help="Inline JSON or YAML workflow input.")
@click.option(
    "--input-file",
    default=None,
    type=click.Path(exists=True),
    help="JSON or YAML file providing workflow input.",
)
@click.option(
    "--save-preview",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Save preview state to a JSON file for use with apply --preview-file.",
)
@decision_record_option
@json_option
@handle_errors
def run_cmd(
    workflow_name: str,
    input_text: str | None,
    input_file: str | None,
    save_preview: Path | None,
    decision_record_id: str | None,
    output_json: bool,
) -> None:
    """Execute a workflow for the current instance.

    Canonical workflows run as previews and return apply identity values.
    For workflows that produce group proposals, use 'cruxible propose' instead.
    """
    payload = _resolve_workflow_input(input_text=input_text, input_file=input_file)
    resolved_decision_record_id = _resolve_decision_record_id(decision_record_id)
    decision_kwargs: dict[str, Any] = (
        {"decision_record_id": resolved_decision_record_id}
        if resolved_decision_record_id is not None
        else {}
    )
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.workflow_run(
            instance_id,
            workflow_name=workflow_name,
            input_payload=payload,
            **decision_kwargs,
        ),
        lambda instance: service_run(
            instance,
            workflow_name,
            payload,
            context=_operation_context(resolved_decision_record_id),
        ),
        allow_local=False,
        command_name="run",
    )
    if save_preview is not None:
        if not result.canonical or not result.apply_digest:
            raise click.ClickException(
                f"Workflow '{result.workflow}' did not produce preview state; "
                "--save-preview only works for canonical workflows."
            )
        _write_preview_file(
            save_preview,
            workflow=result.workflow,
            input_payload=payload,
            apply_digest=result.apply_digest,
            head_snapshot_id=result.head_snapshot_id,
            apply_previews=result.apply_previews,
        )
    if output_json:
        _emit_json(cast(BaseModel, result).model_dump(mode="json"))
        return
    click.echo(f"Workflow {result.workflow} completed.")
    if result.mode != "run":
        click.echo(f"Mode: {result.mode}")
    if result.apply_digest:
        click.echo(f"Apply digest: {result.apply_digest}")
    if result.head_snapshot_id:
        click.echo(f"Head snapshot: {result.head_snapshot_id}")
    _print_apply_previews(result.apply_previews)
    click.echo(f"Receipt ID: {result.receipt_id}")
    if result.query_receipt_ids:
        click.echo(f"Query receipt IDs: {', '.join(result.query_receipt_ids)}")
    if result.trace_ids:
        click.echo(f"Trace IDs: {', '.join(result.trace_ids)}")
    click.echo(json.dumps(result.output, indent=2, sort_keys=True))


@click.command("apply")
@click.option("--workflow", "workflow_name", default=None, help="Workflow name from config.")
@click.option("--input", "input_text", default=None, help="Inline JSON or YAML workflow input.")
@click.option(
    "--input-file",
    default=None,
    type=click.Path(exists=True),
    help="JSON or YAML file providing workflow input.",
)
@click.option(
    "--apply-digest",
    default=None,
    help="Preview apply digest from workflow run.",
)
@click.option(
    "--head-snapshot",
    default=None,
    help="Expected head snapshot ID from workflow preview.",
)
@click.option(
    "--preview-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read preview state from a file saved by run --save-preview.",
)
@click.option(
    "--from-last-preview",
    is_flag=True,
    help="Apply the latest stored preview for the workflow.",
)
@decision_record_option
@json_option
@handle_errors
def apply_cmd(
    workflow_name: str | None,
    input_text: str | None,
    input_file: str | None,
    apply_digest: str | None,
    head_snapshot: str | None,
    preview_file: Path | None,
    from_last_preview: bool,
    decision_record_id: str | None,
    output_json: bool,
) -> None:
    """Apply a canonical workflow after verifying preview identity."""
    instance_id: str | None = None
    if preview_file is not None:
        if (
            workflow_name is not None
            or input_text is not None
            or input_file is not None
            or apply_digest is not None
            or head_snapshot is not None
            or from_last_preview
        ):
            raise click.UsageError(
                "--preview-file cannot be combined with --workflow, --input, "
                "--input-file, --apply-digest, --head-snapshot, or --from-last-preview"
            )
        preview = _load_preview_file(preview_file)
        workflow_name = preview["workflow"]
        payload = preview["input"]
        apply_digest = preview["apply_digest"]
        head_snapshot = preview["head_snapshot_id"]
        client = None
    elif from_last_preview or apply_digest is None:
        if workflow_name is None:
            raise click.UsageError("--workflow is required unless --preview-file is used")
        if from_last_preview:
            if any(
                value is not None for value in (input_text, input_file, apply_digest, head_snapshot)
            ):
                raise click.UsageError(
                    "--from-last-preview cannot be combined with --input, --input-file, "
                    "--apply-digest, or --head-snapshot"
                )
        elif any(value is not None for value in (input_text, input_file, head_snapshot)):
            raise click.UsageError(
                "--apply-digest is required when passing --input, --input-file, or --head-snapshot"
            )
        if not from_last_preview and (output_json or not _is_interactive_apply()):
            raise click.UsageError(
                "--apply-digest, --preview-file, or --from-last-preview is required"
            )
        client = _get_client()
        if client is None:
            raise click.UsageError("Local mutation disabled for apply; use server mode.")
        instance_id = _common._require_instance_id()
        preview = _load_latest_preview_from_client(client, instance_id, workflow_name)
        payload = preview["input"]
        apply_digest = preview["apply_digest"]
        head_snapshot = preview["head_snapshot_id"]
        if not output_json:
            _print_preview_reference(preview)
        if not from_last_preview:
            click.confirm("Apply this preview?", default=False, abort=True)
    else:
        if workflow_name is None:
            raise click.UsageError("--workflow is required unless --preview-file is used")
        payload = _resolve_workflow_input(input_text=input_text, input_file=input_file)
        client = None

    assert workflow_name is not None
    assert apply_digest is not None
    resolved_decision_record_id = _resolve_decision_record_id(decision_record_id)
    decision_kwargs: dict[str, Any] = (
        {"decision_record_id": resolved_decision_record_id}
        if resolved_decision_record_id is not None
        else {}
    )
    result: Any
    if client is not None:
        assert instance_id is not None
        result = client.workflow_apply(
            instance_id,
            workflow_name=workflow_name,
            expected_apply_digest=apply_digest,
            expected_head_snapshot_id=head_snapshot,
            input_payload=payload,
            **decision_kwargs,
        )
    else:
        result = _dispatch_cli_instance(
            lambda client, instance_id: client.workflow_apply(
                instance_id,
                workflow_name=workflow_name,
                expected_apply_digest=apply_digest,
                expected_head_snapshot_id=head_snapshot,
                input_payload=payload,
                **decision_kwargs,
            ),
            lambda instance: service_apply_workflow(
                instance,
                workflow_name,
                payload,
                expected_apply_digest=apply_digest,
                expected_head_snapshot_id=head_snapshot,
                context=_operation_context(resolved_decision_record_id),
            ),
            allow_local=False,
            command_name="apply",
        )
    if output_json:
        _emit_json(cast(BaseModel, result).model_dump(mode="json"))
        return
    click.echo(f"Workflow {result.workflow} applied.")
    if result.committed_snapshot_id:
        click.echo(f"Committed snapshot: {result.committed_snapshot_id}")
    _print_apply_previews(result.apply_previews)
    click.echo(f"Receipt ID: {result.receipt_id}")
    if result.trace_ids:
        click.echo(f"Trace IDs: {', '.join(result.trace_ids)}")
    click.echo(json.dumps(result.output, indent=2, sort_keys=True))


@click.command("test")
@click.option("--name", "test_name", default=None, help="Run only a named workflow test.")
@handle_errors
def test_cmd(test_name: str | None) -> None:
    """Execute config-defined workflow tests for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.workflow_test(instance_id, name=test_name),
        lambda instance: service_test(instance, test_name=test_name),
    )
    click.echo(f"Tests: {result.passed} passed, {result.failed} failed, {result.total} total")
    for case in result.cases:
        status = "PASS" if case.passed else "FAIL"
        click.echo(f"[{status}] {case.name} ({case.workflow})")
        if case.error:
            click.echo(f"  {case.error}")
        elif case.receipt_id:
            click.echo(f"  receipt={case.receipt_id}")


@click.command("propose")
@click.option("--workflow", "workflow_name", required=True, help="Workflow name from config.")
@click.option("--input", "input_text", default=None, help="Inline JSON or YAML workflow input.")
@click.option(
    "--input-file",
    default=None,
    type=click.Path(exists=True),
    help="JSON or YAML file providing workflow input.",
)
@decision_record_option
@json_option
@handle_errors
def propose_cmd(
    workflow_name: str,
    input_text: str | None,
    input_file: str | None,
    decision_record_id: str | None,
    output_json: bool,
) -> None:
    """Execute a workflow and bridge its output into a candidate group."""
    payload = _resolve_workflow_input(input_text=input_text, input_file=input_file)
    resolved_decision_record_id = _resolve_decision_record_id(decision_record_id)
    decision_kwargs: dict[str, Any] = (
        {"decision_record_id": resolved_decision_record_id}
        if resolved_decision_record_id is not None
        else {}
    )
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.propose_workflow(
            instance_id,
            workflow_name=workflow_name,
            input_payload=payload,
            **decision_kwargs,
        ),
        lambda instance: service_propose_workflow(
            instance,
            workflow_name,
            payload,
            context=_operation_context(resolved_decision_record_id),
        ),
        allow_local=False,
        command_name="propose",
    )

    if output_json:
        _emit_json(cast(BaseModel, result).model_dump(mode="json"))
        return

    if result.group_status == "no_candidates":
        click.echo(f"Workflow {result.workflow} completed with no candidates.")
        click.echo("No candidate group was created.")
        click.echo(f"Receipt ID: {result.receipt_id}")
        if result.trace_ids:
            click.echo(f"Trace IDs: {', '.join(result.trace_ids)}")
        click.echo(json.dumps(result.output, indent=2, sort_keys=True))
        return

    if result.group_id is None or result.suppressed:
        click.echo(f"Workflow {result.workflow} produced no reviewable group.")
        click.echo(
            "Check whether prerequisite canonical or previously approved governed "
            "relationships exist before running this proposal workflow."
        )
        click.echo(f"Receipt ID: {result.receipt_id}")
        if result.trace_ids:
            click.echo(f"Trace IDs: {', '.join(result.trace_ids)}")
        if result.suppressed_members:
            click.echo(f"Suppressed members: {len(result.suppressed_members)}")
            for item in result.suppressed_members:
                click.echo(
                    "  "
                    f"{item.from_type}:{item.from_id} -[{item.relationship_type}]-> "
                    f"{item.to_type}:{item.to_id} ({item.reason})"
                )
        click.echo(json.dumps(result.output, indent=2, sort_keys=True))
        return

    click.echo(f"Workflow {result.workflow} proposed group {result.group_id}.")
    click.echo(f"Receipt ID: {result.receipt_id}")
    click.echo(f"Group status: {result.group_status} ({result.review_priority})")
    if result.trace_ids:
        click.echo(f"Trace IDs: {', '.join(result.trace_ids)}")
    if result.suppressed_members:
        click.echo(f"Suppressed members: {len(result.suppressed_members)}")
        for item in result.suppressed_members:
            click.echo(
                "  "
                f"{item.from_type}:{item.from_id} -[{item.relationship_type}]-> "
                f"{item.to_type}:{item.to_id} ({item.reason})"
            )
    click.echo(json.dumps(result.output, indent=2, sort_keys=True))


@click.group("snapshot")
def snapshot_group() -> None:
    """Manage immutable state snapshots."""


@snapshot_group.command("create")
@click.option("--label", default=None, help="Optional human label for the snapshot.")
@handle_errors
def snapshot_create_cmd(label: str | None) -> None:
    """Create an immutable full snapshot for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.create_snapshot(instance_id, label=label),
        lambda instance: service_create_snapshot(instance, label=label),
        allow_local=False,
        command_name="snapshot create",
    )

    click.echo(f"Created snapshot {result.snapshot.snapshot_id}")
    if result.snapshot.label:
        click.echo(f"  label={result.snapshot.label}")
    click.echo(f"  graph={result.snapshot.graph_digest}")


@snapshot_group.command("list")
@click.option("--limit", default=None, type=click.IntRange(min=1), help="Max snapshots to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@handle_errors
def snapshot_list_cmd(limit: int | None, offset: int) -> None:
    """List snapshots for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_snapshots(instance_id, limit=limit, offset=offset),
        lambda instance: service_list_snapshots(instance, limit=limit, offset=offset),
    )

    if not result.items:
        click.echo("No snapshots found.")
        return

    for snapshot in result.items:
        label = f" label={snapshot.label}" if snapshot.label else ""
        click.echo(f"{snapshot.snapshot_id} {snapshot.created_at}{label}")


@click.command("clone")
@click.option("--snapshot", "snapshot_id", required=True, help="Snapshot ID to clone from.")
@click.option("--root-dir", required=True, help="Root directory for the new cloned instance.")
@click.option(
    "--activate/--no-activate",
    default=True,
    help="Make the cloned server instance the active CLI context instance.",
)
@handle_errors
def clone_cmd(snapshot_id: str, root_dir: str, activate: bool) -> None:
    """Create a new local instance from a chosen snapshot."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.clone_snapshot(
            instance_id,
            snapshot_id=snapshot_id,
            root_dir=root_dir,
        ),
        lambda instance: service_clone_snapshot(instance, snapshot_id, root_dir),
        allow_local=False,
        command_name="clone",
    )
    if isinstance(result, contracts.CloneSnapshotResult):
        click.echo(
            f"Cloned snapshot {result.snapshot.snapshot_id} into instance {result.instance_id}"
        )
        if result.admin_credential is not None:
            # Auth-enabled daemon: the clone's initial ADMIN token is returned
            # exactly once (claim-bootstrap convention); surface it or it is lost.
            from cruxible_core.cli.commands.credentials import _echo_token_once

            click.echo(f"Credential ID: {result.admin_credential.credential_id}")
            _echo_token_once(result.admin_credential.token, label="Clone admin token")
        if activate:
            _print_active_instance_change(_activate_server_instance(result.instance_id))
        else:
            _print_active_instance_unchanged()
        return
    click.echo(
        f"Cloned snapshot {result.snapshot.snapshot_id} into {result.instance.get_root_path()}"
    )
