"""Shared dispatch and formatting helpers for the Playbill-only CLI."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

import click

from cruxible_client import CruxibleClient
from cruxible_client.compatibility import check_daemon_compatibility
from cruxible_core.cli.context import (
    CliContextState,
    clear_cli_context,
    load_cli_context,
    save_cli_context,
)
from cruxible_core.server.config import get_runtime_bearer_token

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


def _echo_active_write_target() -> None:
    obj = _root_ctx_obj()
    transport = _transport_target(obj)
    instance_id = obj.get("instance_id")
    if transport is not None and instance_id:
        qualifier = _target_source_qualifier(
            str(obj.get("target_instance_source") or "explicit"),
            str(obj.get("target_transport_source") or "explicit"),
        )
        click.echo(f"target: {instance_id} @ {transport} ({qualifier})", err=True)


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
    )
    check_daemon_compatibility(client)
    obj["_client"] = client
    return client


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
