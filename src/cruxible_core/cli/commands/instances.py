"""CLI commands for same-identity instance backup and restore."""

from __future__ import annotations

from pathlib import Path

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _dispatch_cli_instance,
    _emit_json,
    _print_active_instance_change,
    _print_active_instance_unchanged,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import service_restore_instance, service_snapshot_instance


@click.group("instance")
def instance_group() -> None:
    """Back up and restore exact Cruxible instances."""


@instance_group.command("snapshot")
@click.argument("artifact_path")
@click.option("--label", default=None, help="Optional human label for the backup artifact.")
@json_option
@handle_errors
def instance_snapshot_cmd(
    artifact_path: str,
    label: str | None,
    output_json: bool,
) -> None:
    """Write a portable same-identity backup artifact for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.snapshot_instance(
            instance_id,
            artifact_path=artifact_path,
            label=label,
        ),
        lambda instance: service_snapshot_instance(
            instance,
            instance_id=str(instance.get_root_path()),
            artifact_path=artifact_path,
            label=label,
        ),
    )
    payload = (
        result.model_dump(mode="json")
        if isinstance(result, contracts.InstanceSnapshotResult)
        else {
            "instance_id": result.instance_id,
            "artifact_path": result.artifact_path,
            "manifest": result.manifest.model_dump(mode="json"),
        }
    )
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Wrote instance backup {payload['artifact_path']}")
    click.echo(f"  instance={payload['instance_id']}")
    click.echo(f"  format={payload['manifest']['format_version']}")


@instance_group.command("restore")
@click.argument("artifact_path")
@click.option("--at", "root_dir", default=None, help="Restore target root directory.")
@click.option(
    "--activate/--no-activate",
    default=True,
    help="Make the restored server instance the active CLI context instance.",
)
@json_option
@handle_errors
def instance_restore_cmd(
    artifact_path: str,
    root_dir: str | None,
    activate: bool,
    output_json: bool,
) -> None:
    """Restore a same-identity backup artifact."""
    effective_root = root_dir or str(Path.cwd())
    result = _dispatch_cli(
        lambda client: client.restore_instance(
            artifact_path=artifact_path,
            root_dir=root_dir,
        ),
        lambda: service_restore_instance(
            artifact_path=artifact_path,
            root_dir=effective_root,
        ),
    )
    payload = (
        result.model_dump(mode="json")
        if isinstance(result, contracts.InstanceRestoreResult)
        else {
            "instance_id": result.instance_id,
            "root_dir": result.root_dir,
            "manifest": result.manifest.model_dump(mode="json"),
            "registry_status": result.registry_status,
        }
    )
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Restored instance {payload['instance_id']}")
    click.echo(f"  root={payload['root_dir']}")
    click.echo(f"  registry={payload['registry_status']}")
    if isinstance(result, contracts.InstanceRestoreResult):
        if activate:
            _print_active_instance_change(_activate_server_instance(result.instance_id))
        else:
            _print_active_instance_unchanged()


def _unreachable_local_relocate() -> contracts.InstanceRelocateResult:
    """Placeholder local callback for relocate; never invoked (allow_local=False)."""
    raise AssertionError("instance relocate has no local path")


@instance_group.command("relocate")
@click.option("--to", "to_dir", required=True, help="New root directory for the instance.")
@click.option(
    "--remove-source/--keep-source",
    default=False,
    help="Delete the old directory after a successful relocate (default: keep it).",
)
@click.option(
    "--activate/--no-activate",
    default=True,
    help="Make the relocated server instance the active CLI context instance.",
)
@json_option
@handle_errors
def instance_relocate_cmd(
    to_dir: str,
    remove_source: bool,
    activate: bool,
    output_json: bool,
) -> None:
    """Move the current healthy instance to a new directory, preserving identity."""
    # No local path: allow_local=False makes _dispatch_cli raise the shared
    # "local mutation disabled; use server mode" error before this callback can
    # run. A local relocate could not drop the instance from the daemon's
    # in-process manager anyway, so server mode is required.
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.relocate_instance(
            instance_id,
            to_dir=to_dir,
            remove_source=remove_source,
        ),
        lambda instance: _unreachable_local_relocate(),
        allow_local=False,
        command_name="instance relocate",
    )
    payload = result.model_dump(mode="json")
    if output_json:
        _emit_json(payload)
        return
    click.echo(f"Relocated instance {payload['instance_id']}")
    click.echo(f"  from={payload['from_dir']}")
    click.echo(f"  to={payload['to_dir']}")
    click.echo(f"  registry={payload['registry_status']}")
    click.echo(f"  source_removed={payload['source_removed']}")
    if activate:
        _print_active_instance_change(_activate_server_instance(result.instance_id))
    else:
        _print_active_instance_unchanged()
