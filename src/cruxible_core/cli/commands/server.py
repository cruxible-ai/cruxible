"""CLI commands for launching and inspecting the Cruxible daemon.

This group holds both the daemon-launch verb and the client RPCs:

* ``start`` LAUNCHES the daemon in the foreground. It takes no ``--server-url``;
  it is the process that becomes the daemon. ``--host`` / ``--port`` /
  ``--state-root`` mirror ``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` /
  ``CRUXIBLE_STATE_ROOT`` (env vars are honored as defaults).
* ``status`` / ``restart`` / ``stop`` are CLIENT RPCs that talk to an
  already-running daemon. They require a transport (``--server-url`` /
  ``--server-socket``, or the ``CRUXIBLE_SERVER_URL`` / ``CRUXIBLE_SERVER_SOCKET``
  env vars, or a remembered CLI context) and fail with a clear message when no
  daemon is reachable.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import click

from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.errors import CoreError, DaemonOperationScopeError
from cruxible_client.transport.lifecycle import DaemonLifecycleClient
from cruxible_core.cli.commands._common import (
    SERVER_MODE_REQUIRED_MESSAGE,
    _emit_json,
    _root_ctx_obj,
)
from cruxible_core.cli.commands._common import (
    _get_client as _get_checked_client,
)
from cruxible_core.cli.commands._common import (
    _get_lifecycle_client as _get_client,
)
from cruxible_core.cli.main import handle_errors, long_running_command
from cruxible_core.runtime.permissions import PERMISSION_MODE_NAMES
from cruxible_core.server.config import (
    get_server_state_root,
    is_server_auth_enabled,
)
from cruxible_core.server.service_install import (
    build_service_config,
    current_service_platform,
    durable_credentials_available,
    install_service,
    load_service_config,
    render_service,
    resolve_service_auth_posture,
    resolved_cruxible_executable,
    service_config_path,
)
from cruxible_core.server.shutdown import ServerStopNotConfirmed
from cruxible_core.server.state_lock import state_lock_holder_is_alive, state_lock_path

# Poll cadence while waiting for the re-exec'd daemon to start answering again.
_RESTART_POLL_INTERVAL_SECONDS = 0.25

# Client RPCs (status/restart/stop) need a reachable daemon; surface a single,
# actionable line instead of a hang or an opaque transport traceback when the
# daemon is down or no transport is configured.
_DAEMON_REQUIRED_HINT = (
    "Start one with `cruxible server start`, or point `--server-url` / "
    "`CRUXIBLE_SERVER_URL` at a running daemon."
)


def _client_transport_label() -> str:
    """Describe the transport the active client RPC is talking to."""
    obj = _root_ctx_obj()
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if server_url:
        return str(server_url)
    if server_socket:
        return f"unix socket {server_socket}"
    return "configured Cruxible server"


def _wait_for_daemon(
    client: DaemonLifecycleClient, timeout: float, *, old_boot_id: str | None
) -> str:
    """Poll the daemon's /version probe until the NEW image answers.

    The old image keeps answering for a beat after it acknowledges the
    restart, and the re-exec keeps its pid, so an answer only counts once its
    boot id differs from the one that acknowledged. Returns the version the new
    image reports; raising here keeps the command skew-proof.
    """
    if old_boot_id is None or not old_boot_id.strip():
        raise click.ClickException(
            "Daemon replacement could not be confirmed: restart acknowledgement has no boot ID."
        )
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    still_old = False
    missing_boot_id = False
    while time.monotonic() < deadline:
        try:
            version, boot_id = client.daemon_identity()
        except Exception as exc:  # connection refused while the image is replaced
            last_error = exc
            still_old = False
        else:
            identifiable = boot_id is not None and bool(boot_id.strip())
            if identifiable and boot_id != old_boot_id:
                return version
            missing_boot_id = not identifiable
            still_old = identifiable
            last_error = None
        time.sleep(_RESTART_POLL_INTERVAL_SECONDS)
    raise click.ClickException(
        f"Daemon did not come back within {timeout:.0f}s after restart; "
        "replacement could not be confirmed"
        + ("; the old process image is still answering" if still_old else "")
        + ("; a version probe has no boot ID" if missing_boot_id else "")
        + (f": {last_error}" if last_error is not None else "")
    )


def _daemon_still_answers(client: DaemonLifecycleClient) -> bool:
    """Return whether the daemon is still answering over the configured transport."""
    try:
        client.version()
    except Exception:
        # A closed socket or a refused connection is the daemon's own answer
        # that it has left; nothing else distinguishes those cases usefully.
        return False
    return True


def _observe_stop(
    client: DaemonLifecycleClient,
    state_root: Path,
    timeout: float,
) -> tuple[bool, bool | None]:
    """Return (daemon exited, state root released) as OBSERVED, not as assumed.

    The stop is acknowledged before the process leaves, so the acknowledgement
    alone proves nothing. The exit is observed from the DAEMON, over the
    transport the caller configured. The lock is only consulted when the state
    root is a path on this filesystem: against a bound-TCP daemon on another
    host it is not, and polling it there answered "released" instantly for a
    daemon that had not even begun shutting down.

    The released value is None when release is not observable from this client.
    """
    deadline = time.monotonic() + timeout
    observable = state_lock_path(state_root).is_file()
    exited = False
    while True:
        if not exited:
            exited = not _daemon_still_answers(client)
        if exited:
            if not observable:
                return True, None
            if not state_lock_holder_is_alive(state_root):
                return True, True
        if time.monotonic() >= deadline:
            return exited, False
        time.sleep(_RESTART_POLL_INTERVAL_SECONDS)


@click.group("server")
def server_group() -> None:
    """Launch and inspect the Cruxible daemon."""


@server_group.command("start")
@click.option(
    "--host",
    default=None,
    help="Bind host (default: CRUXIBLE_HOST or 127.0.0.1). Ignored when --socket is set.",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Bind port (default: CRUXIBLE_PORT or 8100). Ignored when --socket is set.",
)
@click.option(
    "--state-root",
    default=None,
    help="Server-owned state root (default: CRUXIBLE_STATE_ROOT or ~/.cruxible).",
)
@click.option(
    "--socket",
    "socket_path",
    default=None,
    help="Listen on this Unix socket path instead of host/port (default: CRUXIBLE_SERVER_SOCKET).",
)
@click.option(
    "--capability-ceiling",
    type=click.Choice(PERMISSION_MODE_NAMES, case_sensitive=False),
    default=None,
    help=(
        "Immutable daemon capability ceiling (default: CRUXIBLE_MODE or admin). "
        "Bearer credentials cannot exceed it."
    ),
)
@click.option(
    "--auth",
    "auth",
    is_flag=True,
    default=False,
    help=(
        "Require bearer credentials (also CRUXIBLE_SERVER_AUTH=true). A Unix-socket "
        "daemon defaults to auth off; a TCP daemon refuses to start without it."
    ),
)
@click.option(
    "--bootstrap-secret-file",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Also write the runtime bootstrap secret to this file (mode 0600). It is always "
        "written to <state-root>/daemon/bootstrap-secret and never printed."
    ),
)
@handle_errors
@long_running_command
def server_start_cmd(
    host: str | None,
    port: int | None,
    state_root: str | None,
    socket_path: str | None,
    capability_ceiling: str | None,
    auth: bool,
    bootstrap_secret_file: str | None,
) -> None:
    """Launch the Cruxible daemon in the foreground.

    This becomes the long-running daemon process; it is NOT a client of an
    existing one, so it takes no `--server-url`. Flags override the matching
    environment variables (`CRUXIBLE_HOST`, `CRUXIBLE_PORT`,
    `CRUXIBLE_STATE_ROOT`, `CRUXIBLE_SERVER_SOCKET`, `CRUXIBLE_MODE`);
    unset flags fall back to the env value or the built-in default. The
    capability ceiling is fixed for the daemon process lifetime. Use a durable
    `--state-root` (e.g. `~/.cruxible`), not a volatile temp path. Stop
    with Ctrl-C.

    Auth: a Unix-socket daemon defaults to auth off and says so on start, since
    every process that can reach its 0700 socket directory already runs as this
    OS user. A TCP daemon refuses to start without auth. `--auth` (or
    `CRUXIBLE_SERVER_AUTH=true`) opts in. With auth on, the bootstrap secret is
    written owner-only to `<state-root>/daemon/bootstrap-secret` and never
    printed; `server status`, `restart` and `stop` read it from there.
    """
    if auth:
        os.environ["CRUXIBLE_SERVER_AUTH"] = "true"
    if bootstrap_secret_file is not None and not is_server_auth_enabled():
        raise click.UsageError(
            "--bootstrap-secret-file needs auth, and this daemon would start with auth "
            "off; repair: add --auth (or set CRUXIBLE_SERVER_AUTH=true)"
        )
    # Imported lazily so `cruxible server start --help` (and the rest of the CLI)
    # never pays the uvicorn/server import cost, and so the optional `server`
    # extra is only required when actually launching.
    from cruxible_core.server.app import run_server

    run_server(
        host=host,
        port=port,
        state_root=state_root,
        socket_path=socket_path,
        capability_ceiling=capability_ceiling,
        auth=auth,
        bootstrap_secret_file=(
            None
            if bootstrap_secret_file is None
            else str(Path(bootstrap_secret_file).expanduser().resolve())
        ),
    )


@server_group.command("stop")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Seconds to wait for the daemon to release its state root.",
)
@handle_errors
def server_stop_cmd(output_json: bool, timeout: float) -> None:
    """Stop the running daemon gracefully and release its state-root lock.

    A CLIENT command: it asks the daemon over the configured transport
    (`--server-url` / `--server-socket` or the matching env vars) to shut itself
    down. Reaching for `kill` or a terminal-multiplexer quit instead kills the
    launching shell and orphans the daemon, leaving a second process serving the
    same state root.

    The release is OBSERVED, never assumed: the daemon must stop answering over
    that transport, and when its state root is a directory on this filesystem
    its lock must be free. Against a daemon on another host the lock is not
    observable from here and the command says so rather than claiming a release
    it cannot see. Exits non-zero when the root was NOT released, so
    `server stop && server start` cannot walk into the lock refusal the stop was
    meant to clear.
    """
    client = _get_client()
    if client is None:
        raise click.UsageError(f"{SERVER_MODE_REQUIRED_MESSAGE} {_DAEMON_REQUIRED_HINT}")
    result = client.server_stop()
    exited, released = _observe_stop(client, Path(result.state_root), timeout)

    if output_json:
        payload = result.model_dump(mode="python")
        payload["daemon_exited"] = exited
        payload["state_root_released"] = released
        _emit_json(payload)
    else:
        click.echo(f"Stop scheduled (pid {result.pid}, version {result.version}).")
        click.echo(f"State root: {result.state_root}")
        if released is True:
            click.echo("Daemon exited and released its state root.")
        elif released is None:
            click.echo(
                "Stop requested; lock release not observable from this client.",
                err=True,
            )

    if released is not False:
        return
    detail = (
        f"the daemon was still answering after {timeout:.0f}s"
        if not exited
        else f"the daemon exited but its state root was still locked after {timeout:.0f}s"
    )
    raise ServerStopNotConfirmed(
        f"{ServerStopNotConfirmed.error_code}: {detail}; the next `cruxible server start` "
        "on this state root would refuse. Repair: re-run `cruxible server stop`, then "
        "`cruxible server status`, and check the daemon's logs"
    )


@server_group.command("install-service")
@click.option("--state-root", default=None, help="Durable daemon state root.")
@click.option("--socket", "socket_path", default=None, help="Unix socket for server start.")
@click.option("--host", default=None, help="TCP bind host when no socket is selected.")
@click.option("--port", type=int, default=None, help="TCP bind port when no socket is selected.")
@click.option(
    "--capability-ceiling",
    type=click.Choice(PERMISSION_MODE_NAMES, case_sensitive=False),
    default=None,
    help="Recorded server capability ceiling (default: admin).",
)
@click.option("--auth/--no-auth", default=None, help="Enable or disable daemon authentication.")
@click.option("--replace", is_flag=True, help="Replace an existing service unit.")
@click.option("--print", "print_only", is_flag=True, help="Print without writing or enabling.")
@handle_errors
def server_install_service_cmd(
    state_root: str | None,
    socket_path: str | None,
    host: str | None,
    port: int | None,
    capability_ceiling: str | None,
    auth: bool | None,
    replace: bool,
    print_only: bool,
) -> None:
    """Render or install a user service that runs `cruxible server start`."""

    if socket_path is not None and (host is not None or port is not None):
        raise click.UsageError("choose --socket or --host/--port, not both")
    root = (
        Path(state_root).expanduser().resolve()
        if state_root is not None
        else get_server_state_root()
    )
    explicit_settings = any(
        value is not None for value in (socket_path, host, port, capability_ceiling, auth)
    )
    recorded_path = service_config_path(root)
    if print_only and recorded_path.is_file() and not explicit_settings:
        config = load_service_config(root)
        if config.platform != current_service_platform():
            raise click.UsageError(
                "recorded service platform differs from this host; rerun --print with explicit "
                "server settings"
            )
        resolve_service_auth_posture(root, config.auth_enabled)
        if config.auth_enabled and not durable_credentials_available(root):
            raise click.UsageError(
                "recorded auth-on service no longer has an active durable runtime credential; "
                "repair: run `cruxible server start --auth --bootstrap-secret-file PATH`, "
                "claim the bootstrap credential, then rerun install-service"
            )
        click.echo(render_service(config).decode("utf-8"), nl=False)
        return

    auth_enabled = resolve_service_auth_posture(root, auth)
    if auth_enabled and not durable_credentials_available(root):
        raise click.UsageError(
            "auth-on unattended startup requires an active durable runtime credential; "
            "repair: run `cruxible server start --auth --bootstrap-secret-file PATH`, claim the "
            "bootstrap credential, then rerun install-service"
        )
    config = build_service_config(
        platform=current_service_platform(),
        executable=str(resolved_cruxible_executable()),
        state_root=str(root),
        socket_path=(
            str(Path(socket_path).expanduser().resolve()) if socket_path is not None else None
        ),
        host=None if socket_path is not None else (host or "127.0.0.1"),
        port=None if socket_path is not None else (port or 8100),
        capability_ceiling=cast(
            Literal["read_only", "governed_write", "graph_write", "admin"],
            (capability_ceiling or "admin").lower(),
        ),
        auth_enabled=auth_enabled,
    )
    if config.socket_path is None and not config.auth_enabled:
        raise click.UsageError(
            "service_install.tcp_requires_auth: a TCP daemon refuses to start without auth, "
            "so this service would never come up; repair: rerun install-service with "
            "--socket PATH, or with --auth"
        )
    if print_only:
        click.echo(render_service(config).decode("utf-8"), nl=False)
        return
    destination = install_service(config, replace=replace)
    click.echo(f"Installed service unit: {destination}")
    click.echo(f"Recorded settings: {root / 'daemon' / 'service-install-v1.json'}")
    click.echo(
        "Enabled but not started. Start it with the service manager or `cruxible server start`."
    )


@dataclass(frozen=True)
class _InstanceStatusNeedsMatchingClient:
    version: str
    transport: str
    instance_id: str
    scope: Literal["instance"] = "instance"
    instance_status: Literal["needs_matching_client"] = "needs_matching_client"
    code: str = IncompatibleDaemonVersion.code
    message: str = "Instance section needs a matching client; lifecycle facts remain available."


def _echo_instance_scoped_status(
    client: DaemonLifecycleClient, instance_id: str, transport: str, output_json: bool
) -> None:
    version = client.version()
    try:
        checked = _get_checked_client()
    except IncompatibleDaemonVersion:
        partial = _InstanceStatusNeedsMatchingClient(version, transport, instance_id)
        if output_json:
            _emit_json(asdict(partial))
        else:
            click.echo(f"Daemon: reachable ({transport})")
            click.echo(f"Version: {version}")
            click.echo(f"Scope: instance {instance_id}")
            click.echo(partial.message)
        return
    if checked is None:
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)
    host = checked.show_playbill_host(instance_id)
    try:
        identity = checked.playbill_whoami(instance_id)
    except CoreError:  # an uninitialized host has no identity to read yet
        identity = None
    if output_json:
        _emit_json(
            {
                "scope": "instance",
                "instance_id": instance_id,
                "version": version,
                "transport": transport,
                "host": host.model_dump(mode="python"),
                "identity": None if identity is None else identity.model_dump(mode="python"),
            }
        )
        return
    click.echo(f"Daemon: reachable ({transport})")
    click.echo(f"Version: {version}")
    click.echo(
        f"Scope: instance {instance_id} (the credential is instance-scoped; daemon-wide "
        "status needs the bootstrap secret or a daemon-scope token)"
    )
    click.echo(
        f"Host {host.instance_id}: {host.compatibility} "
        f"({host.compiler_revision or '-'}, {host.compiler_coordinate or '-'}), "
        f"floor delivery {'on (default)' if host.floor_delivery else 'off (opted out)'}"
    )
    if host.reason is not None:
        click.echo(f"  Reason: {host.reason.code}: {host.reason.detail}")
    if identity is not None:
        click.echo(
            f"Actor: {identity.actor_id or 'none'} ({identity.credential_permission_mode}, "
            f"principal {identity.principal_registration_status})"
        )


@server_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def server_status_cmd(output_json: bool) -> None:
    """Report a running daemon's version, state root, transport, auth, and instances.

    A CLIENT command: it queries an already-running daemon over the configured
    transport (`--server-url` / `--server-socket` or the matching env vars). If
    no daemon is reachable it fails with a clear message rather than hanging.
    """
    client = _get_client()
    if client is None:
        raise click.UsageError(f"{SERVER_MODE_REQUIRED_MESSAGE} {_DAEMON_REQUIRED_HINT}")
    transport = _client_transport_label()
    try:
        result = client.server_info()
    except DaemonOperationScopeError as exc:
        # An instance-scoped credential cannot read daemon-wide state, but it
        # can read its own host and identity; answer with exactly that.
        _echo_instance_scoped_status(client, exc.credential_scope, transport, output_json)
        return
    if output_json:
        payload = result.model_dump(mode="python")
        payload["scope"] = "daemon"
        payload["transport"] = transport
        _emit_json(payload)
        return
    click.echo(f"Daemon: reachable ({transport})")
    click.echo(f"Version: {result.version}")
    click.echo(f"State root: {result.state_root}")
    click.echo(f"Instances: {result.instance_count}")
    click.echo(f"Compiler coordinate: {result.compiler_coordinate or '-'}")
    click.echo(f"Compiler revision: {result.compiler_revision or '-'}")
    for host in result.hosts:
        click.echo(
            f"Host {host.instance_id}: {host.compatibility} "
            f"({host.compiler_revision or '-'}, {host.compiler_coordinate or '-'}), "
            f"floor delivery {'on (default)' if host.floor_delivery else 'off (opted out)'}"
        )
        if host.reason is not None:
            click.echo(f"  Reason: {host.reason.code}: {host.reason.detail}")
    for consumer in result.consumers:
        click.echo(
            f"Consumer {consumer.instance_id} {consumer.kind} {consumer.consumer_id}: "
            f"{consumer.state}"
        )
    click.echo(f"Server required: {'yes' if result.server_required else 'no'}")
    click.echo(f"Auth enabled: {'yes' if result.auth_enabled else 'no'}")
    click.echo(f"Auth required: {'yes' if result.auth_required else 'no'}")
    click.echo(f"Provider lane: {result.provider_lane.state}")
    if result.provider_lane.code is not None:
        click.echo(
            f"Provider lane reason: {result.provider_lane.code}: {result.provider_lane.detail}"
        )
    elif result.provider_lane.detail is not None:
        click.echo(f"Provider lane detail: {result.provider_lane.detail}")


@server_group.command("restart")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.option(
    "--no-wait",
    is_flag=True,
    default=False,
    help="Return immediately after scheduling the restart, without confirming the daemon is back.",
)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Seconds to wait for the restarted daemon to answer again.",
)
@handle_errors
def server_restart_cmd(output_json: bool, no_wait: bool, timeout: float) -> None:
    """Re-exec the live daemon in place, preserving its port, state dir, and env.

    The daemon replaces its own process image, so picks up code changes without
    losing its transport or instances. By default this waits for the new image
    to answer before returning, giving the dev loop a one-command, skew-proof
    upgrade step.
    """
    client = _get_client()
    if client is None:
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)
    result = client.server_restart()

    confirmed_version: str | None = None
    if not no_wait:
        confirmed_version = _wait_for_daemon(client, timeout, old_boot_id=result.boot_id)

    if output_json:
        payload = result.model_dump(mode="python")
        payload["waited"] = not no_wait
        payload["confirmed_version"] = confirmed_version
        _emit_json(payload)
        return

    click.echo(f"Restart scheduled (was version {result.version}).")
    click.echo(f"State root: {result.state_root}")
    if no_wait:
        click.echo("Not waiting for the daemon to come back (--no-wait).")
    else:
        click.echo(f"Daemon is back on version {confirmed_version}.")
