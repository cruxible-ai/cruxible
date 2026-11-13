"""CLI commands for runtime credential bootstrap and management."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from typing import cast

import click

from cruxible_client import CruxibleClient, contracts
from cruxible_client.authoring.signing import sign_runtime_credential_mint
from cruxible_client.contracts.runtime_credentials import RuntimeCredentialPrincipalProof
from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import handle_errors
from cruxible_core.cli.principal_settings import set_principal_settings_token
from cruxible_core.server.config import get_server_state_root
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


def sign_principal_mint(
    *,
    instance_id: str,
    principal_id: str,
    permission_mode: contracts.RuntimeCredentialPermissionMode,
    label: str,
    key_dir: Path,
) -> RuntimeCredentialPrincipalProof:
    """Sign the principal's consent with the key `cruxible init`/`principal add` wrote."""

    private_key = key_dir.expanduser() / f"{principal_id}.ed25519"
    if not private_key.is_file():
        raise click.UsageError(
            f"no private key for principal {principal_id} at {private_key}; repair: pass the "
            "--key-dir that `cruxible init` or `cruxible principal add` wrote for it"
        )
    return sign_runtime_credential_mint(
        instance_id=instance_id,
        principal_id=principal_id,
        permission_mode=permission_mode,
        label=label,
        private_key_path=private_key,
        forbidden_roots=(),
    )


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
        principal_id=record.principal_id,
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
    click.echo(f"Principal: {credential.principal_id or 'none (unbound: cannot author)'}")
    click.echo(f"Label: {credential.label}")
    click.echo(f"Permission mode: {credential.permission_mode}")
    click.echo(f"Created at: {credential.created_at}")
    if credential.created_by:
        click.echo(f"Created by: {credential.created_by}")
    if credential.revoked_at:
        click.echo(f"Revoked at: {credential.revoked_at}")


def _echo_preview(result: contracts.RuntimeCredentialResult, verb: str) -> bool:
    """Print a preview's outcome and the commit that makes it; False for a commit."""

    if not result.status.startswith("would_"):
        return False
    click.echo(f"Would {verb} credential {result.credential.credential_id}; nothing changed.")
    _echo_credential_metadata(result.credential)
    _common.echo_preview_next(result.status, result.coordinate)
    return True


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
@_common.change_control_options
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def claim_bootstrap_cmd(
    secret_file: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Exchange the bootstrap secret for this host's first ADMIN runtime token.

    The secret is claimable once per host: each host on the daemon claims its
    own first ADMIN credential with it, with no restart in between.
    """
    client, instance_id = _require_server_client("credential claim-bootstrap")
    result = client.claim_runtime_bootstrap(
        instance_id, _read_bootstrap_secret(secret_file), dry_run=dry_run, at=at
    )
    if output_json:
        _common._emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_claim":
        click.echo(
            f"Would claim the bootstrap credential of {result.instance_id}; nothing claimed."
        )
        _common.echo_preview_next(result.status, result.coordinate)
        return

    click.echo("Bootstrap claimed.")
    click.echo(f"Credential ID: {result.credential_id}")
    click.echo(f"Instance ID: {result.instance_id}")
    click.echo(f"Permission mode: {result.permission_mode}")
    assert result.token is not None  # a commit always issues one
    _echo_token_once(result.token, label="Admin token")


@credential_group.command("mint")
@click.option(
    "--principal-id",
    required=True,
    help="Principal the credential acts as. It must be registered and active.",
)
@click.option(
    "--key-dir",
    default=None,
    type=click.Path(file_okay=False),
    help=(
        "The principal's key directory. Signs the principal's consent to this "
        "credential; needed unless this request already acts as that principal."
    ),
)
@click.option("--label", default=None, help="Description only (default: the principal ID).")
@click.option(
    "--mode",
    "permission_mode",
    required=True,
    type=click.Choice(_PERMISSION_MODES),
    help=(
        "Credential permission tier (cumulative). read_only: reads only. "
        "governed_write: also proposes and authors, but cannot submit approvals or "
        "activate. graph_write: also submits approvals and activates. admin: also "
        "operator actions (credentials, host and init, principal changes, compiler "
        "upgrades, provider installs, ledger mirrors, daemon stop/restart)."
    ),
)
@_common.change_control_options
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def mint_cmd(
    principal_id: str,
    key_dir: str | None,
    label: str | None,
    permission_mode: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Mint a bearer credential that acts as one principal.

    Minting needs that principal's authority, not just an admin credential:
    either this request already acts as the principal, or `--key-dir` signs the
    principal's single-use consent with its registered key.
    """
    client, instance_id = _require_server_client("credential mint")
    mode = cast(contracts.RuntimeCredentialPermissionMode, permission_mode)
    proof = (
        None
        if key_dir is None
        else sign_principal_mint(
            instance_id=instance_id,
            principal_id=principal_id,
            permission_mode=mode,
            label=label or principal_id,
            key_dir=Path(key_dir),
        )
    )
    result = client.create_runtime_credential(
        instance_id,
        principal_id=principal_id,
        permission_mode=mode,
        label=label,
        principal_proof=proof,
        dry_run=dry_run,
        at=at,
    )
    settings = (
        None
        if key_dir is None or not result.token
        else set_principal_settings_token(Path(key_dir), result.token)
    )
    if output_json:
        payload = result.model_dump(mode="json")
        if settings is not None:
            payload["token"] = None
            payload["settings_path"] = str(settings)
        _common._emit_json(payload)
        return
    if _echo_preview(result, "mint"):
        return

    click.echo("Credential minted.")
    _echo_credential_metadata(result.credential)
    if settings is not None:
        click.echo(f"Token written to {settings} (not printed).")
    elif result.token:
        _echo_token_once(result.token, label="Token")


@credential_group.command("list")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def list_cmd(output_json: bool) -> None:
    """List runtime bearer credentials for the active instance."""
    client, instance_id = _require_server_client("credential list")
    result = client.list_runtime_credentials(instance_id)
    if output_json:
        _common._emit_json(result.model_dump(mode="json"))
        return

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
                    credential.principal_id or "-",
                    credential.label,
                    credential.created_at,
                    credential.created_by or "",
                ]
            )
        )


