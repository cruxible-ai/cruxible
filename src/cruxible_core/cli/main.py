"""CLI entry point and error handling."""

from __future__ import annotations

import functools
import os
import sys
from typing import Any

import click

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
    """Resolve the selected governed instance ID."""
    if instance_id is not None:
        return instance_id
    env_instance_id = os.environ.get("CRUXIBLE_INSTANCE_ID")
    if env_instance_id is not None:
        return env_instance_id
    return load_cli_context().instance_id


def handle_errors(f: Any) -> Any:
    """Decorator that catches CoreError and prints a friendly message."""

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except CoreError as e:
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
    envvar="CRUXIBLE_INSTANCE_ID",
    help="Opaque server-mode instance ID.",
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
    clone_cmd,
    config_views_cmd,
    connect_group,
    decision_records_cmd,
    evaluate,
    explain,
    export_group,
    feedback_batch_cmd,
    feedback_cmd,
    feedback_profile_cmd,
    find_candidates_cmd,
    get_entity_cmd,
    get_relationship_cmd,
    group_group,
    ingest,
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
    stats_cmd,
    test_cmd,
    validate,
    world_group,
)  # re-exported from cli.commands submodules

cli.add_command(init)  # type: ignore[has-type]
cli.add_command(validate)  # type: ignore[has-type]
cli.add_command(config_views_cmd, "config-views")  # type: ignore[has-type]
cli.add_command(connect_group, "context")  # type: ignore[has-type]
cli.add_command(decision_records_cmd, "decision-record")  # type: ignore[has-type]
cli.add_command(lock_cmd)  # type: ignore[has-type]
cli.add_command(world_group, "world")  # type: ignore[has-type]
cli.add_command(plan_cmd)  # type: ignore[has-type]
cli.add_command(run_cmd)  # type: ignore[has-type]
cli.add_command(apply_cmd)  # type: ignore[has-type]
cli.add_command(test_cmd)  # type: ignore[has-type]
cli.add_command(propose_cmd)  # type: ignore[has-type]
cli.add_command(snapshot_group, "snapshot")  # type: ignore[has-type]
cli.add_command(clone_cmd, "clone")  # type: ignore[has-type]
cli.add_command(ingest)  # type: ignore[has-type]
cli.add_command(query)  # type: ignore[has-type]
cli.add_command(render_wiki_cmd, "render-wiki")  # type: ignore[has-type]
cli.add_command(reload_config_cmd, "reload-config")  # type: ignore[has-type]
cli.add_command(server_group, "server")  # type: ignore[has-type]
cli.add_command(explain)  # type: ignore[has-type]
cli.add_command(feedback_cmd, "feedback")  # type: ignore[has-type]
cli.add_command(feedback_batch_cmd, "feedback-batch")  # type: ignore[has-type]
cli.add_command(feedback_profile_cmd, "feedback-profile")  # type: ignore[has-type]
cli.add_command(analyze_feedback_cmd, "analyze-feedback")  # type: ignore[has-type]
cli.add_command(outcome_cmd, "outcome")  # type: ignore[has-type]
cli.add_command(outcome_profile_cmd, "outcome-profile")  # type: ignore[has-type]
cli.add_command(analyze_outcomes_cmd, "analyze-outcomes")  # type: ignore[has-type]
cli.add_command(list_group, "list")  # type: ignore[has-type]
cli.add_command(find_candidates_cmd, "find-candidates")  # type: ignore[has-type]
cli.add_command(schema)  # type: ignore[has-type]
cli.add_command(stats_cmd, "stats")  # type: ignore[has-type]
cli.add_command(sample)  # type: ignore[has-type]
cli.add_command(evaluate)  # type: ignore[has-type]
cli.add_command(lint_cmd, "lint")  # type: ignore[has-type]
cli.add_command(inspect_group, "inspect")  # type: ignore[has-type]
cli.add_command(get_entity_cmd, "get-entity")  # type: ignore[has-type]
cli.add_command(get_relationship_cmd, "get-relationship")  # type: ignore[has-type]
cli.add_command(add_entity_cmd, "add-entity")  # type: ignore[has-type]
cli.add_command(add_relationship_cmd, "add-relationship")  # type: ignore[has-type]
cli.add_command(add_constraint_cmd, "add-constraint")  # type: ignore[has-type]
cli.add_command(add_decision_policy_cmd, "add-decision-policy")  # type: ignore[has-type]
cli.add_command(export_group, "export")  # type: ignore[has-type]
cli.add_command(group_group, "group")  # type: ignore[has-type]
