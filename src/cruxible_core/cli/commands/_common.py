"""Shared dispatch, parsing, and formatting helpers for CLI commands."""

from __future__ import annotations

import json as _json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import click

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.context import (
    CliContextState,
    clear_cli_context,
    load_cli_context,
    save_cli_context,
)
from cruxible_core.errors import ConfigError, InstanceNotFoundError
from cruxible_core.server.config import get_runtime_bearer_token

if TYPE_CHECKING:
    from rich.console import Console

    from cruxible_core.cli.instance import CruxibleInstance
    from cruxible_core.config.schema import CoreConfig
    from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
    from cruxible_core.graph.types import EntityInstance
    from cruxible_core.group.types import CandidateGroup, CandidateMember
    from cruxible_core.query.continuation import ContinuationSurface, ContinuationToken
    from cruxible_core.service import OperationContext


class _LazyConsole:
    """Delay Rich's rendering stack until a command emits a table."""

    def __init__(self) -> None:
        self._console: Console | None = None

    def __getattr__(self, name: str) -> Any:
        console = self._console
        if console is None:
            from rich.console import Console

            console = Console()
            self._console = console
        return getattr(console, name)


console = _LazyConsole()
LocalResultT = TypeVar("LocalResultT")
RemoteResultT = TypeVar("RemoteResultT")

# Single source of truth for the "server mode required" message so every command
# that refuses a local fallback surfaces identical wording and remediation.
SERVER_MODE_REQUIRED_MESSAGE = (
    "Server mode is required. Set CRUXIBLE_SERVER_SOCKET or CRUXIBLE_SERVER_URL."
)

json_option = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)

decision_record_option = click.option(
    "--decision-record",
    "decision_record_id",
    default=None,
    help="Decision record ID for audit logging.",
)

# Opt-in agent-local working-set capture for JSON read commands. Capture is a
# pure side effect after the payload is printed (stdout is never changed); it
# can also be enabled globally with CRUXIBLE_WORKING_SET=1. See
# ``cruxible_core.cli.working_set`` and the ``cruxible ws`` group.
ws_option = click.option(
    "--ws",
    "ws_capture",
    is_flag=True,
    default=False,
    help=(
        "Also capture this --json read into the agent-local working set "
        "(non-authoritative cache; see 'cruxible ws')."
    ),
)

# Output profile for entity-shaped read payloads. ``standard`` (default) is
# today's full shape; ``compact`` trims JSON items to bounded identity cards
# that keep governance markers (lifecycle / review status) but drop
# actor_context and provenance blobs; ``full`` is reserved as a superset of
# standard. The profile is applied at JSON-emit time through the shared
# serializer (cruxible_core.query.profiles) in BOTH local and server mode, so
# the two modes cannot drift. Table output is already bounded and unaffected.
profile_option = click.option(
    "--profile",
    "profile",
    type=click.Choice(["compact", "standard", "full"]),
    default="standard",
    show_default=True,
    help=(
        "JSON output profile: compact (bounded identity cards with governance "
        "markers), standard (full shape), or full (reserved superset of standard)."
    ),
)

# Transport layout for query output. ``rows`` (default) is today's per-row
# item layout, bit-for-bit; ``graph`` normalizes the already-filtered,
# already-profiled rows into nodes/edges serialized once each, with results
# as ordered references and paths as step-ref (edge index + alias)
# sequences. Applied at
# JSON-emit time through the shared normalizer
# (cruxible_core.query.graph_layout) in BOTH local and server mode, so the
# two modes cannot drift. Orthogonal to --profile (detail level) and the
# query's result_shape (semantic unit).
layout_option = click.option(
    "--layout",
    "layout",
    type=click.Choice(["rows", "graph"]),
    default="rows",
    show_default=True,
    help=(
        "Query output layout: rows (per-row items) or graph (normalized "
        "nodes/edges with results as ordered references; each entity and "
        "relationship serialized once)."
    ),
)

