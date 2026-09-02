"""CLI commands for launching and inspecting the Cruxible daemon.

This group holds both the daemon-launch verb and the client RPCs:

* ``start`` LAUNCHES the daemon in the foreground. It takes no ``--server-url``;
  it is the process that becomes the daemon. ``--host`` / ``--port`` /
  ``--state-root`` mirror ``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` /
  ``CRUXIBLE_STATE_ROOT`` (env vars are honored as defaults).
* ``status`` / ``info`` / ``restart`` are CLIENT RPCs that talk to an
  already-running daemon. They require a transport (``--server-url`` /
  ``--server-socket``, or the ``CRUXIBLE_SERVER_URL`` / ``CRUXIBLE_SERVER_SOCKET``
  env vars, or a remembered CLI context) and fail with a clear message when no
  daemon is reachable.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Literal, cast

import click

from cruxible_client import CruxibleClient
from cruxible_core.cli.commands._common import (
    SERVER_MODE_REQUIRED_MESSAGE,
    _emit_json,
    _get_client,
    _root_ctx_obj,
)
from cruxible_core.cli.main import handle_errors, long_running_command
from cruxible_core.runtime.permissions import PERMISSION_MODE_NAMES
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    get_server_state_root,
    is_server_auth_enabled,
)
from cruxible_core.server.service_install import (
    ServiceInstallConfigV1,
    current_service_platform,
    durable_credentials_available,
    install_service,
    load_service_config,
    render_service,
    resolved_cruxible_executable,
    service_config_path,
)

# Poll cadence while waiting for the re-exec'd daemon to start answering again.
_RESTART_POLL_INTERVAL_SECONDS = 0.25

# Client RPCs (status/info/restart) need a reachable daemon; surface a single,
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


def _wait_for_daemon(client: CruxibleClient, timeout: float) -> str:
    """Poll the daemon's /version probe until it answers or the budget expires.

    Returns the version reported by the restarted daemon. Raising here surfaces
    a skew-proof failure: the command only succeeds once the new image responds.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return client.version()
        except Exception as exc:  # connection refused while the image is replaced
            last_error = exc
            time.sleep(_RESTART_POLL_INTERVAL_SECONDS)
    raise click.ClickException(
        f"Daemon did not come back within {timeout:.0f}s after restart"
        + (f": {last_error}" if last_error is not None else "")
    )


def _write_bootstrap_secret_file(path: Path, secret: str) -> Path:
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{secret}\n")
    resolved.chmod(0o600)
    return resolved


def _prepare_generated_bootstrap_secret(bootstrap_secret_file: str | None) -> None:
    """Generate the one-time runtime bootstrap secret when auth needs one."""
    if not is_server_auth_enabled() or get_runtime_bootstrap_secret() is not None:
        return

    secret = secrets.token_urlsafe(32)
    os.environ["CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET"] = secret

    written_path: Path | None = None
    if bootstrap_secret_file is not None:
        written_path = _write_bootstrap_secret_file(Path(bootstrap_secret_file), secret)

    if written_path is not None:
        click.echo(f"Wrote bootstrap secret file: {written_path} (0600)", err=True)
        click.echo(
            "Set CRUXIBLE_SERVER_BEARER_TOKEN to the bootstrap secret file contents, "
            "then run `cruxible playbill host create`.",
            err=True,
        )
        click.echo(
            f"Claim admin token: cruxible credential claim-bootstrap --secret-file {written_path}",
            err=True,
        )
        return

    click.echo("Generated runtime bootstrap secret:", err=True)
    click.echo(secret, err=True)
    click.echo("Save it now; this value is printed only once.", err=True)
    click.echo(
        "Set CRUXIBLE_SERVER_BEARER_TOKEN to the bootstrap secret, then run "
        "`cruxible playbill host create`.",
        err=True,
    )
    click.echo("Claim admin token: cruxible credential claim-bootstrap", err=True)


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
    "--bootstrap-secret-file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write an auto-generated runtime bootstrap secret to this file with mode 0600.",
)
@handle_errors
@long_running_command
def server_start_cmd(
    host: str | None,
    port: int | None,
    state_root: str | None,
    socket_path: str | None,
    capability_ceiling: str | None,
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
    """
    _prepare_generated_bootstrap_secret(bootstrap_secret_file)
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
        if config.auth_enabled and not durable_credentials_available(root):
            raise click.UsageError(
                "recorded auth-on service no longer has an active durable runtime credential; "
                "repair: run `cruxible server start --bootstrap-secret-file PATH`, claim the "
                "bootstrap credential, then rerun install-service"
            )
        click.echo(render_service(config).decode("utf-8"), nl=False)
        return

    auth_enabled = bool(auth)
    if auth_enabled and not durable_credentials_available(root):
        raise click.UsageError(
            "auth-on unattended startup requires an active durable runtime credential; "
            "repair: run `cruxible server start --bootstrap-secret-file PATH`, claim the "
            "bootstrap credential, then rerun install-service"
        )
    config = ServiceInstallConfigV1(
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
    if print_only:
        click.echo(render_service(config).decode("utf-8"), nl=False)
        return
    destination = install_service(config, replace=replace)
    click.echo(f"Installed service unit: {destination}")
    click.echo(f"Recorded settings: {root / 'daemon' / 'service-install-v1.json'}")
    click.echo(
        "Enabled but not started. Start it with the service manager or `cruxible server start`."
    )


@server_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def server_status_cmd(output_json: bool) -> None:
    """Report a running daemon's version, state root, transport, and instances.

    A CLIENT command: it queries an already-running daemon over the configured
    transport (`--server-url` / `--server-socket` or the matching env vars). If
    no daemon is reachable it fails with a clear message rather than hanging.
    """
    client = _get_client()
    if client is None:
        raise click.UsageError(f"{SERVER_MODE_REQUIRED_MESSAGE} {_DAEMON_REQUIRED_HINT}")
    result = client.server_info()
    transport = _client_transport_label()
    if output_json:
        payload = result.model_dump(mode="python")
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
            f"({host.compiler_revision or '-'}, {host.compiler_coordinate or '-'})"
        )
        if host.reason is not None:
            click.echo(f"  Reason: {host.reason.code}: {host.reason.detail}")
    click.echo(f"Auth enabled: {'yes' if result.auth_enabled else 'no'}")
    click.echo(f"Auth required: {'yes' if result.auth_required else 'no'}")
    click.echo(f"Provider lane: {result.provider_lane.state}")
    if result.provider_lane.code is not None:
        click.echo(
            f"Provider lane reason: {result.provider_lane.code}: {result.provider_lane.detail}"
        )
    elif result.provider_lane.detail is not None:
        click.echo(f"Provider lane detail: {result.provider_lane.detail}")


@server_group.command("info")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def server_info_cmd(output_json: bool) -> None:
    """Show live daemon metadata such as transport policy and state dir."""
    client = _get_client()
    if client is None:
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)
    result = client.server_info()
    if output_json:
        _emit_json(result.model_dump(mode="python"))
        return
    click.echo(f"Version: {result.version}")
    click.echo(f"Server required: {'yes' if result.server_required else 'no'}")
    click.echo(f"Auth enabled: {'yes' if result.auth_enabled else 'no'}")
    click.echo(f"Auth required: {'yes' if result.auth_required else 'no'}")
    click.echo(f"State root: {result.state_root}")
    click.echo(f"Instances: {result.instance_count}")
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
        confirmed_version = _wait_for_daemon(client, timeout)

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
