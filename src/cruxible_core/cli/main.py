"""CLI entry point and error handling."""

from __future__ import annotations

import functools
import importlib
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from cruxible_core.cli.context import load_cli_context
from cruxible_core.errors import ConfigError
from cruxible_core.server.config import resolve_server_settings

if TYPE_CHECKING:
    import httpx

# Authoritative CLI inventory for commands that write authoritative state or
# write an artifact from a selected instance. ``handle_errors`` consults this
# mapping immediately before invoking the command callback, which keeps target
# visibility centralized and leaves every unlisted read command silent.
#
# Modes:
# - active: acts on the selected instance.
# - create: creates/restores an instance and therefore has no instance ID yet.
# - lock: acts on the selected instance unless --kit-dir names an explicit kit.
# - manual: the command resolves its target from command-specific inputs and
#   emits the notice itself immediately before the write.
MUTATING_COMMAND_TARGETS: dict[tuple[str, ...], str] = {
    ("init",): "create",
    ("lock",): "lock",
    ("run",): "active",
    ("apply",): "active",
    ("propose",): "active",
    ("snapshot", "create"): "active",
    ("clone",): "active",
    ("source", "register"): "active",
    ("state", "publish"): "active",
    ("state", "create-overlay"): "create",
    ("state", "pull-apply"): "active",
    ("instance", "backup"): "active",
    ("instance", "restore"): "create",
    ("instance", "relocate"): "active",
    ("credential", "claim-bootstrap"): "active",
    ("credential", "mint"): "active",
    ("credential", "recover-admin"): "manual",
    ("credential", "revoke"): "active",
    ("credential", "rotate"): "active",
    ("decision-record", "create"): "active",
    ("decision-record", "finalize"): "active",
    ("decision-record", "abandon"): "active",
    ("config", "reload"): "active",
    ("config", "add-constraint"): "active",
    ("config", "add-decision-policy"): "active",
    ("feedback", "record"): "active",
    ("feedback", "from-query"): "active",
    ("feedback", "batch"): "active",
    ("outcome", "record"): "active",
    ("entity", "add"): "active",
    ("entity", "update"): "active",
    ("relationship", "add"): "active",
    ("relationship", "update"): "active",
    ("batch-direct-write",): "active",
    ("group", "propose"): "active",
    ("group", "resolve"): "active",
    ("group", "trust"): "active",
}


def _command_path(ctx: click.Context) -> tuple[str, ...]:
    """Return the registered command path below the root CLI group."""
    names: list[str] = []
    current: click.Context | None = ctx
    while current is not None and current.parent is not None:
        if current.command.name:
            names.append(current.command.name)
        current = current.parent
    return tuple(reversed(names))


def _target_source(
    *,
    explicit: bool,
    environment: bool,
    remembered: bool,
) -> str:
    if explicit:
        return "explicit"
    if environment:
        return "environment"
    if remembered:
        return "remembered"
    return "local"


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
            ctx = click.get_current_context(silent=True)
            if ctx is not None:
                target_mode = MUTATING_COMMAND_TARGETS.get(_command_path(ctx))
                if target_mode is not None and target_mode != "manual":
                    # Runtime import avoids the main <-> commands import cycle.
                    from cruxible_core.cli.commands._common import _echo_write_target

                    _echo_write_target(target_mode, kwargs)
            return f(*args, **kwargs)
        except Exception as exc:
            # Error packages and HTTP transport support stay off the import path
            # until a command actually fails. Core errors share the client base,
            # so this remains one catch surface for local and remote execution.
            from cruxible_client.errors import CoreError as ClientCoreError
            from cruxible_client.errors import ServerUnreachableError

            if isinstance(exc, ServerUnreachableError):
                # Transport failures already render as a friendly single line;
                # the class-name prefix would only add noise.
                click.secho(f"Error: {exc}", fg="red", err=True)
                sys.exit(1)
            if isinstance(exc, ClientCoreError):
                click.secho(
                    f"Error: {exc.__class__.__name__}: {exc}",
                    fg="red",
                    err=True,
                )
                sys.exit(1)

            import httpx

            if isinstance(exc, httpx.TransportError):
                click.secho(
                    "Error: could not reach Cruxible server at "
                    f"{_active_transport_label(exc)}: {exc}",
                    fg="red",
                    err=True,
                )
                sys.exit(1)
            raise

    return wrapper