# Unified read-visibility selector. Gates entities by lifecycle and edges by
# review+lifecycle through the same engine filter, so a single flag controls
# every read surface. ``live`` is the implicit default (None => server/service
# default). ``not-live`` and ``all`` are the audit/recovery views.
state_option = click.option(
    "--state",
    "state",
    type=click.Choice(["live", "accepted", "all", "not-live", "pending", "reviewable"]),
    default=None,
    help=(
        "Read-visibility state: live (default), accepted, all, not-live, pending, or reviewable."
    ),
)


# Continuation token option shared by resumable read commands. Tokens are
# opaque, bound to the instance/config/read_revision/filter set; replay after
# any mutation fails with a typed stale-continuation error (restart the read).
continuation_option = click.option(
    "--continue",
    "continuation",
    default=None,
    metavar="TOKEN",
    help=(
        "Continuation token from a previous truncated page; repeat the same "
        "filters. Stale after any state mutation - restart the read."
    ),
)


def _local_continuation_binding(
    instance: CruxibleInstance, filters: dict[str, Any]
) -> dict[str, Any]:
    """Token binding for CLI local mode (no daemon instance id available).

    Local tokens use the resolved instance root as the instance key, so they
    are valid only for local-mode reads of the same workspace; daemon-minted
    tokens are bound to the daemon instance id instead and are rejected here.
    """
    from cruxible_core.query.continuation import compute_filter_hash
    from cruxible_core.workflow.compiler import compute_lock_config_digest

    return {
        "instance_key": f"local:{Path(instance.get_root_path()).resolve()}",
        "config_digest": compute_lock_config_digest(instance.load_config()),
        "read_revision": instance.get_read_revision(),
        "filter_hash": compute_filter_hash(filters),
    }


def _local_accept_continuation(
    instance: CruxibleInstance,
    *,
    surface: ContinuationSurface,
    filters: dict[str, Any],
    continuation: str | None,
) -> ContinuationToken | None:
    from cruxible_core.query.continuation import (
        decode_continuation_token,
        validate_continuation_token,
    )

    if continuation is None:
        return None
    token = decode_continuation_token(continuation)
    binding = _local_continuation_binding(instance, filters)
    validate_continuation_token(token, surface=surface, **binding)
    return token


def _local_mint_continuation(
    instance: CruxibleInstance,
    *,
    surface: ContinuationSurface,
    filters: dict[str, Any],
    cursor: dict[str, int],
) -> str:
    from cruxible_core.query.continuation import mint_continuation_token

    return mint_continuation_token(
        surface=surface, cursor=cursor, **_local_continuation_binding(instance, filters)
    )


def _echo_continuation_hint(continuation_token: str | None) -> None:
    """Print the resume hint for truncated table output."""
    if continuation_token:
        click.echo(f"Truncated. Continue with: --continue {continuation_token}")


def _json_compact_enabled() -> bool:
    """Resolve compact JSON at emit time; an explicit root flag wins over env."""
    context_value = _root_ctx_obj().get("json_compact")
    if context_value is not None:
        return bool(context_value)
    return os.environ.get("CRUXIBLE_JSON_COMPACT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emit_json(data: Any, *, sort_keys: bool = False) -> None:
    """Emit structured JSON to stdout, bypassing Rich."""
    if _json_compact_enabled():
        from cruxible_core.primitives import compact_json

        click.echo(compact_json(data, default=str, sort_keys=sort_keys))
        return
    click.echo(_json.dumps(data, indent=2, sort_keys=sort_keys, default=str))


def _list_envelope(
    result: Any, *, item_count: int, limit: int | None, offset: int
) -> dict[str, Any]:
    """Build the standard list envelope (total/limit/offset/truncated/read_revision).

    Server mode hands back a contract model that already carries the envelope;
    local mode now gets it from the service ``ListResult`` (which owns
    truncation/read_revision), so both branches consume rather than re-derive.
    The synthesized fallback remains only for service results that predate the
    envelope.
    """
    total = result.total
    if all(hasattr(result, name) for name in ("limit", "offset", "truncated")):
        return {
            "total": total,
            "limit": result.limit,
            "offset": result.offset,
            "truncated": result.truncated,
            "read_revision": getattr(result, "read_revision", None),
        }
    from cruxible_core.service.types import list_truncated

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": list_truncated(total=total, offset=offset, returned=item_count),
        "read_revision": getattr(result, "read_revision", None),
    }


