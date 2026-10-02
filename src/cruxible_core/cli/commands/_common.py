"""Shared dispatch and formatting helpers for the Playbill-only CLI."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import click

import cruxible_client.compatibility as client_compatibility
from cruxible_client import CruxibleClient
from cruxible_client.transport.lifecycle import DaemonLifecycleClient
from cruxible_core.cli.context import (
    CliContextState,
    clear_cli_context,
    load_cli_context,
    save_cli_context,
)
from cruxible_core.server.config import get_runtime_bearer_token

if TYPE_CHECKING:
    from cruxible_core.server.bootstrap_secret import LocalOperatorKey

LocalResultT = TypeVar("LocalResultT")
RemoteResultT = TypeVar("RemoteResultT")

SERVER_MODE_REQUIRED_MESSAGE = (
    "Server mode is required. Set CRUXIBLE_SERVER_SOCKET or CRUXIBLE_SERVER_URL."
)

json_option = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)


def change_control_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--dry-run/--commit`` and ``--at``: the R12 change control on every change.

    Neither flag: the operation's default (a change derived across several
    artifacts, or one that cannot be undone, previews). ``--commit --at OID``
    commits exactly the preview that answered at OID.
    """

    fn = click.option(
        "--at",
        "at",
        default=None,
        metavar="OID",
        help=(
            "The coordinate a preview answered with (a git oid, or for operational state "
            "its digest); the commit refuses if that state moved since. Required to commit "
            "a change that cannot be undone."
        ),
    )(fn)
    return click.option(
        "--dry-run/--commit",
        "dry_run",
        default=None,
        help=(
            "--dry-run: run every check and write nothing. --commit: make the change. "
            "Default: preview a change derived across several artifacts or one that "
            "cannot be undone; commit anything else."
        ),
    )(fn)


def echo_preview_next(status: str, coordinate: Any) -> None:
    """After a preview, say nothing was written and how to commit exactly it."""

    if not status.startswith("would_") or coordinate is None:
        return
    # An accepted coordinate pins by its git oid; operational state by its digest.
    pin = getattr(coordinate, "git_oid", None) or getattr(coordinate, "digest", None)
    if pin is None:
        return
    click.echo(f"Preview at {pin}; nothing was written.")
    if status != "would_block" and status != "would_refuse":
        click.echo(f"Commit it: rerun the same command with --commit --at {pin}")


brief_option = click.option(
    "--brief",
    "output_brief",
    is_flag=True,
    default=False,
    help="Render only the outcome, the ids, and the command to run next.",
)

and_activate_option = click.option(
    "--and-activate",
    "and_activate",
    is_flag=True,
    default=False,
    help="Activate immediately when the candidate needs no approval; never half-activate.",
)


def _emit_brief(
    *,
    outcome: str,
    ids: Mapping[str, str | None],
    next_command: str | None,
    reason: str | None = None,
) -> None:
    """Render the three things a caller acts on: what happened, what to name, what to run.

    Full JSON stays the default because it is the record; this is the read.
    """

    click.echo(f"outcome: {outcome}")
    for label, value in ids.items():
        if value:
            click.echo(f"{label}: {value}")
    if reason:
        click.echo(f"reason: {reason}")
    click.echo(f"next: {next_command}" if next_command else "next: nothing to run")


def _root_ctx_obj() -> dict[str, Any]:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}
    root = ctx.find_root()
    root.ensure_object(dict)
    return cast(dict[str, Any], root.obj)


