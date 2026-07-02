"""CLI commands for runtime credential bootstrap and management."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import click

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import handle_errors

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
        raise click.UsageError(
            "Provide --secret-file or set CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET."
        )
    return secret


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
                ]
            )
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