def _resolve_decision_record_id(decision_record_id: str | None) -> str | None:
    return decision_record_id


@dataclass(frozen=True)
class ActiveInstanceChange:
    """Result of updating the remembered active instance."""

    previous: str | None
    current: str


def _operation_context(decision_record_id: str | None) -> OperationContext | None:
    from cruxible_core.service import OperationContext

    resolved = _resolve_decision_record_id(decision_record_id)
    if resolved is None:
        return None
    return OperationContext(decision_record_id=resolved, surface="cli")


def _root_ctx_obj() -> dict[str, Any]:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}
    root = ctx.find_root()
    root.ensure_object(dict)
    return cast(dict[str, Any], root.obj)


def _transport_target(obj: Mapping[str, Any]) -> str | None:
    server_url = obj.get("server_url")
    if server_url:
        return str(server_url)
    server_socket = obj.get("server_socket")
    if server_socket:
        return f"unix://{Path(str(server_socket)).expanduser().resolve()}"
    return None


def _target_source_qualifier(instance_source: str, transport_source: str) -> str:
    if instance_source == transport_source:
        return instance_source
    return f"instance={instance_source}, transport={transport_source}"


def _echo_active_write_target() -> None:
    """Emit the selected instance target once, without changing stdout."""
    obj = _root_ctx_obj()
    transport = _transport_target(obj)
    if transport is not None:
        instance_id = obj.get("instance_id")
        if not instance_id:
            # The command callback will produce the established usage error.
            # There is no actionable target to display yet.
            return
        qualifier = _target_source_qualifier(
            str(obj.get("target_instance_source") or "explicit"),
            str(obj.get("target_transport_source") or "explicit"),
        )
        click.echo(f"target: {instance_id} @ {transport} ({qualifier})", err=True)
        return

    from cruxible_core.cli.instance import CruxibleInstance

    try:
        instance = CruxibleInstance.load()
    except InstanceNotFoundError:
        # Preserve the command's own error when no local instance can resolve.
        return
    click.echo(
        f"target: local @ {instance.get_root_path().resolve()} (discovered)",
        err=True,
    )


def _echo_creation_write_target(params: Mapping[str, Any]) -> None:
    """Emit a destination for a command whose instance ID does not exist yet."""
    obj = _root_ctx_obj()
    transport = _transport_target(obj)
    command_name = click.get_current_context().command.name or "instance"
    target_label = "<restored instance>" if command_name == "restore" else "<new instance>"
    if transport is not None:
        transport_source = str(obj.get("target_transport_source") or "explicit")
        click.echo(
            f"target: {target_label} @ {transport} (transport={transport_source})",
            err=True,
        )
        return

    root_dir = params.get("root_dir")
    root = Path(str(root_dir)).expanduser().resolve() if root_dir else Path.cwd().resolve()
    source = "explicit" if root_dir else "discovered"
    click.echo(f"target: {target_label} @ {root} ({source})", err=True)


def _echo_write_target(mode: str, params: Mapping[str, Any]) -> None:
    """Central target notice used by the authoritative mutating-command inventory."""
    if mode == "active":
        _echo_active_write_target()
        return
    if mode == "create":
        _echo_creation_write_target(params)
        return
    if mode == "lock":
        kit_dir = params.get("kit_dir")
        if kit_dir is not None:
            click.echo(
                f"target: workflow lock @ {Path(str(kit_dir)).resolve()} (explicit)",
                err=True,
            )
        else:
            _echo_active_write_target()
        return
    raise AssertionError(f"Unknown write target mode: {mode}")


def _echo_explicit_write_target(instance_id: str, location: str | Path) -> None:
    """Emit a target resolved from command-specific explicit local inputs."""
    click.echo(
        f"target: {instance_id} @ {Path(location).expanduser().resolve()} (explicit)",
        err=True,
    )


