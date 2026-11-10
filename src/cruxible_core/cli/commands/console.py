"""`cruxible console`: open the read-only console in a browser, served from this install."""

from __future__ import annotations

import secrets
import threading
import webbrowser
from typing import TYPE_CHECKING, Any

import click
import httpx

from cruxible_core.cli.commands._common import _root_ctx_obj, no_daemon_message
from cruxible_core.cli.main import handle_errors, long_running_command

if TYPE_CHECKING:
    from cruxible_core.console.listener import Upstream

_NO_BUNDLE = (
    "This install has no built console (nothing in {root}). Release wheels include "
    "it. From a source checkout, build it into the package with `python "
    "scripts/build_console.py --console PATH --commit SHA`, or run the console's dev "
    "server instead (`pnpm dev` in a cruxible-app checkout, as its README says)."
)


def _upstream(obj: dict[str, Any]) -> Upstream:
    from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER
    from cruxible_client.transport.http import validate_principal_id
    from cruxible_core.console.listener import Upstream
    from cruxible_core.server.config import get_runtime_bearer_token

    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if not server_url and not server_socket:
        raise click.UsageError(no_daemon_message("cruxible console"))
    # The same credential and principal the CLI's own client would send.
    headers: dict[str, str] = {}
    token = get_runtime_bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    principal_id = obj.get("principal_id")
    if principal_id:
        headers[PRINCIPAL_ID_HEADER] = validate_principal_id(principal_id)
    if server_socket:
        return Upstream(
            label=f"unix socket {server_socket}",
            socket_path=str(server_socket),
            headers=headers,
            start_hint=f"start it with `cruxible server start --socket {server_socket}`",
        )
    return Upstream(
        label=str(server_url),
        base_url=str(server_url).rstrip("/"),
        headers=headers,
        start_hint="start it with `cruxible server start --auth`",
    )


def _probe(upstream: Upstream, instance_id: str, pin: dict[str, Any] | None) -> None:
    """Fail now, in the terminal, when the daemon or the instance cannot be read."""

    transport = httpx.HTTPTransport(uds=upstream.socket_path) if upstream.socket_path else None
    with httpx.Client(
        base_url=upstream.base_url or "http://cruxible",
        headers=dict(upstream.headers),
        transport=transport,
        timeout=httpx.Timeout(10.0),
    ) as client:
        try:
            version = client.get("/version")
            head = client.get(f"/api/v1/{instance_id}/head")
        except httpx.TransportError as exc:
            raise click.ClickException(
                f"the Cruxible daemon at {upstream.label} is not answering "
                f"({exc.__class__.__name__}); {upstream.start_hint}."
            ) from exc
    if head.status_code != 200:
        try:
            detail = str(head.json().get("message") or head.text)
        except ValueError:
            detail = head.text
        if head.status_code in (401, 403):
            raise click.ClickException(
                f"the daemon at {upstream.label} refused to read {instance_id}: {detail}. "
                "A TCP daemon needs a credential: export CRUXIBLE_SERVER_BEARER_TOKEN "
                "(a read-only one is enough: `cruxible credential mint --mode read_only`)."
            )
        if head.status_code == 404:
            raise click.ClickException(
                f"the daemon at {upstream.label} has no instance {instance_id}: {detail}"
            )
        click.echo(
            f"warning: reading {instance_id}'s head answered {head.status_code}: {detail}", err=True
        )
    if pin and version.status_code == 200:
        served = version.json()
        if served.get("sdk_contract_snapshot_digest") != pin.get("sdk_contract_snapshot_digest"):
            click.echo(
                f"warning: this console was built for cruxible {pin.get('core_version')} "
                f"({pin.get('sdk_contract_snapshot_digest')}); the daemon serves "
                f"{served.get('version')} ({served.get('sdk_contract_snapshot_digest')}). "
                "Some screens may render incompletely.",
                err=True,
            )


@click.command("console")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    show_default=True,
    help="Loopback port to listen on; 0 picks a free one.",
)
@click.option("--no-open", is_flag=True, default=False, help="Print the link; open no browser.")
@click.option(
    "--instance",
    "instance",
    default=None,
    metavar="ID",
    help="Instance to open (default: the one the CLI context names).",
)
@handle_errors
@long_running_command
def console_cmd(port: int, no_open: bool, instance: str | None) -> None:
    """Open the read-only console in your browser, served by this install.

    Listens on 127.0.0.1 only and forwards the console's read routes to the
    daemon the CLI context names; every other route is refused. The link it
    prints signs one browser in, once. Ctrl-C stops it.
    """

    from cruxible_client.contracts.authoring.models import (
        AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    )
    from cruxible_core import __version__
    from cruxible_core.console.bundle import (
        ConsoleBundleInvalid,
        ConsoleBundleMissing,
        default_dist_dir,
        load_bundle,
    )
    from cruxible_core.console.listener import (
        ConsoleListener,
        PortUnavailable,
        bind_loopback,
        serve,
    )

    try:
        bundle = load_bundle()
    except ConsoleBundleMissing as exc:
        raise click.ClickException(_NO_BUNDLE.format(root=default_dist_dir())) from exc
    except ConsoleBundleInvalid as exc:
        raise click.ClickException(f"the installed console build is damaged: {exc}") from exc

    obj = _root_ctx_obj()
    upstream = _upstream(obj)
    instance_id = instance or obj.get("instance_id")
    if not instance_id:
        raise click.UsageError(
            "cruxible console needs an instance: pass --instance ID, or remember one "
            "with `cruxible context use ID`."
        )
    pin = bundle.manifest.get("console_pin")
    _probe(upstream, instance_id, pin if isinstance(pin, dict) else None)

    try:
        sock = bind_loopback(port)
    except PortUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    bound = sock.getsockname()[1]
    bootstrap = secrets.token_urlsafe(32)
    app = ConsoleListener(
        bundle=bundle,
        upstream=upstream,
        instance_id=instance_id,
        port=bound,
        bootstrap_token=bootstrap,
        core_identity={
            "version": __version__,
            "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
        },
    )
    link = f"http://127.0.0.1:{bound}/__console/bootstrap?token={bootstrap}"
    click.echo(
        f"Cruxible console for {instance_id}, reading through {upstream.label} (read-only).",
        err=True,
    )
    click.echo(
        "The link signs one browser in, once; run again for another. Ctrl-C stops it.",
        err=True,
    )
    click.echo(link)
    if not no_open:
        threading.Thread(target=webbrowser.open, args=(link,), daemon=True).start()
    serve(app, sock)
