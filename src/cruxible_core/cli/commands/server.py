"""CLI commands for live daemon status and diagnostics."""

from __future__ import annotations

import time

import click

from cruxible_client import CruxibleClient
from cruxible_core.cli.commands._common import (
    SERVER_MODE_REQUIRED_MESSAGE,
    _emit_json,
    _get_client,
)
from cruxible_core.cli.main import handle_errors

# Poll cadence while waiting for the re-exec'd daemon to start answering again.
_RESTART_POLL_INTERVAL_SECONDS = 0.25


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


@click.group("server")
def server_group() -> None:
    """Inspect live daemon state."""


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