@dataclass(frozen=True)
class LazyCommandSpec:
    """Import target plus static help metadata for one CLI command."""

    module: str | None
    attr: str | None
    help: str
    commands: dict[str, LazyCommandSpec] | None = None


class LazyCommand(click.Command):
    """Lightweight command placeholder that delegates on first real access."""

    def __init__(self, name: str, spec: LazyCommandSpec) -> None:
        super().__init__(name=name, help=spec.help)
        self._lazy_spec = spec
        self._lazy_loaded: click.Command | None = None

    def _load(self) -> click.Command:
        command = self._lazy_loaded
        if command is None:
            spec = self._lazy_spec
            if spec.module is None or spec.attr is None:
                raise RuntimeError(f"Lazy command {self.name!r} has no import target")
            module = importlib.import_module(spec.module)
            command = getattr(module, spec.attr)
            if not isinstance(command, click.Command):
                raise TypeError(f"{spec.module}:{spec.attr} is not a Click command")
            self._lazy_loaded = command
        return command

    def __getattribute__(self, name: str) -> Any:
        # A few tests and Click completion integrations inspect command
        # callbacks/options through ``cli.commands`` instead of invoking the
        # command. Treat that as first access and preserve the public behavior.
        if name in {"callback", "params"} and "_lazy_spec" in vars(self):
            return getattr(self._load(), name)
        return super().__getattribute__(name)

    def make_context(self, *args: Any, **kwargs: Any) -> click.Context:
        return self._load().make_context(*args, **kwargs)

    def shell_complete(self, *args: Any, **kwargs: Any) -> list[Any]:
        return self._load().shell_complete(*args, **kwargs)


class LazyGroup(click.Group):
    """Click group whose registry is visible without importing command modules."""

    def __init__(
        self,
        *args: Any,
        lazy_spec: LazyCommandSpec | None = None,
        lazy_subcommands: dict[str, LazyCommandSpec] | None = None,
        **kwargs: Any,
    ) -> None:
        self._lazy_spec = lazy_spec
        self._lazy_loaded: click.Group | None = None
        super().__init__(*args, **kwargs)
        if lazy_subcommands:
            self._install_lazy_commands(lazy_subcommands)

    def _install_lazy_commands(self, specs: dict[str, LazyCommandSpec]) -> None:
        for name, spec in specs.items():
            if spec.commands is None:
                command: click.Command = LazyCommand(name, spec)
            else:
                command = LazyGroup(
                    name=name,
                    help=spec.help,
                    lazy_spec=spec,
                    lazy_subcommands=spec.commands,
                )
            self.add_command(command, name)

    def _load(self) -> click.Group:
        group = self._lazy_loaded
        if group is None:
            spec = self._lazy_spec
            if spec is None or spec.module is None or spec.attr is None:
                return self
            module = importlib.import_module(spec.module)
            group = getattr(module, spec.attr)
            if not isinstance(group, click.Group):
                raise TypeError(f"{spec.module}:{spec.attr} is not a Click group")
            for name, command in self.commands.items():
                if name not in group.commands:
                    group.add_command(command, name)
            self._lazy_loaded = group
        return group

    def make_context(self, *args: Any, **kwargs: Any) -> click.Context:
        group = self._load()
        if group is not self:
            return group.make_context(*args, **kwargs)
        return super().make_context(*args, **kwargs)

    def shell_complete(self, *args: Any, **kwargs: Any) -> list[Any]:
        group = self._load()
        if group is not self:
            return group.shell_complete(*args, **kwargs)
        return super().shell_complete(*args, **kwargs)


_COMMAND_PACKAGE = "cruxible_core.cli.commands"


def _command(module: str, attr: str, help: str) -> LazyCommandSpec:
    return LazyCommandSpec(f"{_COMMAND_PACKAGE}.{module}", attr, help)


def _group(
    help: str,
    commands: dict[str, LazyCommandSpec],
    *,
    module: str | None = None,
    attr: str | None = None,
) -> LazyCommandSpec:
    module_path = f"{_COMMAND_PACKAGE}.{module}" if module is not None else None
    return LazyCommandSpec(module_path, attr, help, commands)