def _get_client() -> CruxibleClient | None:
    obj = _root_ctx_obj()
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if not server_url and not server_socket:
        return None
    client = obj.get("_client")
    if isinstance(client, CruxibleClient):
        return client
    client = CruxibleClient(
        base_url=server_url,
        socket_path=server_socket,
        token=get_runtime_bearer_token(),
    )
    obj["_client"] = client
    return client


def _server_required() -> bool:
    """Return whether this invocation declared server mode as required.

    Resolved once at the root group (flags/env -> ``require_server``) and stashed
    on the click context. Reads consult it so they share the writes' transport
    contract: when the daemon is the declared backend, reads never silently fall
    back to a local on-disk instance.
    """
    return bool(_root_ctx_obj().get("require_server"))


def _current_cli_context() -> CliContextState:
    obj = _root_ctx_obj()
    return CliContextState(
        server_url=obj.get("server_url"),
        server_socket=obj.get("server_socket"),
        instance_id=obj.get("instance_id"),
    )


def _activate_server_instance(instance_id: str) -> ActiveInstanceChange | None:
    """Persist *instance_id* as the active server-mode CLI instance."""
    state = _current_cli_context()
    if not state.server_url and not state.server_socket:
        return None
    save_cli_context(
        CliContextState(
            server_url=state.server_url,
            server_socket=state.server_socket,
            instance_id=instance_id,
        )
    )
    _root_ctx_obj()["instance_id"] = instance_id
    return ActiveInstanceChange(previous=state.instance_id, current=instance_id)


def _print_active_instance_change(change: ActiveInstanceChange | None) -> None:
    """Print the active-instance update for a server-created instance."""
    if change is None:
        return
    click.echo(f"Active instance: {change.current}")
    if change.previous and change.previous != change.current:
        click.echo(f"Previous active instance: {change.previous}")


def _print_active_instance_unchanged() -> None:
    """Print the active instance when a server-created instance is not activated."""
    current = _current_cli_context().instance_id
    if current:
        click.echo(f"Active instance unchanged: {current}")
    else:
        click.echo("No active instance selected.")


def _persist_cli_context(
    *,
    server_url: str | None,
    server_socket: str | None,
    instance_id: str | None,
) -> None:
    save_cli_context(
        CliContextState(
            server_url=server_url,
            server_socket=server_socket,
            instance_id=instance_id,
        )
    )


def _clear_persisted_cli_context() -> None:
    clear_cli_context()


def _load_persisted_cli_context() -> CliContextState:
    return load_cli_context()


def _dispatch_cli(
    remote_call: Callable[[CruxibleClient], RemoteResultT],
    local_call: Callable[[], LocalResultT],
    *,
    allow_local: bool = True,
    command_name: str | None = None,
) -> RemoteResultT | LocalResultT:
    client = _get_client()
    if client is not None:
        return remote_call(client)
    if not allow_local:
        raise click.UsageError(
            f"Local mutation disabled for {command_name or 'this command'}; use server mode."
        )
    if _server_required():
        # Server mode is the declared backend, but no client could be resolved.
        # Refuse the silent on-disk fallback so reads fail the same way writes do
        # instead of leaking a confusing InstanceNotFoundError/ConfigError.
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)
    return local_call()


def _dispatch_cli_instance(
    remote_call: Callable[[CruxibleClient, str], RemoteResultT],
    local_call: Callable[[CruxibleInstance], LocalResultT],
    *,
    allow_local: bool = True,
    command_name: str | None = None,
) -> RemoteResultT | LocalResultT:
    def load_local_instance() -> CruxibleInstance:
        from cruxible_core.cli.instance import CruxibleInstance

        return CruxibleInstance.load()

    return _dispatch_cli(
        lambda client: remote_call(client, _require_instance_id()),
        lambda: local_call(load_local_instance()),
        allow_local=allow_local,
        command_name=command_name,
    )


