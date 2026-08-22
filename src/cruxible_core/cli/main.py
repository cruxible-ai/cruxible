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
# - kit: writes metadata for the explicitly selected local materialized kit.
# - manual: the command resolves its target from command-specific inputs and
#   emits the notice itself immediately before the write.
MUTATING_COMMAND_TARGETS: dict[tuple[str, ...], str] = {
    ("playbill", "host", "create"): "create",
    ("playbill", "init"): "active",
    ("playbill", "body", "store"): "active",
    ("playbill", "document", "propose"): "active",
    ("playbill", "subject", "propose"): "active",
    ("playbill", "claim-type", "propose"): "active",
    ("playbill", "claim", "propose"): "active",
    ("playbill", "claim", "propose-batch"): "active",
    ("playbill", "authoring", "create"): "manual",
    ("playbill", "authoring", "bind"): "active",
    ("playbill", "authoring", "compile"): "active",
    ("playbill", "authoring", "preflight"): "active",
    ("playbill", "authoring", "submit"): "active",
    # Compile is two acts behind one command: --preview writes nothing at all,
    # so the notice belongs immediately before the submit rather than before
    # the command that might only be previewing.
    ("playbill", "native", "compile"): "manual",
    # Seeding is the same shape: --plan is an offline reading of a directory
    # that reaches no instance, so the notice belongs before the submit.
    ("playbill", "seed", "apply"): "manual",
    ("playbill", "query", "propose"): "active",
    ("playbill", "proposal", "approve"): "active",
    ("playbill", "proposal", "activate"): "active",
    ("playbill", "proposal", "readmit"): "active",
    ("playbill", "sources", "propose"): "active",
    ("playbill", "principal", "rotate"): "active",
    ("playbill", "principal", "recover"): "active",
    ("playbill", "principal", "revoke"): "active",
    ("credential", "claim-bootstrap"): "active",
    ("credential", "mint"): "active",
    ("credential", "recover-admin"): "manual",
    ("credential", "revoke"): "active",
    ("credential", "rotate"): "active",
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


LONG_RUNNING_MARKER = "_cruxible_long_running"


def long_running_command(f: Any) -> Any:
    """Document that a callback owns the process for its lifetime."""
    setattr(f, LONG_RUNNING_MARKER, True)
    return f


def handle_errors(f: Any) -> Any:
    """Decorator that catches any Cruxible error and prints a friendly message.

    Core errors subclass the client base, so the client hierarchy is the
    single catch surface for local and remote failures.
    """

    def run_with_error_handling(*args: Any, **kwargs: Any) -> Any:
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

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return run_with_error_handling(*args, **kwargs)

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
    "playbill": _group(
        "Govern state through Playbill's proposal and acceptance ledger.",
        {
            "host": _group(
                "Allocate daemon-owned Playbill hosts.",
                {
                    "create": _command(
                        "playbill", "create_host", "Allocate an empty host for Playbill."
                    )
                },
                module="playbill",
                attr="host_group",
            ),
            "init": _command("playbill", "init_playbill", "Bootstrap Playbill state."),
            "whoami": _command(
                "playbill", "whoami", "Explain the active writer identity and permissions."
            ),
            "body": _group(
                "Store inert Document body bytes.",
                {
                    "store": _command(
                        "playbill", "store_body", "Store exact bytes without creating authority."
                    )
                },
                module="playbill",
                attr="body_group",
            ),
            "document": _group(
                "Propose and read governed Documents.",
                {
                    "propose": _command(
                        "playbill", "propose_document", "Propose a Document envelope."
                    ),
                    "list": _command("playbill", "list_documents", "List accepted Documents."),
                    "get": _command("playbill", "get_document", "Read an accepted Document."),
                    "body": _command(
                        "playbill", "get_document_body", "Dereference verified body bytes."
                    ),
                    "history": _command(
                        "playbill", "document_history", "Read accepted Document history."
                    ),
                },
                module="playbill",
                attr="document_group",
            ),
            "proposal": _group(
                "Inspect, review, approve, and activate candidates.",
                {
                    "list": _command(
                        "playbill", "list_proposals", "List open or settled proposals."
                    ),
                    "readmit": _command(
                        "playbill",
                        "readmit_proposal",
                        "Re-admit one stale proposal at the current head.",
                    ),
                    "inspect": _command(
                        "playbill", "inspect_proposal", "Inspect immutable proposal evidence."
                    ),
                    "refusal": _command(
                        "playbill", "inspect_refusal", "Inspect typed refusal diagnostics."
                    ),
                    "review": _command(
                        "playbill", "review_proposal", "Render structured candidate review."
                    ),
                    "approve": _command(
                        "playbill", "approve_proposal", "Sign locally and submit an attestation."
                    ),
                    "activate": _command(
                        "playbill", "activate_proposal", "Settle an approved candidate."
                    ),
                },
                module="playbill",
                attr="proposal_group",
            ),
            "subject": _group(
                "Propose and read identity-only governed Subjects.",
                {
                    "propose": _command("playbill", "propose_subject", "Propose a Subject shell."),
                    "list": _command("playbill", "list_subjects", "List accepted Subjects."),
                    "get": _command("playbill", "get_subject", "Read an accepted Subject."),
                    "history": _command(
                        "playbill", "subject_history", "Read accepted Subject history."
                    ),
                },
                module="playbill",
                attr="subject_group",
            ),
            "claim-type": _group(
                "Propose and read the governed predicate vocabulary.",
                {
                    "propose": _command(
                        "playbill", "propose_claim_type", "Propose a ClaimType interface."
                    ),
                    "list": _command("playbill", "list_claim_types", "List accepted ClaimTypes."),
                    "get": _command("playbill", "get_claim_type", "Read one accepted ClaimType."),
                },
                module="playbill",
                attr="claim_type_group",
            ),
            "claim": _group(
                "Propose, read, and explain first-class Claims.",
                {
                    "propose": _command(
                        "playbill", "propose_claim", "Propose a direct Claim and its Capture."
                    ),
                    "propose-batch": _command(
                        "playbill",
                        "propose_claims",
                        "Propose several Claims as one change set.",
                    ),
                    "list": _command("playbill", "list_claims", "List accepted Claims."),
                    "get": _command("playbill", "get_claim", "Read an accepted Claim."),
                    "history": _command(
                        "playbill", "claim_history", "Read accepted Claim history."
                    ),
                    "explain": _command(
                        "playbill", "explain_claim", "Explain one Claim's verdict and evidence."
                    ),
                },
                module="playbill",
                attr="claim_group",
            ),
            "authoring": _group(
                "Author, preflight, submit, and resume governed writes.",
                {
                    "create": _command(
                        "playbill", "create_authoring_intent", "Create a durable intent."
                    ),
                    "get": _command(
                        "playbill", "get_authoring_intent", "Read one authoring intent."
                    ),
                    "resume": _command(
                        "playbill", "resume_authoring_intent", "Resume durable authoring."
                    ),
                    "list": _command(
                        "playbill",
                        "list_pending_authoring_intents",
                        "List pending authoring intents.",
                    ),
                    "compile": _command(
                        "playbill", "compile_authoring", "Author and preflight a payload."
                    ),
                    "bind": _command(
                        "playbill",
                        "bind_authoring_selection",
                        "Bind a Flow-A selection and compile it.",
                    ),
                    "preflight": _command(
                        "playbill",
                        "preflight_authoring_intent",
                        "Recheck an authoring intent.",
                    ),
                    "submit": _command(
                        "playbill", "submit_authoring_intent", "Submit a passing intent."
                    ),
                    "status": _command(
                        "playbill",
                        "authoring_intent_status",
                        "Read the path to acceptance.",
                    ),
                    "confirm-insertion": _command(
                        "playbill",
                        "confirm_authoring_insertion",
                        "Confirm a client-applied publication copy.",
                    ),
                    "abandon-insertion": _command(
                        "playbill",
                        "abandon_authoring_insertion",
                        "Abandon a pending publication copy.",
                    ),
                },
                module="playbill",
                attr="authoring_group",
            ),
            "query": _group(
                "Propose, read, and execute governed named entrypoints.",
                {
                    "propose": _command(
                        "playbill", "propose_query_definition", "Propose a QueryDefinition."
                    ),
                    "list": _command(
                        "playbill", "list_query_definitions", "List accepted entrypoints."
                    ),
                    "get": _command(
                        "playbill", "get_query_definition", "Read one accepted entrypoint."
                    ),
                    "run": _command(
                        "playbill", "run_query", "Execute an entrypoint with a replay receipt."
                    ),
                },
                module="playbill",
                attr="query_group",
            ),
            "discover": _command(
                "playbill", "discover", "Find accepted interfaces and Subjects by name."
            ),
            "search": _command(
                "playbill", "search", "Search accepted Claims, Briefs, and Procedures."
            ),
            "list": _command(
                "playbill", "search_list", "List accepted state in deterministic pages."
            ),
            "orient": _command(
                "playbill", "orient", "Summarize accepted state and exact follow-up filters."
            ),
            "expand": _command(
                "playbill", "expand", "Expand one address into a bounded context capsule."
            ),
            "seed": _group(
                "Apply a bundle of authoring JSONs as governed proposals.",
                {
                    "apply": _command(
                        "playbill", "apply_seed", "Propose one group of a seed bundle."
                    )
                },
                module="playbill",
                attr="seed_group",
            ),
            "floor": _group(
                "Materialize the deterministic greppable floor.",
                {
                    "export": _command(
                        "playbill", "export_floor", "Write the accepted floor to a directory."
                    )
                },
                module="playbill",
                attr="floor_group",
            ),
            "coverage": _group(
                "Deliver what working files have to do with accepted state.",
                {
                    "resolve": _command(
                        "playbill", "resolve_coverage", "Resolve coverage for working sources."
                    ),
                    "status": _command(
                        "playbill", "coverage_status", "Render the coverage manifest."
                    ),
                },
                module="playbill",
                attr="coverage_group",
            ),
            "hook": _group(
                "Deliver coverage into a harness's own tool results.",
                {
                    "post-tool-use": _command(
                        "playbill",
                        "post_tool_use_hook",
                        "Annotate a Claude Code tool result with coverage.",
                    ),
                },
                module="playbill",
                attr="hook_group",
            ),
            "explain": _command(
                "playbill", "explain", "Explain governance at an accepted coordinate."
            ),
            "sources": _group(
                "Compile declared local files into exact-byte bundles.",
                {
                    "compile": _command(
                        "playbill", "compile_sources", "Compile a read-only frozen bundle."
                    ),
                    "check": _command(
                        "playbill", "check_sources", "Compare local bytes with accepted state."
                    ),
                    "propose": _command(
                        "playbill", "propose_sources", "Propose one exact compiled source."
                    ),
                },
                module="playbill",
                attr="sources_group",
            ),
            "native": _group(
                "Check out accepted knowledge as an editable in-repo working tree.",
                {
                    "render": _command(
                        "playbill", "render_native", "Render accepted knowledge as Markdown."
                    ),
                    "status": _command(
                        "playbill",
                        "native_status_cmd",
                        "Report clean, edited, and tampered fields.",
                    ),
                    "compile": _command(
                        "playbill",
                        "compile_native",
                        "Compile local edits into one governed proposal.",
                    ),
                    "review-current": _command(
                        "playbill",
                        "native_review_current",
                        "Check that review evidence binds the current candidate.",
                    ),
                    "stash": _group(
                        "Keep and restore local edits a re-render would overwrite.",
                        {
                            "list": _command(
                                "playbill", "native_stash_list", "List stashed local edits."
                            ),
                            "show": _command(
                                "playbill", "native_stash_show", "Show one stashed local edit."
                            ),
                            "restore": _command(
                                "playbill",
                                "native_stash_restore",
                                "Re-apply stashed edits by region identity.",
                            ),
                        },
                        module="playbill",
                        attr="native_stash_group",
                    ),
                },
                module="playbill",
                attr="native_group",
            ),
            "principal": _group(
                "Govern owner, reviewer, and recovery public keys.",
                {
                    "list": _command(
                        "playbill", "list_principals", "List accepted principal keys."
                    ),
                    "rotate": _command(
                        "playbill", "rotate_principal", "Self-rotate a principal key."
                    ),
                    "recover": _command(
                        "playbill", "recover_principal", "Recover a principal key narrowly."
                    ),
                    "revoke": _command(
                        "playbill", "revoke_principal", "Propose principal revocation."
                    ),
                },
                module="playbill",
                attr="principal_group",
            ),
        },
        module="playbill",
        attr="playbill_group",
    ),
    "context": _group(
        "Manage remembered daemon and instance context.",
        {
            "show": _command("context", "context_show", "Show remembered CLI context."),
            "connect": _command("context", "context_connect", "Persist daemon context."),
            "use": _command("context", "context_use", "Set the active instance ID."),
            "clear": _command("context", "context_clear", "Clear remembered context."),
        },
        module="context",
        attr="connect_group",
    ),
    "credential": _group(
        "Manage daemon transport credentials.",
        {
            "claim-bootstrap": _command(
                "credentials", "claim_bootstrap_cmd", "Claim the initial ADMIN token."
            ),
            "mint": _command("credentials", "mint_cmd", "Mint a runtime credential."),
            "list": _command("credentials", "list_cmd", "List runtime credentials."),
            "recover-admin": _command(
                "credentials", "recover_admin_cmd", "Recover ADMIN from local custody."
            ),
            "revoke": _command("credentials", "revoke_cmd", "Revoke a runtime credential."),
            "rotate": _command("credentials", "rotate_cmd", "Rotate a runtime credential."),
        },
        module="credentials",
        attr="credential_group",
    ),
    "server": _group(
        "Launch and inspect the Cruxible daemon.",
        {
            "start": _command("server", "server_start_cmd", "Launch the daemon in the foreground."),
            "status": _command("server", "server_status_cmd", "Report daemon status."),
            "info": _command("server", "server_info_cmd", "Show daemon metadata."),
            "restart": _command("server", "server_restart_cmd", "Re-exec the daemon in place."),
        },
        module="server",
        attr="server_group",
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
