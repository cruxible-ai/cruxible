"""Shared dispatch and formatting helpers for the CLI."""

from __future__ import annotations

import functools
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
    normalized_transport,
    principal_binding,
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


#: ``ctx.meta`` keys: whether this invocation offers to commit its preview, and
#: the coordinate a preview answered with when it does.
_COMMIT_OFFER_KEY = "cruxible.change_control.offer_commit"
_COMMIT_PIN_KEY = "cruxible.change_control.preview_pin"


def _interactive_terminal() -> bool:
    """Whether a person is at this invocation: standard input and output are both a TTY."""

    return sys.stdin.isatty() and sys.stdout.isatty()


def _can_confirm() -> bool:
    """Whether a prompt on standard error can be answered: stdin and stderr are a TTY.

    A command whose prompt goes to stderr keeps stdout for its output (``--json``
    piped elsewhere), so only the prompt's own streams need a person.
    """

    return sys.stdin.isatty() and sys.stderr.isatty()


def change_control_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--dry-run/--commit``, ``--at`` and ``--yes``: the R12 change control on every change.

    Neither flag: the operation's default (a change derived across several
    artifacts, or one that cannot be undone, previews). ``--commit --at OID``
    commits exactly the preview that answered at OID.

    At an interactive terminal (no ``--json``), a change that previews by
    default renders the preview and asks ``Commit this change? [y/N]``; yes
    commits against the exact coordinate the preview answered with, and so
    refuses the same way ``--commit --at OID`` would if that state moved.
    ``--yes`` commits the preview without asking. Anywhere else the preview
    prints the ``--commit --at`` hint, unchanged.
    """

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        yes = bool(kwargs.pop("yes", False))
        output_json = bool(kwargs.get("output_json", False))
        if yes and output_json:
            raise click.UsageError(
                "--yes renders the preview and then commits it; with --json, commit the "
                "preview with --commit --at OID"
            )
        if yes and kwargs.get("dry_run") is not None:
            raise click.UsageError(
                "--yes commits a change that previews by default; drop --dry-run/--commit"
            )
        ctx = click.get_current_context()
        offer = kwargs.get("dry_run") is None and not output_json
        offer = offer and (yes or _interactive_terminal())
        ctx.meta[_COMMIT_OFFER_KEY] = offer
        ctx.meta.pop(_COMMIT_PIN_KEY, None)
        result = fn(*args, **kwargs)
        ctx.meta[_COMMIT_OFFER_KEY] = False
        pin = ctx.meta.pop(_COMMIT_PIN_KEY, None)
        if not offer or pin is None:
            return result
        if not yes and not click.confirm("Commit this change?", default=False):
            click.echo(f"Not committed. Commit it later: rerun with --commit --at {pin}")
            return result
        return fn(*args, **{**kwargs, "dry_run": False, "at": pin})

    command = click.option(
        "--yes",
        "yes",
        is_flag=True,
        default=False,
        help=(
            "Commit the preview without asking (a change that previews by default); "
            "at an interactive terminal it otherwise asks."
        ),
    )(run)
    command = click.option(
        "--at",
        "at",
        default=None,
        metavar="OID",
        help=(
            "The coordinate a preview answered with (a git oid, or for operational state "
            "its digest); the commit refuses if that state moved since. Required to commit "
            "a change that cannot be undone."
        ),
    )(command)
    return click.option(
        "--dry-run/--commit",
        "dry_run",
        default=None,
        help=(
            "--dry-run: run every check and write nothing. --commit: make the change. "
            "Default: preview a change derived across several artifacts or one that "
            "cannot be undone; commit anything else."
        ),
    )(command)


def echo_preview_next(status: str, coordinate: Any) -> None:
    """After a preview, say nothing was written and how to commit exactly it.

    When the invocation offers to commit its preview (an interactive terminal,
    or ``--yes``), the pin is handed to ``change_control_options``, which asks
    once the command has rendered everything.
    """

    if not status.startswith("would_") or coordinate is None:
        return
    # An accepted coordinate pins by its git oid; operational state by its digest.
    pin = getattr(coordinate, "git_oid", None) or getattr(coordinate, "digest", None)
    if pin is None:
        return
    click.echo(f"Preview at {pin}; nothing was written.")
    if status == "would_block" or status == "would_refuse":
        return
    ctx = click.get_current_context(silent=True)
    if ctx is not None and ctx.meta.get(_COMMIT_OFFER_KEY):
        ctx.meta[_COMMIT_PIN_KEY] = pin
        return
    click.echo(f"Commit it: rerun the same command with --commit --at {pin}")


brief_option = click.option(
    "--brief",
    "output_brief",
    is_flag=True,
    default=False,
    help="Render only the outcome, the ids, and the command to run next.",
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


def _echo_refusal(
    where: str,
    *,
    code: str,
    message: str,
    change: int | None = None,
    candidates: tuple[str, ...] = (),
    repair: str | None = None,
) -> None:
    """Render one refusal as value writes do: where, code and message, nearest, repair."""

    click.echo(f"refused ({where})", err=True)
    at = "" if change is None else f" (change {change})"
    click.echo(f"{code}{at}: {message}", err=True)
    if candidates:
        click.echo(f"  nearest: {', '.join(candidates)}", err=True)
    if repair:
        click.echo(f"  repair: {repair}", err=True)


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
    return normalized_transport(obj.get("server_url"), obj.get("server_socket"))


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
        target = params.get("instance_id") or "<new Cruxible host>"
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


def _get_lifecycle_client(state_root: Path | None = None) -> DaemonLifecycleClient | None:
    """An unchecked client whose interface only permits lifecycle endpoints.

    ``state_root`` is where the local daemon keeps its bootstrap secret, when it
    is not the configured one (``server start --state-root``).
    """
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
        else _local_operator_key(
            server_url=server_url, server_socket=server_socket, state_root=state_root
        )
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
    *, server_url: str | None, server_socket: str | None, state_root: Path | None = None
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

    if state_root is None:
        try:
            state_root = get_server_state_root()
        except CoreError:
            return None
    return read_local_operator_key(
        state_root.expanduser().resolve(), server_url=server_url, server_socket=server_socket
    )


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
        replace(
            load_cli_context(),
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
    """Remember transport and instance; the principals the context knows are kept."""

    save_cli_context(
        replace(
            load_cli_context(),
            server_url=server_url,
            server_socket=server_socket,
            instance_id=instance_id,
            instance_transport=instance_transport,
        )
    )


def _remember_principal_settings(
    instance_id: str, principal_id: str, settings_path: Path, *, activate: bool
) -> None:
    """Remember a principal's settings file so the CLI can load it (and act as it)."""

    transport = _transport_target(_root_ctx_obj())
    if transport is None:  # pragma: no cover - settings are written only in server mode
        return
    save_cli_context(
        load_cli_context().remember_principal(
            principal_binding(transport, instance_id),
            principal_id,
            settings_path,
            activate=activate,
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