def _guard_local_read_fallback() -> None:
    """Refuse a local on-disk read fallback when server mode is required.

    Read commands that resolve the client directly (rather than via
    ``_dispatch_cli``) call this before loading a local instance so every read
    verb shares one transport contract and one error surface.
    """
    if _server_required():
        raise click.UsageError(SERVER_MODE_REQUIRED_MESSAGE)


def _require_instance_id() -> str:
    obj = _root_ctx_obj()
    instance_id = obj.get("instance_id")
    if not instance_id:
        raise click.UsageError("--instance-id is required in server mode")
    return str(instance_id)


def _raise_server_mode_unsupported(command_name: str) -> None:
    raise click.UsageError(f"{command_name} is local-only and is not available in server mode.")


def _require_local_instance(command_name: str) -> CruxibleInstance:
    from cruxible_core.cli.instance import CruxibleInstance

    if _get_client() is not None:
        _raise_server_mode_unsupported(command_name)
    return CruxibleInstance.load()


def _read_text_or_error(path_str: str) -> str:
    path = Path(path_str)
    try:
        return path.read_text()
    except OSError as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc


def _read_validation_yaml_or_error(path_str: str) -> str:
    """Read config YAML for remote validation, composing overlays when needed."""
    import yaml

    from cruxible_core.config.composer import compose_config_sequence, resolve_config_layers
    from cruxible_core.config.loader import load_config

    path = Path(path_str)
    config = load_config(path)
    composed = compose_config_sequence(
        resolve_config_layers(config, config_path=path.resolve()),
    )
    composed_data = composed.model_dump(mode="python", by_alias=True, exclude_none=True)
    return yaml.safe_dump(composed_data, default_flow_style=False, sort_keys=False)


def _read_config_upload_or_error(
    path_str: str,
) -> tuple[str, contracts.ConfigSourceManifest]:
    """Compose an authored config and return its complete source manifest."""
    import yaml

    from cruxible_core.config.provenance import compose_file_with_source_manifest

    composed, source_manifest = compose_file_with_source_manifest(path_str)
    composed_data = composed.model_dump(mode="python", by_alias=True, exclude_none=True)
    config_yaml = yaml.safe_dump(composed_data, default_flow_style=False, sort_keys=False)
    return config_yaml, contracts.ConfigSourceManifest.model_validate(
        source_manifest.model_dump(mode="python")
    )


def _read_input_payload(path_str: str) -> dict[str, Any]:
    import yaml

    path = Path(path_str)
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc

    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse input file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ConfigError(f"Input file {path} must contain a top-level mapping")
    return payload


def _parse_inline_mapping(raw: str, *, source: str) -> dict[str, Any]:
    import yaml

    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"{source} must contain a top-level mapping")
    return payload


def _resolve_workflow_input(
    *,
    input_text: str | None,
    input_file: str | None,
) -> dict[str, Any]:
    if input_text is not None and input_file is not None:
        raise click.UsageError("Provide either --input or --input-file, not both")
    if input_text is not None:
        return _parse_inline_mapping(input_text, source="--input")
    if input_file is not None:
        return _read_input_payload(input_file)
    return {}


def _print_apply_previews(apply_previews: dict[str, Any]) -> None:
    if not apply_previews:
        return
    click.echo("Apply previews:")
    for step_id, preview in apply_previews.items():
        target = preview.get("entity_type") or preview.get("relationship_type") or step_id
        summary = (
            f"  {step_id}: {target} "
            f"creates={preview.get('create_count', 0)} "
            f"updates={preview.get('update_count', 0)} "
            f"noops={preview.get('noop_count', 0)}"
        )
        duplicate_count = preview.get("duplicate_input_count", 0)
        conflicting_count = preview.get("conflicting_duplicate_count", 0)
        if duplicate_count or conflicting_count:
            summary += f" duplicates={duplicate_count} conflicting={conflicting_count}"
        click.echo(summary)