def _refuse_recover_admin_server_mode() -> None:
    """Refuse only a transport the caller chose for this invocation.

    A remembered CLI context or workspace binding is not a request to talk to a
    daemon here, so this local-only command ignores it.
    """
    obj = _common._root_ctx_obj()
    selected = obj.get("server_url") or obj.get("server_socket")
    source = obj.get("target_transport_source")
    if selected and source == "explicit":
        named = "--server-url/--server-socket"
    elif selected and source == "environment":
        named = "CRUXIBLE_SERVER_URL/CRUXIBLE_SERVER_SOCKET"
    elif obj.get("require_server"):
        named = "CRUXIBLE_REQUIRE_SERVER"
    else:
        return
    raise click.UsageError(
        f"credential recover-admin is local-only; unset {named} and run it directly "
        "against --state-root with the daemon stopped."
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


def _select_recovery_instance_id(
    db_path: Path, instance_id: str | None, *, state_root: Path
) -> str:
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
    # Credentials can outlive their instance's storage; when exactly one of
    # them still has its directory under this state root, that is the target.
    present = [
        candidate for candidate in instance_ids if (state_root / "instances" / candidate).is_dir()
    ]
    if len(present) == 1:
        return present[0]
    raise click.UsageError(
        "Credentials DB contains multiple instance IDs; pass --instance-id. "
        f"Found: {', '.join(instance_ids)}"
    )


@credential_group.command("recover-admin")
@click.option(
    "--state-root",
    default=None,
    help=(
        "Server state root containing daemon/runtime_credentials.db (default: "
        "CRUXIBLE_STATE_ROOT or ~/.cruxible). Stop the daemon first; the lock check "
        "only refuses a writer caught mid-transaction and does not detect an idle "
        "running daemon."
    ),
)
@click.option("--state-dir", default=None, hidden=True)
@click.option(
    "--instance-id",
    default=None,
    help=(
        "Target instance ID when the credentials DB contains multiple instances and "
        "not exactly one of them has a directory under <state-root>/instances."
    ),
)
@click.option(
    "--label",
    default="recovered-admin",
    show_default=True,
    help="Human-readable label for the recovered ADMIN credential.",
)
@_common.change_control_options
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def recover_admin_cmd(
    state_root: str | None,
    state_dir: str | None,
    instance_id: str | None,
    label: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Recover an ADMIN token by local filesystem ownership of server state.

    Trust model: this local-only command never contacts a Cruxible server. It
    treats ownership of --state-root and its daemon/runtime_credentials.db by the
    invoking uid as authority to mint one new ADMIN runtime credential directly
    in that DB. Stop the daemon first: the BEGIN IMMEDIATE check refuses a
    writer caught mid-transaction but cannot detect an idle running daemon,
    so operator discipline is the real guarantee.
    Existing credentials are not revoked automatically.
    """
    _refuse_recover_admin_server_mode()
    if state_dir is not None:
        raise click.UsageError("--state-dir is obsolete; use --state-root")
    if state_root is not None:
        if not state_root.strip():
            raise click.UsageError("--state-root may not be empty")
        resolved_state_root = Path(state_root).expanduser().resolve()
    else:
        resolved_state_root = get_server_state_root()
    db_path = resolved_state_root / "daemon" / "runtime_credentials.db"
    uid = os.getuid()
    _require_owned_path(resolved_state_root, description="State root", uid=uid, directory=True)
    _require_owned_path(db_path, description="Runtime credentials DB", uid=uid, directory=False)
    resolved_instance_id = _select_recovery_instance_id(
        db_path, instance_id, state_root=resolved_state_root
    )
    _common._echo_explicit_write_target(resolved_instance_id, resolved_state_root)

    # A preview runs the recovery's own transaction -- the write lock, the
    # schema and target checks, every insert -- and rolls it back (R12).
    from cruxible_core.service.change_preview import state_change_scope

    store = RuntimeCredentialStore(db_path, initialize=False)
    try:
        with state_change_scope(
            dry_run=dry_run,
            at=at,
            kind="direct",
            operation="credential.recover-admin",
            describe=f"recovering an ADMIN credential for {resolved_instance_id}",
        ) as change:
            recovered = store.recover_admin_credential(
                instance_id=resolved_instance_id,
                label=label,
                uid=uid,
                hostname=socket.gethostname(),
                dry_run=change.previewing,
                observe=change.observe,
            )
    except RuntimeCredentialRecoveryBusyError as exc:
        raise click.UsageError(str(exc)) from exc
    except RuntimeCredentialRecoveryError as exc:
        raise click.UsageError(str(exc)) from exc

    result = contracts.RuntimeCredentialResult(
        status="would_recover" if change.previewing else "recovered",
        credential=_credential_metadata_from_record(recovered.record),
        token=None if change.previewing else recovered.token,
        coordinate=change.coordinate,
    )
    if output_json:
        _common._emit_json(
            {
                **result.model_dump(mode="json"),
                "existing_credentials_revoked": False,
                "next_step": (
                    "Commit the preview with --commit --at <its coordinate digest>."
                    if change.previewing
                    else "Restart the daemon with auth enabled. Revoke old admin credentials "
                    "after recovery if desired."
                ),
            }
        )
        return

    if change.previewing:
        click.echo(
            f"Would mint one ADMIN credential for {resolved_instance_id} in {db_path}; "
            "nothing was written."
        )
        _common.echo_preview_next(result.status, result.coordinate)
        return
    assert result.token is not None
    click.echo("Admin credential recovered.")
    _echo_credential_metadata(result.credential)
    _echo_token_once(result.token, label="Admin token")
    click.echo(
        "Existing admin credentials were not revoked. Restart the daemon with auth "
        "enabled, then revoke old admin credentials if desired."
    )


@credential_group.command("revoke")
@click.argument("credential_id")
@_common.change_control_options
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def revoke_cmd(credential_id: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Revoke a runtime bearer credential.

    It cannot be undone, so it previews first; commit that preview with
    ``--commit --at OID``.
    """
    client, instance_id = _require_server_client("credential revoke")
    result = client.revoke_runtime_credential(instance_id, credential_id, dry_run=dry_run, at=at)
    if output_json:
        _common._emit_json(result.model_dump(mode="json"))
        return
    if _echo_preview(result, "revoke"):
        return

    click.echo("Credential revoked.")
    _echo_credential_metadata(result.credential)


@credential_group.command("rotate")
@click.argument("credential_id")
@click.option(
    "--key-dir",
    default=None,
    type=click.Path(file_okay=False),
    help=(
        "The bound principal's key directory. Signs its consent to the replacement; "
        "needed unless this request already acts as that principal."
    ),
)
@_common.change_control_options
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def rotate_cmd(
    credential_id: str, key_dir: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Rotate a runtime bearer credential and print the replacement token once.

    A credential bound to a principal is replaced only with that principal's
    authority, exactly as minting one: this request acts as the principal, or
    `--key-dir` signs its consent. An admin may revoke it but never receives it.
    The old token is revoked, which cannot be undone, so it previews first;
    commit that preview with ``--commit --at OID``.
    """
    client, instance_id = _require_server_client("credential rotate")
    proof = None
    if key_dir is not None:
        listed = client.list_runtime_credentials(instance_id)
        target = next(
            (item for item in listed.credentials if item.credential_id == credential_id), None
        )
        if target is None or target.principal_id is None:
            raise click.UsageError(
                f"credential {credential_id} is not an active principal-bound credential of "
                "this instance; --key-dir signs only for a bound one"
            )
        proof = sign_principal_mint(
            instance_id=instance_id,
            principal_id=target.principal_id,
            permission_mode=target.permission_mode,
            label=target.label,
            key_dir=Path(key_dir),
        )
    result = client.rotate_runtime_credential(
        instance_id, credential_id, principal_proof=proof, dry_run=dry_run, at=at
    )
    settings = (
        None
        if key_dir is None or not result.token
        else set_principal_settings_token(Path(key_dir), result.token)
    )
    if output_json:
        payload = result.model_dump(mode="json")
        if settings is not None:
            payload["token"] = None
            payload["settings_path"] = str(settings)
        _common._emit_json(payload)
        return
    if _echo_preview(result, "rotate"):
        return

    click.echo("Credential rotated.")
    _echo_credential_metadata(result.credential)
    if settings is not None:
        click.echo(f"Token written to {settings} (not printed).")
    elif result.token:
        _echo_token_once(result.token, label="Token")