def _json_compact_enabled() -> bool:
    context_value = _root_ctx_obj().get("json_compact")
    if context_value is not None:
        return bool(context_value)
    return os.environ.get("CRUXIBLE_JSON_COMPACT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emit_json(data: Any, *, sort_keys: bool = False) -> None:
    if _json_compact_enabled():
        from cruxible_client.contracts.primitives import compact_json

        click.echo(compact_json(data, default=str, sort_keys=sort_keys))
        return
    click.echo(json.dumps(data, indent=2, sort_keys=sort_keys, default=str))


def _transport_target(obj: Mapping[str, Any]) -> str | None:
    if obj.get("server_url"):
        return str(obj["server_url"]).rstrip("/")
    if obj.get("server_socket"):
        return f"unix://{Path(str(obj['server_socket'])).expanduser().resolve()}"
    return None


def _target_source_qualifier(instance_source: str, transport_source: str) -> str:
    if instance_source == transport_source:
        return instance_source
    return f"instance={instance_source}, transport={transport_source}"


def _echo_active_write_target(
    *,
    instance_id: str | None = None,
    instance_source: str | None = None,
) -> None:
    obj = _root_ctx_obj()
    transport = _transport_target(obj)
    selected = instance_id or obj.get("instance_id")
    if transport is not None and selected:
        qualifier = _target_source_qualifier(
            instance_source or str(obj.get("target_instance_source") or "explicit"),
            str(obj.get("target_transport_source") or "explicit"),
        )
        click.echo(f"target: {selected} @ {transport} ({qualifier})", err=True)


def _echo_creation_write_target(params: Mapping[str, Any]) -> None:
    obj = _root_ctx_obj()
    transport = _transport_target(obj)
    if transport is not None:
        target = params.get("instance_id") or "<new Playbill host>"
        transport_source = str(obj.get("target_transport_source") or "explicit")
        click.echo(
            f"target: {target} @ {transport} (transport={transport_source})",
            err=True,
        )


def _echo_write_target(mode: str, params: Mapping[str, Any]) -> None:
    if mode == "active":
        _echo_active_write_target()
        return
    if mode == "create":
        _echo_creation_write_target(params)
        return
    raise AssertionError(f"Unknown write target mode: {mode}")


def _echo_explicit_write_target(instance_id: str, location: str | Path) -> None:
    click.echo(
        f"target: {instance_id} @ {Path(location).expanduser().resolve()} (explicit)",
        err=True,
    )


def _get_client() -> CruxibleClient | None:
    obj = _root_ctx_obj()
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if not server_url and not server_socket:
        return None
    client = obj.get("_client")
    if isinstance(client, CruxibleClient):
        return client
    client = CruxibleClient(
        base_url=server_url,
        socket_path=server_socket,
        token=get_runtime_bearer_token(),
        principal_id=obj.get("principal_id"),
    )
    try:
        client_compatibility.check_daemon_compatibility(client)
    except Exception:
        client.close()
        raise
    obj["_client"] = client
    return client


def _get_lifecycle_client() -> DaemonLifecycleClient | None:
    """An unchecked client whose interface only permits lifecycle endpoints."""
    obj = _root_ctx_obj()
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if not server_url and not server_socket:
        return None
    client = obj.get("_lifecycle_client")
    if isinstance(client, DaemonLifecycleClient):
        return client
    token = get_runtime_bearer_token()
    key = (
        None
        if token is not None
        else _local_operator_key(server_url=server_url, server_socket=server_socket)
    )
    client = DaemonLifecycleClient(
        base_url=server_url,
        socket_path=server_socket,
        token=token,
        operator_secret=None if key is None else key.secret,
        operator_boot_id=None if key is None else key.boot_id,
    )
    obj["_lifecycle_client"] = client
    return client


def _local_operator_key(
    *, server_url: str | None, server_socket: str | None
) -> LocalOperatorKey | None:
    """The bootstrap secret and boot id of the local daemon this command targets.

    Lifecycle commands (`server status`, `restart`, `stop`) fall back to it when
    no bearer token is configured, so a local restart needs no credential typed
    in. It keys a per-request MAC, bound to the boot id the live daemon wrote in
    its lock record, and never goes on the wire. It is read only while a live
    daemon holds the state-root lock on this exact transport.
    """

    from cruxible_core.errors import CoreError
    from cruxible_core.server.bootstrap_secret import read_local_operator_key
    from cruxible_core.server.config import get_server_state_root

    try:
        state_root = get_server_state_root()
    except CoreError:
        return None
    return read_local_operator_key(state_root, server_url=server_url, server_socket=server_socket)


def _current_cli_context() -> CliContextState:
    obj = _root_ctx_obj()
    return CliContextState(
        server_url=obj.get("server_url"),
        server_socket=obj.get("server_socket"),
        instance_id=obj.get("instance_id"),
        instance_transport=obj.get("instance_transport"),
    )


@dataclass(frozen=True)
class ActiveInstanceChange:
    previous: str | None
    current: str


def _activate_server_instance(instance_id: str) -> ActiveInstanceChange | None:
    state = _current_cli_context()
    if not state.server_url and not state.server_socket:
        return None
    save_cli_context(
        CliContextState(
            server_url=state.server_url,
            server_socket=state.server_socket,
            instance_id=instance_id,
            instance_transport=(
                state.server_url.rstrip("/")
                if state.server_url
                else (
                    f"unix://{Path(state.server_socket).expanduser().resolve()}"
                    if state.server_socket
                    else None
                )
            ),
        )
    )
    _root_ctx_obj()["instance_id"] = instance_id
    return ActiveInstanceChange(previous=state.instance_id, current=instance_id)


def _persist_cli_context(
    *,
    server_url: str | None,
    server_socket: str | None,
    instance_id: str | None,
    instance_transport: str | None = None,
) -> None:
    save_cli_context(
        CliContextState(
            server_url=server_url,
            server_socket=server_socket,
            instance_id=instance_id,
            instance_transport=instance_transport,
        )
    )


def _clear_persisted_cli_context() -> None:
    clear_cli_context()


def _load_persisted_cli_context() -> CliContextState:
    return load_cli_context()


def _dispatch_cli(
    remote_call: Callable[[CruxibleClient], RemoteResultT],
    local_call: Callable[[], LocalResultT],
    *,
    allow_local: bool = True,
    command_name: str | None = None,
) -> RemoteResultT | LocalResultT:
    client = _get_client()
    if client is not None:
        return remote_call(client)
    if not allow_local:
        raise click.UsageError(
            f"Local execution disabled for {command_name or 'this command'}; use server mode."
        )
    if _root_ctx_obj().get("require_server"):
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)
    return local_call()


def _require_instance_id() -> str:
    instance_id = _root_ctx_obj().get("instance_id")
    if not instance_id:
        obj = _root_ctx_obj()
        if mismatch := obj.get("context_instance_transport_mismatch"):
            raise click.UsageError(str(mismatch))
        source = _target_source_qualifier(
            str(obj.get("target_instance_source") or "local"),
            str(obj.get("target_transport_source") or "local"),
        )
        raise click.UsageError(
            f"--instance-id is required in server mode (target source: {source})"
        )
    return str(instance_id)