def _print_query_param_hints(hints: contracts.QueryParamHints | None) -> None:
    if hints is None:
        return
    click.echo("Param hints:")
    click.echo(f"  entry_point={hints.entry_point}")
    if hints.primary_key is not None:
        click.echo(f"  primary_key={hints.primary_key}")
    if hints.required_params:
        click.echo(f"  required={', '.join(hints.required_params)}")
    if hints.example_ids:
        click.echo(f"  examples={', '.join(hints.example_ids)}")


def _build_query_param_hints(
    config: CoreConfig,
    query_name: str,
    example_entities: list[EntityInstance],
) -> contracts.QueryParamHints | None:
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    if query_schema.entry_point is None:
        return contracts.QueryParamHints(
            entry_point=None,
            required_params=[],
            primary_key=None,
            example_ids=[],
        )
    entity_schema = config.get_entity_type(query_schema.entry_point)
    primary_key = entity_schema.get_primary_key() if entity_schema is not None else None
    required_params = [primary_key] if primary_key is not None else []
    return contracts.QueryParamHints(
        entry_point=query_schema.entry_point,
        required_params=required_params,
        primary_key=primary_key,
        example_ids=sorted(entity.entity_id for entity in example_entities),
    )


def _lookup_query_param_hints_local(
    instance: CruxibleInstance,
    query_name: str,
) -> contracts.QueryParamHints | None:
    from cruxible_core.service import service_sample, service_schema

    config = service_schema(instance)
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    if query_schema.entry_point is None:
        return _build_query_param_hints(config, query_name, [])
    examples = service_sample(instance, query_schema.entry_point, limit=3).items
    return _build_query_param_hints(config, query_name, examples)


def _lookup_query_param_hints_server(
    client: CruxibleClient,
    instance_id: str,
    query_name: str,
) -> contracts.QueryParamHints | None:
    payload = client.schema(instance_id)
    query_payload = (payload.get("named_queries") or {}).get(query_name)
    if query_payload is None:
        return None
    entry_point = query_payload.get("entry_point")
    if entry_point is None:
        return contracts.QueryParamHints(
            entry_point=None,
            required_params=[],
            primary_key=None,
            example_ids=[],
        )
    entity_payload = (payload.get("entity_types") or {}).get(entry_point) or {}
    primary_key = next(
        (
            prop_name
            for prop_name, prop in (entity_payload.get("properties") or {}).items()
            if prop.get("primary_key")
        ),
        None,
    )
    sample = client.sample(instance_id, entry_point, limit=3)
    examples = _entities_from_payload(sample.items)
    return contracts.QueryParamHints(
        entry_point=entry_point,
        required_params=[primary_key] if primary_key is not None else [],
        primary_key=primary_key,
        example_ids=sorted(entity.entity_id for entity in examples),
    )


# ---- payload deserializers ----


def _entities_from_payload(items: list[dict[str, Any]]) -> list[EntityInstance]:
    from cruxible_core.graph.types import EntityInstance

    return [EntityInstance.model_validate(item) for item in items]


def _feedback_from_payload(items: list[dict[str, Any]]) -> list[FeedbackRecord]:
    from cruxible_core.feedback.types import FeedbackRecord

    return [FeedbackRecord.model_validate(item) for item in items]


def _outcomes_from_payload(items: list[dict[str, Any]]) -> list[OutcomeRecord]:
    from cruxible_core.feedback.types import OutcomeRecord

    return [OutcomeRecord.model_validate(item) for item in items]


def _groups_from_payload(items: list[dict[str, Any]]) -> list[CandidateGroup]:
    from cruxible_core.group.types import CandidateGroup

    return [CandidateGroup.model_validate(item) for item in items]


def _members_from_payload(items: list[dict[str, Any]]) -> list[CandidateMember]:
    from cruxible_core.group.types import CandidateMember

    return [CandidateMember.model_validate(item) for item in items]


def _parse_params(params: tuple[str, ...]) -> dict[str, str]:
    """Parse KEY=VALUE pairs into a dict."""
    result: dict[str, str] = {}
    for p in params:
        parts = p.split("=", 1)
        if len(parts) != 2:
            raise click.BadParameter(f"Parameter must be KEY=VALUE, got: {p}")
        result[parts[0]] = parts[1]
    return result
