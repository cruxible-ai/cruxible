"""Shared dispatch, parsing, and formatting helpers for CLI commands."""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

import click
import yaml
from rich.console import Console

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.context import (
    CliContextState,
    clear_cli_context,
    load_cli_context,
    save_cli_context,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_sequence, resolve_config_layers
from cruxible_core.config.loader import load_config
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateGroup, CandidateMember
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.service import OperationContext, service_sample, service_schema

console = Console()
LocalResultT = TypeVar("LocalResultT")
RemoteResultT = TypeVar("RemoteResultT")

json_option = click.option(
    "--json", "output_json", is_flag=True, default=False, help="Output as JSON.",
)

decision_record_option = click.option(
    "--decision-record",
    "decision_record_id",
    default=None,
    help="Decision record ID for audit logging.",
)


def _emit_json(data: Any) -> None:
    """Emit structured JSON to stdout, bypassing Rich."""
    click.echo(_json.dumps(data, indent=2, default=str))


def _resolve_decision_record_id(decision_record_id: str | None) -> str | None:
    return decision_record_id


@dataclass(frozen=True)
class ActiveInstanceChange:
    """Result of updating the remembered active instance."""

    previous: str | None
    current: str


def _operation_context(decision_record_id: str | None) -> OperationContext | None:
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
    return local_call()


def _dispatch_cli_instance(
    remote_call: Callable[[CruxibleClient, str], RemoteResultT],
    local_call: Callable[[CruxibleInstance], LocalResultT],
    *,
    allow_local: bool = True,
    command_name: str | None = None,
) -> RemoteResultT | LocalResultT:
    return _dispatch_cli(
        lambda client: remote_call(client, _require_instance_id()),
        lambda: local_call(CruxibleInstance.load()),
        allow_local=allow_local,
        command_name=command_name,
    )


def _require_instance_id() -> str:
    obj = _root_ctx_obj()
    instance_id = obj.get("instance_id")
    if not instance_id:
        raise click.UsageError("--instance-id is required in server mode")
    return str(instance_id)


def _raise_server_mode_unsupported(command_name: str) -> None:
    raise click.UsageError(
        f"{command_name} is not available in server mode. Use it locally or wait for v2."
    )


def _require_local_instance(command_name: str) -> CruxibleInstance:
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
    path = Path(path_str)
    config = load_config(path)
    composed = compose_config_sequence(
        resolve_config_layers(config, config_path=path.resolve()),
    )
    composed_data = composed.model_dump(mode="python", by_alias=True, exclude_none=True)
    return yaml.safe_dump(composed_data, default_flow_style=False, sort_keys=False)


def _read_input_payload(path_str: str) -> dict[str, Any]:
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
            summary += (
                f" duplicates={duplicate_count} "
                f"conflicting={conflicting_count}"
            )
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
    config = service_schema(instance)
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    if query_schema.entry_point is None:
        return _build_query_param_hints(config, query_name, [])
    examples = service_sample(instance, query_schema.entry_point, limit=3)
    return _build_query_param_hints(config, query_name, examples)


def _lookup_query_param_hints_server(
    client: CruxibleClient,
    instance_id: str,
    query_name: str,
) -> contracts.QueryParamHints | None:
    config = CoreConfig.model_validate(client.schema(instance_id))
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    if query_schema.entry_point is None:
        return _build_query_param_hints(config, query_name, [])
    sample = client.sample(instance_id, query_schema.entry_point, limit=3)
    examples = _entities_from_payload(sample.items)
    return _build_query_param_hints(config, query_name, examples)


# ---- payload deserializers ----


def _entities_from_payload(items: list[dict[str, Any]]) -> list[EntityInstance]:
    return [EntityInstance.model_validate(item) for item in items]


def _feedback_from_payload(items: list[dict[str, Any]]) -> list[FeedbackRecord]:
    return [FeedbackRecord.model_validate(item) for item in items]


def _outcomes_from_payload(items: list[dict[str, Any]]) -> list[OutcomeRecord]:
    return [OutcomeRecord.model_validate(item) for item in items]


def _groups_from_payload(items: list[dict[str, Any]]) -> list[CandidateGroup]:
    return [CandidateGroup.model_validate(item) for item in items]


def _members_from_payload(items: list[dict[str, Any]]) -> list[CandidateMember]:
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