# The registry is the authoritative import-free command inventory. Command
# names and first-paragraph help live here so top-level help and completion can
# enumerate the full surface without importing any domain command module.
CLI_COMMANDS: dict[str, LazyCommandSpec] = {
    "init": _command(
        "workflows",
        "init",
        "Initialize a new instance or governed server-backed workspace.",
    ),
    "validate": _command(
        "workflows",
        "validate",
        "Validate a config YAML file without creating an instance.",
    ),
    "context": _group(
        "Manage remembered governed server and instance context.",
        {
            "show": _command("context", "context_show", "Show the remembered CLI context."),
            "connect": _command(
                "context",
                "context_connect",
                "Persist the current governed transport and optional instance.",
            ),
            "use": _command("context", "context_use", "Set the active governed instance ID."),
            "clear": _command("context", "context_clear", "Clear remembered governed CLI context."),
        },
        module="context",
        attr="connect_group",
    ),
    "decision-record": _group(
        "Manage decision records and their logged receipts.",
        {
            "create": _command("decision_records", "create_cmd", "Create an open decision record."),
            "get": _command("decision_records", "get_cmd", "Fetch one decision record."),
            "list": _command("decision_records", "list_cmd", "List decision records."),
            "events": _command("decision_records", "events_cmd", "List decision-record events."),
            "finalize": _command(
                "decision_records", "finalize_cmd", "Finalize an open decision record."
            ),
            "abandon": _command(
                "decision_records", "abandon_cmd", "Abandon an open decision record."
            ),
        },
        module="decision_records",
        attr="decision_records_cmd",
    ),
    "credential": _group(
        "Manage runtime bearer credentials for a governed server instance.",
        {
            "claim-bootstrap": _command(
                "credentials",
                "claim_bootstrap_cmd",
                "Exchange the one-time bootstrap secret for the first ADMIN runtime token.",
            ),
            "mint": _command("credentials", "mint_cmd", "Mint a new runtime bearer credential."),
            "list": _command(
                "credentials",
                "list_cmd",
                "List runtime bearer credentials for the active instance.",
            ),
            "recover-admin": _command(
                "credentials",
                "recover_admin_cmd",
                "Recover an ADMIN token by local filesystem ownership of server state.",
            ),
            "revoke": _command("credentials", "revoke_cmd", "Revoke a runtime bearer credential."),
            "rotate": _command(
                "credentials",
                "rotate_cmd",
                "Rotate a runtime bearer credential and print the replacement token once.",
            ),
        },
        module="credentials",
        attr="credential_group",
    ),
    "lock": _command(
        "workflows",
        "lock_cmd",
        "Generate a workflow lock file for the current instance config.",
    ),
    "state": _group(
        "Publish immutable states and manage pullable overlays.",
        {
            "publish": _command(
                "state",
                "state_publish_cmd",
                "Publish the current root state instance as an immutable release bundle.",
            ),
            "create-overlay": _command(
                "state",
                "create_state_overlay_cmd",
                "Create a new local overlay instance from a published state release.",
            ),
            "status": _command(
                "state",
                "state_status_cmd",
                "Show upstream tracking metadata for the current instance.",
            ),
            "health": _command(
                "state",
                "state_health_cmd",
                "Show read-only deterministic state-health maintenance signals.",
            ),
            "pull-preview": _command(
                "state",
                "state_pull_preview_cmd",
                "Preview pulling a newer upstream release into the current overlay.",
            ),
            "pull-apply": _command(
                "state",
                "state_pull_apply_cmd",
                "Apply a previewed upstream release into the current overlay.",
            ),
        },
        module="state",
        attr="state_group",
    ),
    "instance": _group(
        "Back up and restore exact Cruxible instances.",
        {
            "backup": _command(
                "instances",
                "instance_backup_cmd",
                "Write a portable same-identity backup artifact for the current instance.",
            ),
            "restore": _command(
                "instances", "instance_restore_cmd", "Restore a same-identity backup artifact."
            ),
            "relocate": _command(
                "instances",
                "instance_relocate_cmd",
                "Move the current healthy instance to a new directory, preserving identity.",
            ),
        },
        module="instances",
        attr="instance_group",
    ),
    "plan": _command("workflows", "plan_cmd", "Compile a workflow plan for the current instance."),
    "run": _command("workflows", "run_cmd", "Execute a workflow for the current instance."),
    "apply": _command(
        "workflows",
        "apply_cmd",
        "Apply a canonical workflow after verifying preview identity.",
    ),
    "test": _command(
        "workflows",
        "test_cmd",
        "Execute config-defined workflow tests for the current instance.",
    ),
    "propose": _command(
        "workflows",
        "propose_cmd",
        "Execute a workflow and bridge its output into a candidate group.",
    ),
    "snapshot": _group(
        "Manage immutable state snapshots.",
        {
            "create": _command(
                "workflows",
                "snapshot_create_cmd",
                "Create an immutable full snapshot for the current instance.",
            ),
            "list": _command(
                "workflows", "snapshot_list_cmd", "List snapshots for the current instance."
            ),
        },
        module="workflows",
        attr="snapshot_group",
    ),
    "source": _group(
        "Register and dereference source-backed evidence.",
        {
            "list": _command(
                "source_artifacts", "list_source_artifacts", "List registered source artifacts."
            ),
            "get": _command(
                "source_artifacts",
                "get_source_artifact",
                "Read source artifact metadata and chunk summaries.",
            ),
            "register": _command(
                "source_artifacts",
                "register_source_artifact",
                "Register a source artifact for proposal evidence.",
            ),
            "dereference": _command(
                "source_artifacts",
                "dereference_source_evidence",
                "Return source text for a registered source-evidence locator.",
            ),
        },
        module="source_artifacts",
        attr="source_group",
    ),
    "clone": _command(
        "workflows", "clone_cmd", "Create a new local instance from a chosen snapshot."
    ),
    "query": _group(
        "Run, inspect, and discover named queries on this instance.",
        {
            "run": _command(
                "reads", "query_run", "Execute a named query and display results plus the receipt."
            ),
            "inline": _command(
                "reads",
                "query_inline_cmd",
                "Execute a bounded inline query without persisting it to config.",
            ),
            "list": _command("reads", "query_list_cmd", "List named queries as bounded summaries."),
            "describe": _command(
                "reads",
                "query_describe_cmd",
                "Describe one named query with required params and example IDs.",
            ),
        },
        module="reads",
        attr="query",
    ),
    "server": _group(
        "Launch and inspect the Cruxible daemon.",
        {
            "start": _command(
                "server", "server_start_cmd", "Launch the Cruxible daemon in the foreground."
            ),
            "status": _command(
                "server",
                "server_status_cmd",
                "Report a running daemon's version, state dir, transport, and instances.",
            ),
            "info": _command(
                "server",
                "server_info_cmd",
                "Show live daemon metadata such as transport policy and state dir.",
            ),
            "restart": _command(
                "server",
                "server_restart_cmd",
                "Re-exec the live daemon in place, preserving its port, state dir, and env.",
            ),
        },
        module="server",
        attr="server_group",
    ),
    "explain": _command("reads", "explain", "Explain a query result using its receipt."),
    "list": _group(
        "List entities, receipts, or feedback.",
        {
            "entities": _command("lists", "list_entities", "List entities of a given type."),
            "receipts": _command("lists", "list_receipts", "List receipt summaries."),
            "traces": _command("lists", "list_traces", "List provider execution trace summaries."),
            "feedback": _command("lists", "list_feedback", "List feedback records."),
            "outcomes": _command("lists", "list_outcomes", "List outcome records."),
            "edges": _command("lists", "list_edges", "List edges in the graph."),
        },
        module="lists",
        attr="list_group",
    ),
    "schema": _command("reads", "schema", "Display the config schema for this instance."),
    "stats": _command(
        "read_stats", "stats_cmd", "Display entity and relationship counts for this instance."
    ),
    "sample": _command("reads", "sample", "Show a sample of entities of a given type."),
    "evaluate": _command(
        "reads",
        "evaluate",
        "Assess graph quality: orphans, gaps, violations, unreviewed co-members.",
    ),
    "lint": _command("reads", "lint_cmd", "Run the aggregate read-only corpus lint pass."),
    "inspect": _group(
        "Inspect entities plus canonical read-only system views.",
        {
            "ontology": _command(
                "reads",
                "inspect_ontology_cmd",
                "Show the canonical ontology view for the current instance config.",
            ),
            "workflows": _command(
                "reads",
                "inspect_workflows_cmd",
                "Show the canonical workflow view for the current instance config.",
            ),
            "queries": _command(
                "reads",
                "inspect_queries_cmd",
                "Show the canonical query view for the current instance config.",
            ),
            "governance": _command(
                "reads",
                "inspect_governance_cmd",
                "Show the canonical governance view for the current instance.",
            ),
            "overview": _command(
                "reads",
                "inspect_overview_cmd",
                "Show the generated config overview built from canonical views.",
            ),
            "trace": _command(
                "reads", "inspect_trace_cmd", "Inspect a provider execution trace by ID."
            ),
        },
        module="reads",
        attr="inspect_group",
    ),
    "gate": _group(
        "Evaluate declared repo gates against state.",
        {
            "list": _command("gates", "gate_list", "Show the active instance's declared gates."),
            "check": _command(
                "gates",
                "gate_check",
                "Evaluate gate NAME: is every candidate value pinned by satisfying state?",
            ),
        },
        module="gates",
        attr="gate_group",
    ),
    "ws": _group(
        "Agent-local working set: opt-in, NON-AUTHORITATIVE read cache.",
        {
            "path": _command(
                "working_set",
                "ws_path_cmd",
                "Print the records file path for the current context (for rg/jq).",
            ),
            "status": _command(
                "working_set",
                "ws_status_cmd",
                "Show record counts, file size, and cached-vs-current revision spread.",
            ),
            "verify": _command(
                "working_set",
                "ws_verify_cmd",
                "Verify cached records against the current instance read revision.",
            ),
            "refresh": _command(
                "working_set",
                "ws_refresh_cmd",
                "Re-fetch stale/unknown records; drop deleted ones; leave fresh untouched.",
            ),
            "clear": _command(
                "working_set",
                "ws_clear_cmd",
                "Delete the current context's records file (working-set dir only).",
            ),
        },
        module="working_set",
        attr="ws_group",
    ),
    "config": _group(
        "Edit, validate, and render the active config.",
        {
            "reload": _command(
                "mutations",
                "reload_config_cmd",
                "Validate the active config or repoint the instance to a new config file.",
            ),
            "status": _command(
                "mutations",
                "config_status_cmd",
                "Report source drift and active materialized-config integrity.",
            ),
            "views": _command(
                "config_views",
                "config_views_cmd",
                "Render canonical Mermaid/Markdown views for a Cruxible config.",
            ),
            "expand": _command(
                "config_views",
                "config_expand_cmd",
                "Expand a compact authoring config to the explicit engine config.",
            ),
            "add-constraint": _command(
                "mutations", "add_constraint_cmd", "Add a constraint rule to the config."
            ),
            "add-decision-policy": _command(
                "mutations",
                "add_decision_policy_cmd",
                "Add a decision policy to the config.",
            ),
        },
    ),
    "feedback": _group(
        "Record, batch, analyze, and inspect edge feedback.",
        {
            "record": _command(
                "feedback",
                "feedback_cmd",
                "Submit feedback on a specific edge by explicit relationship coordinates.",
            ),
            "from-query": _command(
                "feedback",
                "feedback_from_query_cmd",
                "Submit edge feedback by selecting relationship evidence from a query receipt.",
            ),
            "batch": _command(
                "feedback",
                "feedback_batch_cmd",
                "Submit a batch of edge feedback with one top-level receipt.",
            ),
            "profile": _command(
                "feedback",
                "feedback_profile_cmd",
                "Display the configured feedback profile for one relationship type.",
            ),
            "analyze": _command(
                "reads",
                "analyze_feedback_cmd",
                "Analyze structured feedback and print remediation suggestions.",
            ),
        },
        module="feedback",
        attr="feedback_group",
    ),
    "outcome": _group(
        "Record, analyze, and inspect decision outcomes.",
        {
            "record": _command("feedback", "outcome_cmd", "Record the outcome of a decision."),
            "profile": _command(
                "feedback",
                "outcome_profile_cmd",
                "Display the configured outcome profile for one anchor context.",
            ),
            "analyze": _command(
                "reads",
                "analyze_outcomes_cmd",
                "Analyze structured outcomes and print trust/debugging suggestions.",
            ),
        },
        module="feedback",
        attr="outcome_group",
    ),
    "entity": _group(
        "Entity reads and writes.",
        {
            "add": _command(
                "mutations",
                "add_entity_cmd",
                "Create one entity using JSON properties or FIELD=VALUE assignments.",
            ),
            "update": _command(
                "mutations",
                "update_entity_cmd",
                "Update one existing entity's properties and/or lifecycle state.",
            ),
            "get": _command("reads", "get_entity_cmd", "Look up a specific entity by type and ID."),
            "inspect": _command(
                "reads", "inspect_entity_cmd", "Inspect an entity and its bounded neighborhood."
            ),
            "history": _command(
                "reads",
                "inspect_entity_history_cmd",
                "Inspect receipt-derived entity change history for one entity type or entity.",
            ),
        },
    ),
    "relationship": _group(
        "Relationship reads and writes.",
        {
            "add": _command(
                "mutations",
                "add_relationship_cmd",
                "Add one relationship using FIELD=VALUE property assignments.",
            ),
            "update": _command(
                "mutations",
                "update_relationship_cmd",
                "Update one existing relationship's properties, evidence, or lifecycle.",
            ),
            "get": _command(
                "reads",
                "get_relationship_cmd",
                "Look up a specific relationship by its endpoints and type.",
            ),
            "lineage": _command(
                "reads",
                "inspect_relationship_lineage_cmd",
                "Inspect a relationship's stored provenance lineage.",
            ),
        },
    ),
    "batch-direct-write": _command(
        "mutations",
        "batch_direct_write_cmd",
        "Validate or apply a direct batch graph write payload.",
    ),
    "export": _group(
        "Export graph data to files.",
        {"edges": _command("lists", "export_edges", "Export all edges to CSV.")},
        module="lists",
        attr="export_group",
    ),
    "group": _group(
        "Manage candidate groups for batch edge review.",
        {
            "propose": _command(
                "groups", "group_propose", "Propose a candidate group of edges for batch review."
            ),
            "resolve": _command(
                "groups", "group_resolve", "Resolve a candidate group (approve or reject)."
            ),
            "trust": _command("groups", "group_trust", "Update trust status on a resolution."),
            "get": _command("groups", "group_get", "Get details of a candidate group."),
            "list": _command("groups", "group_list", "List candidate groups."),
            "resolutions": _command("groups", "group_resolutions", "List group resolutions."),
            "status": _command(
                "groups", "group_status", "Show lifecycle status for a signature bucket."
            ),
        },
        module="groups",
        attr="group_group",
    ),
}


@click.group(cls=LazyGroup, lazy_subcommands=CLI_COMMANDS)
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
@click.option(
    "--json-compact",
    is_flag=True,
    default=None,
    help="Emit all CLI JSON as compact single-line output (also CRUXIBLE_JSON_COMPACT=1).",
)
@click.pass_context
def cli(
    ctx: click.Context,
    server_url: str | None,
    server_socket: str | None,
    instance_id: str | None,
    json_compact: bool | None,
) -> None:
    """Cruxible — hard state for AI agents: governed, queryable, durable, with receipts."""
    try:
        stored = load_cli_context()
        env_server_url = os.environ.get("CRUXIBLE_SERVER_URL")
        env_server_socket = os.environ.get("CRUXIBLE_SERVER_SOCKET")
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
            "json_compact": json_compact,
            "target_transport_source": _target_source(
                explicit=server_url is not None or server_socket is not None,
                environment=env_server_url is not None or env_server_socket is not None,
                remembered=stored.server_url is not None or stored.server_socket is not None,
            ),
            "target_instance_source": _target_source(
                explicit=instance_id is not None,
                environment=False,
                remembered=stored.instance_id is not None,
            ),
        }
    )
