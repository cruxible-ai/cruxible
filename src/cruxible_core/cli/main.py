"""CLI entry point and error handling."""

from __future__ import annotations

import functools
import os
import sys
from typing import Any

import click
import httpx

from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.cli.context import load_cli_context
from cruxible_core.errors import ConfigError
from cruxible_core.server.config import resolve_server_settings


def _resolve_cli_transport(
    *,
    server_url: str | None,
    server_socket: str | None,
) -> tuple[str | None, str | None]:
    """Resolve transport settings atomically across flags, env, and stored context."""
    stored = load_cli_context()
    env_server_url = os.environ.get("CRUXIBLE_SERVER_URL")
    env_server_socket = os.environ.get("CRUXIBLE_SERVER_SOCKET")

    if server_url is not None or server_socket is not None:
        return server_url, server_socket
    if env_server_url is not None or env_server_socket is not None:
        return env_server_url, env_server_socket
    return stored.server_url, stored.server_socket


def _resolve_cli_instance_id(instance_id: str | None) -> str | None:
    """Resolve the active governed instance ID."""
    if instance_id is not None:
        return instance_id
    return load_cli_context().instance_id


def _active_transport_label(exc: httpx.TransportError) -> str:
    ctx = click.get_current_context(silent=True)
    root_obj = {}
    if ctx is not None:
        root = ctx.find_root()
        if isinstance(root.obj, dict):
            root_obj = root.obj
    server_url = root_obj.get("server_url")
    server_socket = root_obj.get("server_socket")
    if server_url:
        return str(server_url)
    if server_socket:
        return f"unix socket {server_socket}"
    request = getattr(exc, "request", None)
    if request is not None:
        return str(request.url)
    return "configured Cruxible server"


def handle_errors(f: Any) -> Any:
    """Decorator that catches any Cruxible error and prints a friendly message.

    Core errors subclass the client base, so the client hierarchy is the
    single catch surface for local and remote failures.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except ServerUnreachableError as e:
            # Transport failures already render as a friendly single line; the
            # class-name prefix would only add noise, so emit the message as-is.
            click.secho(f"Error: {e}", fg="red", err=True)
            sys.exit(1)
        except ClientCoreError as e:
            click.secho(f"Error: {e.__class__.__name__}: {e}", fg="red", err=True)
            sys.exit(1)
        except httpx.TransportError as e:
            click.secho(
                f"Error: could not reach Cruxible server at {_active_transport_label(e)}: {e}",
                fg="red",
                err=True,
            )
            sys.exit(1)

    return wrapper


@click.group()
@click.version_option(package_name="cruxible")
@click.option("--server-url", default=None, help="Remote Cruxible server base URL.")
@click.option(
    "--server-socket",
    default=None,
    help="Local Cruxible server Unix socket path.",
)
@click.option(
    "--instance-id",
    default=None,
    help="Opaque server-mode instance ID. Defaults to remembered CLI context.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    server_url: str | None,
    server_socket: str | None,
    instance_id: str | None,
) -> None:
    """Cruxible — hard state for AI agents: governed, queryable, durable, with receipts."""
    try:
        resolved_url, resolved_socket = _resolve_cli_transport(
            server_url=server_url,
            server_socket=server_socket,
        )
        resolved_instance_id = _resolve_cli_instance_id(instance_id)
        settings = resolve_server_settings(
            server_url=resolved_url,
            server_socket=resolved_socket,
        )
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc

    ctx.ensure_object(dict)
    ctx.obj.update(
        {
            "server_url": settings.server_url,
            "server_socket": settings.server_socket,
            "instance_id": resolved_instance_id,
            "require_server": settings.require_server,
        }
    )


# Import and register commands after cli group is defined
from cruxible_core.cli.commands import (  # noqa: E402
    add_constraint_cmd,
    add_decision_policy_cmd,
    add_entity_cmd,
    add_relationship_cmd,
    analyze_feedback_cmd,
    analyze_outcomes_cmd,
    apply_cmd,
    batch_direct_write_cmd,
    clone_cmd,
    config_expand_cmd,
    config_views_cmd,
    connect_group,
    credential_group,
    decision_records_cmd,
    evaluate,
    explain,
    export_group,
    feedback_group,
    get_entity_cmd,
    get_relationship_cmd,
    group_group,
    init,
    inspect_entity_cmd,
    inspect_entity_history_cmd,
    inspect_group,
    inspect_relationship_lineage_cmd,
    instance_group,
    lint_cmd,
    list_group,
    lock_cmd,
    outcome_group,
    plan_cmd,
    propose_cmd,
    query,
    reload_config_cmd,
    run_cmd,
    sample,
    schema,
    server_group,
    snapshot_group,
    source_group,
    state_group,
    stats_cmd,
    test_cmd,
    update_entity_cmd,
    update_relationship_cmd,
    validate,
)  # re-exported from cli.commands submodules

cli.add_command(init)
cli.add_command(validate)
cli.add_command(connect_group, "context")
cli.add_command(decision_records_cmd, "decision-record")
cli.add_command(credential_group, "credential")
cli.add_command(lock_cmd)
cli.add_command(state_group, "state")
cli.add_command(instance_group, "instance")
cli.add_command(plan_cmd)
cli.add_command(run_cmd)
cli.add_command(apply_cmd)
cli.add_command(test_cmd)
cli.add_command(propose_cmd)
cli.add_command(snapshot_group, "snapshot")
cli.add_command(source_group, "source")
cli.add_command(clone_cmd, "clone")
cli.add_command(query)
cli.add_command(server_group, "server")
cli.add_command(explain)
cli.add_command(list_group, "list")
cli.add_command(schema)
cli.add_command(stats_cmd, "stats")
cli.add_command(sample)
cli.add_command(evaluate)
cli.add_command(lint_cmd, "lint")
cli.add_command(inspect_group, "inspect")

# config group: config-editing and review-surface verbs.
config_group = click.Group("config", help="Edit, validate, and render the active config.")
config_group.add_command(reload_config_cmd, "reload")
config_group.add_command(config_views_cmd, "views")
config_group.add_command(config_expand_cmd, "expand")
config_group.add_command(add_constraint_cmd, "add-constraint")
config_group.add_command(add_decision_policy_cmd, "add-decision-policy")
cli.add_command(config_group, "config")

# feedback group: record/batch/from-query/profile (defined in feedback.py) plus analyze.
feedback_group.add_command(analyze_feedback_cmd, "analyze")
cli.add_command(feedback_group, "feedback")

# outcome group: record/profile (defined in feedback.py) plus analyze.
outcome_group.add_command(analyze_outcomes_cmd, "analyze")
cli.add_command(outcome_group, "outcome")

entity_group = click.Group("entity", help="Entity reads and writes.")
entity_group.add_command(add_entity_cmd, "add")
entity_group.add_command(update_entity_cmd, "update")
entity_group.add_command(get_entity_cmd, "get")
entity_group.add_command(inspect_entity_cmd, "inspect")
entity_group.add_command(inspect_entity_history_cmd, "history")

relationship_group = click.Group("relationship", help="Relationship reads and writes.")
relationship_group.add_command(add_relationship_cmd, "add")
relationship_group.add_command(update_relationship_cmd, "update")
relationship_group.add_command(get_relationship_cmd, "get")
relationship_group.add_command(inspect_relationship_lineage_cmd, "lineage")

cli.add_command(entity_group, "entity")
cli.add_command(relationship_group, "relationship")
cli.add_command(batch_direct_write_cmd, "batch-direct-write")
cli.add_command(export_group, "export")
cli.add_command(group_group, "group")
