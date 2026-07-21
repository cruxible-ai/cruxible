"""CLI commands for runtime credential bootstrap and management."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from typing import cast

import click

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import handle_errors
from cruxible_core.server.credentials import (
    RuntimeCredentialRecord,
    RuntimeCredentialRecoveryBusyError,
    RuntimeCredentialRecoveryError,
    RuntimeCredentialStore,
    list_runtime_credential_instance_ids,
)

_PERMISSION_MODES: tuple[contracts.RuntimeCredentialPermissionMode, ...] = (
    "admin",
    "graph_write",
    "governed_write",
    "read_only",
)


@click.group("credential")
def credential_group() -> None:
    """Manage runtime bearer credentials for a governed server instance."""


def _require_server_client(command_name: str) -> tuple[CruxibleClient, str]:
    client = _common._get_client()
    if client is None:
        raise click.UsageError(f"Local mutation disabled for {command_name}; use server mode.")
    return client, _common._require_instance_id()


def _read_bootstrap_secret(secret_file: str | None) -> str:
    if secret_file is not None:
        try:
            secret = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise click.UsageError(
                f"Could not read bootstrap secret file {secret_file}: {exc}"
            ) from exc
    else:
        secret = (os.environ.get("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET") or "").strip()

    if not secret:
        raise click.UsageError("Provide --secret-file or set CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET.")
    return secret


def _credential_metadata_from_record(
    record: RuntimeCredentialRecord,
) -> contracts.RuntimeCredentialMetadata:
    return contracts.RuntimeCredentialMetadata(
        credential_id=record.credential_id,
        instance_id=record.instance_id,
        label=record.label,
        permission_mode=cast(
            contracts.RuntimeCredentialPermissionMode,
            record.permission_mode.name.lower(),
        ),
        created_at=record.created_at,
        created_by=record.created_by,
        revoked_at=record.revoked_at,
    )


def _echo_credential_metadata(credential: contracts.RuntimeCredentialMetadata) -> None:
    click.echo(f"Credential ID: {credential.credential_id}")
    click.echo(f"Instance ID: {credential.instance_id}")
    click.echo(f"Label: {credential.label}")
    click.echo(f"Permission mode: {credential.permission_mode}")
    click.echo(f"Created at: {credential.created_at}")
    if credential.created_by:
        click.echo(f"Created by: {credential.created_by}")
    if credential.revoked_at:
        click.echo(f"Revoked at: {credential.revoked_at}")


def _echo_token_once(token: str, *, label: str) -> None:
    click.echo(f"{label}: {token}")
    click.echo("Save it now, for example: export CRUXIBLE_SERVER_BEARER_TOKEN=<token>")


@credential_group.command("claim-bootstrap")
@click.option(
    "--secret-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "File containing the runtime bootstrap secret. Defaults to "
        "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET."
    ),
)
@handle_errors
def claim_bootstrap_cmd(secret_file: str | None) -> None:
    """Exchange the one-time bootstrap secret for the first ADMIN runtime token."""
    client, instance_id = _require_server_client("credential claim-bootstrap")
    result = client.claim_runtime_bootstrap(instance_id, _read_bootstrap_secret(secret_file))

    click.echo("Bootstrap claimed.")
    click.echo(f"Credential ID: {result.credential_id}")
    click.echo(f"Instance ID: {result.instance_id}")
    click.echo(f"Permission mode: {result.permission_mode}")
    _echo_token_once(result.token, label="Admin token")


@credential_group.command("mint")
@click.option("--label", required=True, help="Human-readable credential label.")
@click.option(
    "--mode",
    "permission_mode",
    required=True,
    type=click.Choice(_PERMISSION_MODES),
    help="Credential permission mode.",
)
@handle_errors
def mint_cmd(label: str, permission_mode: str) -> None:
    """Mint a new runtime bearer credential."""
    client, instance_id = _require_server_client("credential mint")
    result = client.create_runtime_credential(
        instance_id,
        label=label,
        permission_mode=cast(contracts.RuntimeCredentialPermissionMode, permission_mode),
    )

    click.echo("Credential minted.")
    _echo_credential_metadata(result.credential)
    if result.token:
        _echo_token_once(result.token, label="Token")


@credential_group.command("list")
@handle_errors
def list_cmd() -> None:
    """List runtime bearer credentials for the active instance."""
    client, instance_id = _require_server_client("credential list")
    result = client.list_runtime_credentials(instance_id)

    if not result.credentials:
        click.echo("No runtime credentials.")
        return

    for credential in result.credentials:
        status = "revoked" if credential.revoked_at else "active"
        click.echo(
            "\t".join(
                [
                    credential.credential_id,
                    credential.permission_mode,
                    status,
                    credential.label,
                    credential.created_at,
                    credential.created_by or "",
                ]
            )
        )


def _refuse_recover_admin_server_mode() -> None:
    obj = _common._root_ctx_obj()
    if obj.get("server_url") or obj.get("server_socket") or obj.get("require_server"):
        raise click.UsageError(
            "credential recover-admin is local-only; unset --server-url/--server-socket "
            "and run it directly against --state-dir with the daemon stopped."
        )


def _require_owned_path(path: Path, *, description: str, uid: int, directory: bool) -> None:
    try:
        stat_result = os.stat(path)
    except FileNotFoundError as exc:
        raise click.UsageError(f"{description} does not exist: {path}") from exc
    except OSError as exc:
        raise click.UsageError(f"Could not inspect {description} {path}: {exc}") from exc

    if directory and not stat.S_ISDIR(stat_result.st_mode):
        raise click.UsageError(f"{description} is not a directory: {path}")
    if not directory and not stat.S_ISREG(stat_result.st_mode):
        raise click.UsageError(f"{description} is not a regular file: {path}")
    if stat_result.st_uid != uid:
        raise click.UsageError(
            f"{description} must be owned by invoking uid {uid}; "
            f"{path} is owned by uid {stat_result.st_uid}."
        )


def _select_recovery_instance_id(db_path: Path, instance_id: str | None) -> str:
    try:
        instance_ids = list_runtime_credential_instance_ids(db_path)
    except (RuntimeCredentialRecoveryBusyError, RuntimeCredentialRecoveryError) as exc:
        raise click.UsageError(str(exc)) from exc
    if not instance_ids:
        raise click.UsageError(f"No runtime credentials found in {db_path}.")
    if instance_id is not None:
        if instance_id not in instance_ids:
            raise click.UsageError(
                f"Instance ID {instance_id!r} was not found in credentials DB. "
                f"Found: {', '.join(instance_ids)}"
            )
        return instance_id
    if len(instance_ids) == 1:
        return instance_ids[0]
    raise click.UsageError(
        "Credentials DB contains multiple instance IDs; pass --instance-id. "
        f"Found: {', '.join(instance_ids)}"
    )


@credential_group.command("recover-admin")
@click.option(
    "--state-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Server state directory containing runtime_credentials.db. Stop the daemon "
        "first; the lock check only refuses a writer caught mid-transaction and "
        "does not detect an idle running daemon."
    ),
)
@click.option(
    "--instance-id",
    default=None,
    help="Target instance ID when the credentials DB contains multiple instances.",
)
@click.option(
    "--label",
    default="recovered-admin",
    show_default=True,
    help="Human-readable label for the recovered ADMIN credential.",
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def recover_admin_cmd(
    state_dir: Path,
    instance_id: str | None,
    label: str,
    output_json: bool,
) -> None:
    """Recover an ADMIN token by local filesystem ownership of server state.

    Trust model: this local-only command never contacts a Cruxible server. It
    treats ownership of --state-dir and its runtime_credentials.db by the
    invoking uid as authority to mint one new ADMIN runtime credential directly
    in that DB. Stop the daemon first: the BEGIN IMMEDIATE check refuses a
    writer caught mid-transaction but cannot detect an idle running daemon,
    so operator discipline is the real guarantee.
    Existing credentials are not revoked automatically.
    """
    _refuse_recover_admin_server_mode()
    resolved_state_dir = state_dir.expanduser().resolve()
    db_path = resolved_state_dir / "runtime_credentials.db"
    uid = os.getuid()
    _require_owned_path(resolved_state_dir, description="State dir", uid=uid, directory=True)
    _require_owned_path(db_path, description="Runtime credentials DB", uid=uid, directory=False)
    resolved_instance_id = _select_recovery_instance_id(db_path, instance_id)
    _common._echo_explicit_write_target(resolved_instance_id, resolved_state_dir)

    store = RuntimeCredentialStore(db_path, initialize=False)
    try:
        result = store.recover_admin_credential(
            instance_id=resolved_instance_id,
            label=label,
            uid=uid,
            hostname=socket.gethostname(),
        )
    except RuntimeCredentialRecoveryBusyError as exc:
        raise click.UsageError(str(exc)) from exc
    except RuntimeCredentialRecoveryError as exc:
        raise click.UsageError(str(exc)) from exc

    credential = _credential_metadata_from_record(result.record)
    if output_json:
        _common._emit_json(
            {
                "credential": credential.model_dump(mode="json"),
                "token": result.token,
                "existing_credentials_revoked": False,
                "next_step": (
                    "Restart the daemon with auth enabled. Revoke old admin credentials "
                    "after recovery if desired."
                ),
            }
        )
        return

    click.echo("Admin credential recovered.")
    _echo_credential_metadata(credential)
    _echo_token_once(result.token, label="Admin token")
    click.echo(
        "Existing admin credentials were not revoked. Restart the daemon with auth "
        "enabled, then revoke old admin credentials if desired."
    )


@credential_group.command("revoke")
@click.argument("credential_id")
@handle_errors
def revoke_cmd(credential_id: str) -> None:
    """Revoke a runtime bearer credential."""
    client, instance_id = _require_server_client("credential revoke")
    result = client.revoke_runtime_credential(instance_id, credential_id)

    click.echo("Credential revoked.")
    _echo_credential_metadata(result.credential)


@credential_group.command("rotate")
@click.argument("credential_id")
@handle_errors
def rotate_cmd(credential_id: str) -> None:
    """Rotate a runtime bearer credential and print the replacement token once."""
    client, instance_id = _require_server_client("credential rotate")
    result = client.rotate_runtime_credential(instance_id, credential_id)

    click.echo("Credential rotated.")
    _echo_credential_metadata(result.credential)
    if result.token:
        _echo_token_once(result.token, label="Token")
