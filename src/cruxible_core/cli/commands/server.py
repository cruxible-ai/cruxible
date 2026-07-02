"""CLI commands for launching and inspecting the Cruxible daemon.

This group holds both the daemon-launch verb and the client RPCs:

* ``start`` LAUNCHES the daemon in the foreground. It takes no ``--server-url``;
  it is the process that becomes the daemon. ``--host`` / ``--port`` /
  ``--state-dir`` mirror ``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` /
  ``CRUXIBLE_SERVER_STATE_DIR`` (env vars are honored as defaults).
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

import click

from cruxible_client import CruxibleClient
from cruxible_core.cli.commands._common import (
    SERVER_MODE_REQUIRED_MESSAGE,
    _emit_json,
    _get_client,
    _root_ctx_obj,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    is_server_auth_enabled,
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
            "Hosted init: set CRUXIBLE_SERVER_BEARER_TOKEN to the bootstrap secret file "
            "contents, "
            "then run `cruxible init --kit <ref> --bootstrap`.",
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
        "Hosted init: set CRUXIBLE_SERVER_BEARER_TOKEN to the bootstrap secret, "
        "then run `cruxible init --kit <ref> --bootstrap`.",
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
    "--state-dir",
    default=None,
    help="Server-owned state directory (default: CRUXIBLE_SERVER_STATE_DIR or ~/.cruxible/server).",
)
@click.option(
    "--socket",
    "socket_path",
    default=None,
    help="Listen on this Unix socket path instead of host/port (default: CRUXIBLE_SERVER_SOCKET).",
)
@click.option(
    "--bootstrap-secret-file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write an auto-generated runtime bootstrap secret to this file with mode 0600.",
)
@handle_errors
def server_start_cmd(
    host: str | None,
    port: int | None,
    state_dir: str | None,
    socket_path: str | None,
    bootstrap_secret_file: str | None,
) -> None:
    """Launch the Cruxible daemon in the foreground.

    This becomes the long-running daemon process; it is NOT a client of an
    existing one, so it takes no `--server-url`. Flags override the matching
    environment variables (`CRUXIBLE_HOST`, `CRUXIBLE_PORT`,
    `CRUXIBLE_SERVER_STATE_DIR`, `CRUXIBLE_SERVER_SOCKET`); unset flags fall back
    to the env value or the built-in default. Use a durable `--state-dir` (e.g.
    `~/.cruxible/server`), not a volatile temp path. Stop with Ctrl-C.
    """
    _prepare_generated_bootstrap_secret(bootstrap_secret_file)
    # Imported lazily so `cruxible server start --help` (and the rest of the CLI)
    # never pays the uvicorn/server import cost, and so the optional `server`
    # extra is only required when actually launching.
    from cruxible_core.server.app import run_server

    run_server(host=host, port=port, state_dir=state_dir, socket_path=socket_path)


@server_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def server_status_cmd(output_json: bool) -> None:
    """Report a running daemon's version, state dir, transport, and instances.

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
    click.echo(f"State dir: {result.state_dir}")
    click.echo(f"Instances: {result.instance_count}")
    click.echo(f"Auth enabled: {'yes' if result.auth_enabled else 'no'}")
    click.echo(f"Auth required: {'yes' if result.auth_required else 'no'}")


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
    click.echo(f"State dir: {result.state_dir}")
    click.echo(f"Instances: {result.instance_count}")


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
    click.echo(f"State dir: {result.state_dir}")
    if no_wait:
        click.echo("Not waiting for the daemon to come back (--no-wait).")
    else:
        click.echo(f"Daemon is back on version {confirmed_version}.")
