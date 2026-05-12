"""CLI commands for init, validate, workflows, and snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.commands import _common
from cruxible_core.cli.commands._common import (
    _dispatch_cli,
    _dispatch_cli_instance,
    _emit_json,
    _get_client,
    _operation_context,
    _print_apply_previews,
    _remember_server_context,
    _resolve_decision_record_id,
    _resolve_workflow_input,
    decision_record_option,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.receipt.types import Receipt
from cruxible_core.server.config import is_agent_mode
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
                "created_at": reference.created_at.isoformat(),
                "apply_previews": reference.apply_previews,
            }
    raise click.UsageError(
        f"No stored canonical preview found for workflow '{workflow_name}'. "
        "Run the workflow first, or pass --preview-file/--apply-digest explicitly."
    )


def _is_interactive_apply() -> bool:
    return click.get_text_stream("stdin").isatty() and not is_agent_mode()


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
@click.option("--kit", default=None, help="Standalone kit alias or ref to materialize.")
@click.option(
    "--root-dir",
    default=None,
    help="Workspace root for config/artifact provenance (defaults to current directory).",
)
@click.option("--data-dir", default=None, help="Directory for data files.")
@handle_errors
def init(
    config_path: str | None,
    kit: str | None,
    root_dir: str | None,
    data_dir: str | None,
) -> None:
    """Initialize a new instance or governed server-backed workspace."""
    client = _common._get_client()
    effective_root_dir = root_dir
    if client is not None and effective_root_dir is None:
        effective_root_dir = str(Path.cwd())

    def _remote_init(client) -> contracts.InitResult:
        kwargs = {
            "root_dir": effective_root_dir or str(Path.cwd()),
            "config_yaml": (
                _common._read_validation_yaml_or_error(config_path)
                if config_path is not None
                else None
            ),
            "data_dir": data_dir,
        }
        if kit is not None:
            kwargs["kit"] = kit
        return client.init(**kwargs)

    result = _dispatch_cli(
        _remote_init,
        lambda: service_init(
            Path(effective_root_dir) if effective_root_dir is not None else Path.cwd(),
            config_path=config_path,
            data_dir=data_dir,
            kit=kit,
        ),
        allow_local=False,
        command_name="init",
    )
    if isinstance(result, contracts.InitResult):
        _remember_server_context(instance_id=result.instance_id)
        click.echo(f"Instance {result.status}.")
        click.echo(f"Instance ID: {result.instance_id}")
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
@handle_errors
def lock_cmd(force: bool) -> None:
    """Generate a workflow lock file for the current instance config."""
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
    decision_kwargs = (
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
        _emit_json({
            "workflow": result.workflow,
            "mode": result.mode,
            "workflow_type": result.workflow_type,
            "canonical": result.canonical,
            "apply_digest": result.apply_digest,
            "head_snapshot_id": result.head_snapshot_id,
            "receipt_id": result.receipt_id,
            "trace_ids": result.trace_ids or [],
            "output": result.output,
        })
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
                value is not None
                for value in (input_text, input_file, apply_digest, head_snapshot)
            ):
                raise click.UsageError(
                    "--from-last-preview cannot be combined with --input, --input-file, "
                    "--apply-digest, or --head-snapshot"
                )
        elif any(value is not None for value in (input_text, input_file, head_snapshot)):
            raise click.UsageError(
                "--apply-digest is required when passing --input, --input-file, or "
                "--head-snapshot"
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
    decision_kwargs = (
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
        _emit_json({
            "workflow": result.workflow,
            "mode": result.mode,
            "workflow_type": result.workflow_type,
            "canonical": result.canonical,
            "apply_digest": result.apply_digest,
            "head_snapshot_id": result.head_snapshot_id,
            "committed_snapshot_id": result.committed_snapshot_id,
            "receipt_id": result.receipt_id,
            "trace_ids": result.trace_ids or [],
            "output": result.output,
        })
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
    decision_kwargs = (
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
        _emit_json({
            "workflow": result.workflow,
            "mode": result.mode,
            "workflow_type": result.workflow_type,
            "canonical": result.canonical,
            "group_id": result.group_id,
            "status": result.group_status,
            "suppressed": result.suppressed,
            "suppressed_members": [
                {
                    "relationship_type": item.relationship_type,
                    "from_type": item.from_type,
                    "from_id": item.from_id,
                    "to_type": item.to_type,
                    "to_id": item.to_id,
                    "reason": item.reason,
                    "existing_group_id": item.existing_group_id,
                    "existing_group_status": item.existing_group_status,
                    "existing_signature": item.existing_signature,
                    "source_workflow_name": item.source_workflow_name,
                }
                for item in result.suppressed_members
            ],
            "receipt_id": result.receipt_id,
            "trace_ids": result.trace_ids or [],
            "output": result.output,
        })
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
    """Manage immutable world-model snapshots."""


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
@handle_errors
def snapshot_list_cmd() -> None:
    """List snapshots for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_snapshots(instance_id),
        service_list_snapshots,
    )

    if not result.snapshots:
        click.echo("No snapshots found.")
        return

    for snapshot in result.snapshots:
        label = f" label={snapshot.label}" if snapshot.label else ""
        click.echo(f"{snapshot.snapshot_id} {snapshot.created_at}{label}")


@click.command("clone")
@click.option("--snapshot", "snapshot_id", required=True, help="Snapshot ID to clone from.")
@click.option("--root-dir", required=True, help="Root directory for the new cloned instance.")
@handle_errors
def clone_cmd(snapshot_id: str, root_dir: str) -> None:
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
        _remember_server_context(instance_id=result.instance_id)
        click.echo(
            f"Cloned snapshot {result.snapshot.snapshot_id} into instance {result.instance_id}"
        )
        return
    click.echo(
        f"Cloned snapshot {result.snapshot.snapshot_id} into {result.instance.get_root_path()}"
    )
