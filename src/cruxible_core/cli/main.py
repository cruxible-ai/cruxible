"""CLI entry point and error handling."""

from __future__ import annotations

import functools
import os
import sys
from typing import Any

import click

from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_core.cli.context import load_cli_context
from cruxible_core.errors import ConfigError, CoreError
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


def handle_errors(f: Any) -> Any:
    """Decorator that catches core and client CoreError and prints a friendly message."""

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except (CoreError, ClientCoreError) as e:
            click.secho(f"Error: {e}", fg="red", err=True)
            sys.exit(1)

    return wrapper


@click.group()
@click.version_option(package_name="cruxible-core")
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
    """Cruxible — deterministic decision engine with receipts."""
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
    config_views_cmd,
    connect_group,
    decision_records_cmd,
    evaluate,
    explain,
    export_group,
    feedback_batch_cmd,
    feedback_cmd,
    feedback_from_query_cmd,
    feedback_profile_cmd,
    get_entity_cmd,
    get_relationship_cmd,
    group_group,
    init,
    inspect_group,
    lint_cmd,
    list_group,
    lock_cmd,
    outcome_cmd,
    outcome_profile_cmd,
    plan_cmd,
    propose_cmd,
    query,
    reload_config_cmd,
    render_wiki_cmd,
    run_cmd,
    sample,
    schema,
    server_group,
    snapshot_group,
    source_group,
    stats_cmd,
    test_cmd,
    validate,
    world_group,
)  # re-exported from cli.commands submodules

cli.add_command(init)
cli.add_command(validate)
cli.add_command(config_views_cmd, "config-views")
cli.add_command(connect_group, "context")
cli.add_command(decision_records_cmd, "decision-record")
cli.add_command(lock_cmd)
cli.add_command(world_group, "world")
cli.add_command(plan_cmd)
cli.add_command(run_cmd)
cli.add_command(apply_cmd)
cli.add_command(test_cmd)
cli.add_command(propose_cmd)
cli.add_command(snapshot_group, "snapshot")
cli.add_command(source_group, "source")
cli.add_command(clone_cmd, "clone")
cli.add_command(query)
cli.add_command(render_wiki_cmd, "render-wiki")
cli.add_command(reload_config_cmd, "reload-config")
cli.add_command(server_group, "server")
cli.add_command(explain)
cli.add_command(feedback_cmd, "feedback")
cli.add_command(feedback_batch_cmd, "feedback-batch")
cli.add_command(feedback_from_query_cmd, "feedback-from-query")
cli.add_command(feedback_profile_cmd, "feedback-profile")
cli.add_command(analyze_feedback_cmd, "analyze-feedback")
cli.add_command(outcome_cmd, "outcome")
cli.add_command(outcome_profile_cmd, "outcome-profile")
cli.add_command(analyze_outcomes_cmd, "analyze-outcomes")
cli.add_command(list_group, "list")
cli.add_command(schema)
cli.add_command(stats_cmd, "stats")
cli.add_command(sample)
cli.add_command(evaluate)
cli.add_command(lint_cmd, "lint")
cli.add_command(inspect_group, "inspect")
cli.add_command(get_entity_cmd, "get-entity")
cli.add_command(get_relationship_cmd, "get-relationship")
cli.add_command(add_entity_cmd, "add-entity")
cli.add_command(add_relationship_cmd, "add-relationship")
cli.add_command(batch_direct_write_cmd, "batch-direct-write")
cli.add_command(add_constraint_cmd, "add-constraint")
cli.add_command(add_decision_policy_cmd, "add-decision-policy")
cli.add_command(export_group, "export")
cli.add_command(group_group, "group")
