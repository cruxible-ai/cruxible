"""Runtime facade shared by HTTP routes and MCP handlers."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from cruxible_client import contracts
from cruxible_core.config.schema import schema_wire_payload
from cruxible_core.errors import AuthenticationError, ConfigError
from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    require_hosted_actor_context,
)
from cruxible_core.graph.provenance import (
    SOURCE_REF_ADD_RELATIONSHIP,
    SOURCE_REF_BATCH_DIRECT_WRITE,
)
from cruxible_core.primitives import canonical_json, new_id
from cruxible_core.query.types import dump_query_row
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import (
    check_permission,
    require_unscoped_operator,
    validate_root_dir,
)
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry
from cruxible_core.service import (
    AnalyzeFeedbackResult,
    AnalyzeOutcomesResult,
    resolve_contained_source_path,
    service_abandon_decision_record,
    service_add_constraint,
    service_add_decision_policy,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_analyze_feedback,
    service_analyze_outcomes,
    service_apply_workflow,
    service_backup_instance,
    service_batch_direct_write,
    service_clone_snapshot,
    service_config_compatibility_warnings,
    service_create_decision_record,
    service_create_snapshot,
    service_create_state_overlay,
    service_dereference_source_evidence,
    service_describe_query,
    service_evaluate,
    service_explain_receipt,
    service_feedback_batch_inputs,
    service_feedback_from_query_result,
    service_feedback_input,
    service_finalize_decision_record,
    service_get_decision_record,
    service_get_entity,
    service_get_entity_change_history,
    service_get_feedback_profile,
    service_get_group,
    service_get_outcome_profile,
    service_get_receipt,
    service_get_relationship,
    service_get_relationship_lineage,
    service_get_source_artifact,
    service_get_trace,
    service_group_status,
    service_init,
    service_init_governed_upload,
    service_inspect_entity,
    service_inspect_view,
    service_lint,
    service_list,
    service_list_decision_events,
    service_list_decision_records,
    service_list_groups,
    service_list_queries,
    service_list_resolutions,
    service_list_snapshots,
    service_list_source_artifacts,
    service_list_traces,
    service_lock,
    service_outcome,
    service_plan,
    service_propose_group_inputs,
    service_propose_workflow,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_query_inline_surface,
    service_query_surface,
    service_register_source_artifact,
    service_reload_config,
    service_relocate_instance,
    service_resolve_group,
    service_restore_instance,
    service_run,
    service_sample,
    service_schema,
    service_server_info,
    service_state_health,
    service_state_status,
    service_stats,
    service_test,
    service_update_trust_status,
    service_validate,
)
from cruxible_core.service.lifecycle_inputs import (
    entity_metadata_with_lifecycle,
    relationship_lifecycle_state,
)
from cruxible_core.service.snapshots import paths_overlap, read_instance_backup_manifest
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    FeedbackItemInput,
    GroupMemberInput,
    GroupSignalInput,
    OperationContext,
    QueryServiceResult,
    RelationshipTargetInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
)
from cruxible_core.temporal import format_datetime, utc_now

logger = logging.getLogger(__name__)

WorkflowExecutionContractT = TypeVar(
    "WorkflowExecutionContractT",
    contracts.WorkflowRunResult,
    contracts.WorkflowApplyResult,
)

_HOSTED_INIT_METADATA_RELATIVE_PATH = Path(CruxibleInstance.INSTANCE_DIR) / "hosted_init.json"


def _build_workflow_execution_contract(
    result: Any,
    result_type: type[WorkflowExecutionContractT],
) -> WorkflowExecutionContractT:
    """Normalize workflow run/apply service results into MCP contracts."""
    return result_type(
        workflow=result.workflow,
        output=result.output,
        receipt_id=result.receipt_id,
        mode=result.mode,
        workflow_type=result.workflow_type,
        canonical=result.canonical,
        apply_digest=result.apply_digest,
        head_snapshot_id=result.head_snapshot_id,
        committed_snapshot_id=result.committed_snapshot_id,
        apply_previews=result.apply_previews,
        query_receipt_ids=result.query_receipt_ids,
        read_metadata=result.read_metadata,
        trace_ids=result.trace_ids,
        receipt=result.receipt.model_dump(mode="json") if result.receipt else None,
        traces=[trace.model_dump(mode="json") for trace in result.traces],
    )


def _operation_context(
    decision_record_id: str | None,
    *,
    surface: str = "local",
    actor_context: GovernedActorContext | None = None,
) -> OperationContext | None:
    if decision_record_id is None and actor_context is None:
        return None
    return OperationContext(
        decision_record_id=decision_record_id,
        surface=surface,  # type: ignore[arg-type]
        actor_context=actor_context,
    )


def _runtime_credential_actor_context() -> GovernedActorContext | None:
    from cruxible_core.server.auth import get_current_auth_context

    auth_context = get_current_auth_context()
    if auth_context is None or auth_context.credential_type != "runtime_credential":
        return None
    try:
        return GovernedActorContext(
            actor_type="service_account",
            actor_id=auth_context.principal_label,
            org_id=auth_context.instance_scope or "local",
            operation_id=new_id("op", length=16, separator="_"),
            timestamp=utc_now(),
        )
    except ValidationError as exc:
        raise ConfigError("hosted governed actor context is required") from exc


def _local_operator_actor_context(value: Any) -> GovernedActorContext:
    from cruxible_core.server.auth_managed_entities import local_operator_actor_context

    request_id = None
    if value is not None:
        request_id = require_hosted_actor_context(value).request_id
    return local_operator_actor_context(request_id=request_id)


def _record_actor_operation(actor: GovernedActorContext) -> None:
    from cruxible_core.server.auth import set_current_operation_id

    set_current_operation_id(actor.operation_id)


def _require_review_promotion_actor(
    action: str,
    actor: GovernedActorContext | None,
) -> None:
    """Reject anonymous review-state promotion under an auth-on governed runtime.

    ``cruxible_feedback`` is a GOVERNED_WRITE tool, but an ``approve`` promotes a
    relationship's review status to ``approved``/live, which can satisfy a
    GRAPH_WRITE close-gate precondition (audit F3). When server auth is enabled the
    promotion must carry a resolved actor identity so a lower tier cannot rubber-stamp
    a review edge anonymously. When auth is off there is no tier boundary or governed
    identity to enforce, so local promotion is attributed to the declared
    operator. Legitimate ``correct``/``flag``/``reject``
    actions are untouched.
    """
    from cruxible_core.feedback.applier import REVIEW_PROMOTION_ACTIONS
    from cruxible_core.server.config import is_server_auth_enabled

    if action not in REVIEW_PROMOTION_ACTIONS:
        return
    if actor is not None:
        return
    if is_server_auth_enabled():
        raise AuthenticationError(
            f"Feedback action '{action}' promotes a relationship review to approved "
            "and requires a resolved actor identity (actor_context) under a governed "
            "runtime"
        )


def _hosted_actor_context(value: Any) -> GovernedActorContext | None:
    credential_actor = _runtime_credential_actor_context()
    if credential_actor is not None:
        # The request is authenticated by a runtime credential, so the
        # credential-derived identity is authoritative. A request-supplied
        # actor_context must NOT be able to assert an arbitrary actor; otherwise
        # a credential labeled e.g. "codex-core" could submit
        # actor_id="robert" and pass identity-gated approval guards. When the
        # request also supplies an actor_context, accept it only when it agrees
        # with the credential identity; reject on any mismatch.
        if value is not None:
            actor = _reconcile_credential_actor_context(credential_actor, value)
        else:
            actor = credential_actor
        _record_actor_operation(actor)
        return actor

    from cruxible_core.server.config import is_server_auth_enabled

    if not is_server_auth_enabled():
        # Auth-off: an explicitly supplied actor_context is honored as before
        # (embedded and hosted callers assert identity deliberately; silently
        # rewriting it to the operator would distort provenance). The declared
        # local operator is the DEFAULT for requests that supply nothing, so
        # the resolved actor stays non-None and payload-metadata actor
        # spoofing remains overridden downstream.
        if value is not None:
            actor = require_hosted_actor_context(value)
        else:
            actor = _local_operator_actor_context(None)
        _record_actor_operation(actor)
        return actor
    if value is None:
        return None
    actor = require_hosted_actor_context(value)
    _record_actor_operation(actor)
    return actor


def _reconcile_credential_actor_context(
    credential_actor: GovernedActorContext,
    value: Any,
) -> GovernedActorContext:
    """Reconcile a request-supplied actor_context against the credential identity.

    The runtime credential is authoritative for the actor identity fields
    (actor_type / actor_id / org_id). A supplied actor_context may only carry
    matching identity fields; mismatches are rejected as an authentication
    failure rather than silently overriding the authenticated principal. Any
    supplied request_id is preserved for correlation.
    """
    supplied = require_hosted_actor_context(value)
    mismatches = [
        field
        for field in ("actor_type", "actor_id", "org_id")
        if getattr(supplied, field) != getattr(credential_actor, field)
    ]
    if mismatches:
        raise AuthenticationError(
            "Supplied actor_context does not match the authenticated runtime "
            f"credential identity (mismatched: {', '.join(mismatches)})"
        )
    if supplied.request_id is None:
        return credential_actor
    return credential_actor.model_copy(update={"request_id": supplied.request_id})


def _has_init_config(
    config_path: str | None,
    config_yaml: str | None,
    kits: list[str] | None,
) -> bool:
    return config_path is not None or config_yaml is not None or bool(kits)


def _check_init_permissions(root_dir: str, *, has_config: bool) -> None:
    check_permission("cruxible_init")
    if has_config:
        check_permission(
            "cruxible_init_with_config",
            instance_id=root_dir,
            enforce_instance_scope=False,
        )
    validate_root_dir(root_dir)


def _load_or_initialize_instance(
    *,
    instance_root: Path,
    instance_id: str,
    has_config: bool,
    existing_with_config_error: str,
    initialize: Callable[[], Any],
    include_initialized_warnings: bool,
) -> contracts.InitResult:
    instance_json = instance_root / CruxibleInstance.INSTANCE_DIR / "instance.json"

    if instance_json.exists():
        if has_config:
            raise ConfigError(existing_with_config_error)
        instance = CruxibleInstance.load(instance_root)
        warnings = service_config_compatibility_warnings(instance)
        status = "loaded"
    else:
        result = initialize()
        instance = result.instance
        warnings = result.warnings if include_initialized_warnings else []
        status = "initialized"

    get_manager().register(instance_id, instance)
    return contracts.InitResult(instance_id=instance_id, status=status, warnings=warnings)


def init_local(
    root_dir: str,
    config_path: str | None = None,
    config_yaml: str | None = None,
    data_dir: str | None = None,
    kits: list[str] | None = None,
) -> contracts.InitResult:
    """Initialize a new cruxible instance, or reload an existing one."""
    has_config = _has_init_config(config_path, config_yaml, kits)
    _check_init_permissions(root_dir, has_config=has_config)
    root = Path(root_dir)
    return _load_or_initialize_instance(
        instance_root=root,
        instance_id=str(root),
        has_config=has_config,
        existing_with_config_error=(
            f"Instance already exists at {root}. "
            "To update the config, edit the YAML file on disk, then call "
            "cruxible_init(root_dir=...) without config_path/config_yaml to reload. "
            "The updated config takes effect immediately."
        ),
        initialize=lambda: service_init(
            root_dir,
            config_path=config_path,
            config_yaml=config_yaml,
            data_dir=data_dir,
            kits=kits,
        ),
        include_initialized_warnings=False,
    )


def init_governed(
    root_dir: str,
    config_path: str | None = None,
    config_yaml: str | None = None,
    data_dir: str | None = None,
    kits: list[str] | None = None,
) -> contracts.InitResult:
    """Initialize or reload a daemon-owned governed instance."""
    check_permission(
        "cruxible_governed_instance_lifecycle",
        instance_id=root_dir,
        enforce_instance_scope=False,
    )
    has_config = _has_init_config(config_path, config_yaml, kits)
    _check_init_permissions(root_dir, has_config=has_config)

    registry = get_registry()
    existing = registry.get_governed_instance_by_workspace_root(root_dir)
    if existing is not None:
        governed_root = Path(existing.location)

        def initialize_existing_governed() -> Any:
            if config_path is not None and config_yaml is None:
                raise ConfigError(
                    "Direct server init requires uploaded config content. "
                    "CLI and MCP callers should read the config locally and send config_yaml "
                    "instead of passing config_path."
                )
            return service_init_governed_upload(
                governed_root,
                workspace_root=root_dir,
                config_yaml=config_yaml,
                data_dir=data_dir,
                kits=kits,
            )

        return _load_or_initialize_instance(
            instance_root=governed_root,
            instance_id=existing.instance_id,
            has_config=has_config,
            existing_with_config_error=(
                "Governed instance already exists for this workspace root. "
                "Edit the config locally, then use `config reload` in server mode to sync it."
            ),
            initialize=initialize_existing_governed,
            include_initialized_warnings=True,
        )

    instance_id = registry.generate_governed_instance_id()
    governed_root = registry.governed_instance_location(instance_id)

    def initialize_governed() -> Any:
        if config_path is not None and config_yaml is None:
            raise ConfigError(
                "Direct server init requires uploaded config content. "
                "CLI and MCP callers should read the config locally and send config_yaml "
                "instead of passing config_path."
            )
        return service_init_governed_upload(
            governed_root,
            workspace_root=root_dir,
            config_yaml=config_yaml,
            data_dir=data_dir,
            kits=kits,
        )

    try:
        result = initialize_governed()
    except Exception:
        shutil.rmtree(governed_root, ignore_errors=True)
        raise
    registered = registry.create_governed_instance_with_id(instance_id, workspace_root=root_dir)
    get_manager().register(registered.record.instance_id, result.instance)
    return contracts.InitResult(
        instance_id=registered.record.instance_id,
        status="initialized",
        warnings=result.warnings,
    )


def init_hosted_instance(
    *,
    instance_id: str | None = None,
    source_type: contracts.HostedInstanceSourceType,
    kit_refs: list[str] | None = None,
    transport_ref: str | None = None,
    state_ref: str | None = None,
    overlay_kit_ref: str | None = None,
    no_overlay_kit: bool = False,
) -> contracts.HostedInstanceInitResult:
    """Initialize a fresh server-owned hosted instance from a first-class source ref."""
    registry = get_registry()
    selected_instance_id = (instance_id or "").strip() or registry.generate_governed_instance_id()
    check_permission("cruxible_hosted_instance_init", instance_id=selected_instance_id)
    _validate_hosted_init_inputs(
        source_type=source_type,
        kit_refs=kit_refs,
        transport_ref=transport_ref,
        state_ref=state_ref,
        overlay_kit_ref=overlay_kit_ref,
        no_overlay_kit=no_overlay_kit,
    )
    kit_refs = [value.strip() for value in (kit_refs or []) if value.strip()] or None
    transport_ref = (transport_ref or "").strip() or None
    state_ref = (state_ref or "").strip() or None
    overlay_kit_ref = (overlay_kit_ref or "").strip() or None

    source_payload = _hosted_init_source_payload(
        source_type=source_type,
        kit_refs=kit_refs,
        transport_ref=transport_ref,
        state_ref=state_ref,
        overlay_kit_ref=overlay_kit_ref,
        no_overlay_kit=no_overlay_kit,
    )
    request_digest = _hosted_init_digest(source_payload)

    existing = registry.get(selected_instance_id)
    instance_root = registry.governed_instance_location(selected_instance_id)
    if existing is not None:
        if existing.backend != GOVERNED_DAEMON_BACKEND:
            raise ConfigError(f"Instance '{selected_instance_id}' is not a hosted instance")
        return _load_hosted_instance_idempotently(
            instance_id=selected_instance_id,
            instance_root=Path(existing.location),
            expected_digest=request_digest,
        )
    if instance_root.exists():
        raise ConfigError(f"Hosted instance root already exists for {selected_instance_id}")

    try:
        if source_type == "kit":
            assert kit_refs is not None
            init_result = service_init(
                root_dir=instance_root,
                kits=kit_refs,
                instance_mode=CruxibleInstance.GOVERNED_MODE,
            )
            instance = init_result.instance
            manifest: contracts.PublishedStateManifest | None = None
            resolved_source_ref: str | None = None
            warnings = init_result.warnings
        else:
            overlay_result = service_create_state_overlay(
                transport_ref=transport_ref,
                state_ref=state_ref,
                kit=overlay_kit_ref,
                no_kit=no_overlay_kit,
                root_dir=instance_root,
                instance_mode=CruxibleInstance.GOVERNED_MODE,
            )
            instance = overlay_result.instance
            manifest = contracts.PublishedStateManifest.model_validate(
                overlay_result.manifest.model_dump(mode="json")
            )
            upstream = instance.get_upstream_metadata()
            resolved_source_ref = upstream.transport_ref if upstream is not None else None
            warnings = []

        metadata = _hosted_init_metadata(
            request_digest=request_digest,
            source_payload=source_payload,
            resolved_source_ref=resolved_source_ref,
            manifest=manifest,
            warnings=warnings,
        )
        _write_hosted_init_metadata(instance_root, metadata)
        registered = registry.create_governed_instance_with_id(selected_instance_id)
    except Exception:
        shutil.rmtree(instance_root, ignore_errors=True)
        raise

    get_manager().register(registered.record.instance_id, instance)
    return _hosted_init_result_from_metadata(
        instance_id=registered.record.instance_id,
        status="initialized",
        metadata=metadata,
    )


def _validate_hosted_init_inputs(
    *,
    source_type: contracts.HostedInstanceSourceType,
    kit_refs: list[str] | None,
    transport_ref: str | None,
    state_ref: str | None,
    overlay_kit_ref: str | None,
    no_overlay_kit: bool,
) -> None:
    normalized_kit_refs = [value.strip() for value in (kit_refs or []) if value.strip()]
    if source_type == "kit":
        if not normalized_kit_refs:
            raise ConfigError("kit_refs is required when source_type=kit")
        if any((value or "").strip() for value in (transport_ref, state_ref, overlay_kit_ref)):
            raise ConfigError(
                "transport_ref, state_ref, and overlay_kit_ref require source_type=reference_model"
            )
        if no_overlay_kit:
            raise ConfigError("no_overlay_kit requires source_type=reference_model")
        return

    has_transport = bool((transport_ref or "").strip())
    has_state = bool((state_ref or "").strip())
    if has_transport == has_state:
        raise ConfigError(
            "Provide exactly one of transport_ref or state_ref when source_type=reference_model"
        )
    if (overlay_kit_ref or "").strip() and no_overlay_kit:
        raise ConfigError("Provide overlay_kit_ref or no_overlay_kit, not both")
    if normalized_kit_refs:
        raise ConfigError("kit_refs requires source_type=kit")


def _load_hosted_instance_idempotently(
    *,
    instance_id: str,
    instance_root: Path,
    expected_digest: str,
) -> contracts.HostedInstanceInitResult:
    metadata = _read_hosted_init_metadata(instance_root)
    if metadata is None:
        raise ConfigError(
            f"Hosted instance '{instance_id}' already exists without hosted init metadata"
        )
    if metadata.get("request_digest") != expected_digest:
        raise ConfigError(
            f"Hosted instance '{instance_id}' is already initialized with different material"
        )
    instance = CruxibleInstance.load(instance_root)
    get_manager().register(instance_id, instance)
    return _hosted_init_result_from_metadata(
        instance_id=instance_id,
        status="already_initialized",
        metadata=metadata,
    )


def _hosted_init_source_payload(
    *,
    source_type: contracts.HostedInstanceSourceType,
    kit_refs: list[str] | None,
    transport_ref: str | None,
    state_ref: str | None,
    overlay_kit_ref: str | None,
    no_overlay_kit: bool,
) -> dict[str, Any]:
    if source_type == "kit":
        return {
            "source_type": source_type,
            "kit_refs": kit_refs,
        }
    return {
        "source_type": source_type,
        "transport_ref": transport_ref,
        "state_ref": state_ref,
        "overlay_kit_ref": overlay_kit_ref,
        "no_overlay_kit": no_overlay_kit,
    }


def _hosted_init_digest(payload: dict[str, Any]) -> str:
    encoded = canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _hosted_init_metadata(
    *,
    request_digest: str,
    source_payload: dict[str, Any],
    resolved_source_ref: str | None,
    manifest: contracts.PublishedStateManifest | None,
    warnings: list[str],
) -> dict[str, Any]:
    initialized_at = format_datetime(utc_now())
    assert initialized_at is not None
    return {
        "version": 1,
        "initialized_at": initialized_at,
        "request_digest": request_digest,
        "source": source_payload,
        "resolved_source_ref": resolved_source_ref,
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "warnings": warnings,
    }


def _hosted_init_metadata_path(instance_root: Path) -> Path:
    return instance_root / _HOSTED_INIT_METADATA_RELATIVE_PATH


def _read_hosted_init_metadata(instance_root: Path) -> dict[str, Any] | None:
    path = _hosted_init_metadata_path(instance_root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ConfigError(f"Hosted init metadata at {path} must be a JSON object")
    return payload


def _write_hosted_init_metadata(instance_root: Path, metadata: dict[str, Any]) -> None:
    path = _hosted_init_metadata_path(instance_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def _hosted_init_result_from_metadata(
    *,
    instance_id: str,
    status: contracts.HostedInstanceInitStatus,
    metadata: dict[str, Any],
) -> contracts.HostedInstanceInitResult:
    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ConfigError("Hosted init metadata source must be a JSON object")
    source_type_raw = source.get("source_type")
    if source_type_raw not in ("kit", "reference_model"):
        raise ConfigError("Hosted init metadata source_type is invalid")
    source_type = cast(contracts.HostedInstanceSourceType, source_type_raw)
    if source_type == "kit":
        kit_refs_value = source.get("kit_refs")
        if not isinstance(kit_refs_value, list):
            raise ConfigError("Hosted init metadata kit_refs must be a list")
        source_ref = " ".join(str(value) for value in kit_refs_value)
        overlay_kit_ref = None
    else:
        source_ref = str(source.get("state_ref") or source.get("transport_ref") or "")
        overlay_kit_ref = source.get("overlay_kit_ref")
        if overlay_kit_ref is not None:
            overlay_kit_ref = str(overlay_kit_ref)
    manifest_payload = metadata.get("manifest")
    manifest = (
        contracts.PublishedStateManifest.model_validate(manifest_payload)
        if manifest_payload is not None
        else None
    )
    warnings = metadata.get("warnings") or []
    return contracts.HostedInstanceInitResult(
        instance_id=instance_id,
        status=status,
        source_type=source_type,
        source_ref=source_ref,
        resolved_source_ref=(
            str(metadata["resolved_source_ref"])
            if metadata.get("resolved_source_ref") is not None
            else None
        ),
        overlay_kit_ref=overlay_kit_ref,
        manifest=manifest,
        warnings=list(warnings) if isinstance(warnings, list) else [],
    )


def validate(
    config_path: str | None = None,
    config_yaml: str | None = None,
) -> contracts.ValidateResult:
    """Validate a config file or inline YAML string."""
    check_permission("cruxible_validate")

    result = service_validate(config_path=config_path, config_yaml=config_yaml)
    config = result.config
    return contracts.ValidateResult(
        valid=True,
        name=config.name,
        entity_types=list(config.entity_types.keys()),
        relationships=[relationship.name for relationship in config.relationships],
        named_queries=list(config.named_queries.keys()),
        warnings=result.warnings,
    )


def server_info() -> contracts.ServerInfoResult:
    """Return live daemon metadata without requiring an instance."""
    check_permission("cruxible_server_info")
    # Daemon-wide read of global, cross-tenant metadata: gate behind an unscoped
    # operator credential so an instance-scoped ADMIN cannot enumerate the shared
    # daemon's global state. Auth-off/local and bootstrap operators still pass.
    require_unscoped_operator("cruxible_server_info")
    result = service_server_info()
    return contracts.ServerInfoResult(
        server_required=result.server_required,
        state_dir=result.state_dir,
        version=result.version,
        instance_count=result.instance_count,
        auth_enabled=result.auth_enabled,
        auth_required=result.auth_required,
    )


def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place re-exec of the daemon, preserving port/state/env."""
    check_permission("cruxible_server_restart")
    # Re-exec is daemon-wide: on a shared daemon it restarts every tenant's
    # instance. Require an unscoped operator credential so an instance-scoped
    # ADMIN cannot trigger a cross-tenant restart (DoS). Auth-off/local and
    # bootstrap operators still pass.
    require_unscoped_operator("cruxible_server_restart")
    from cruxible_core.server.restart import schedule_server_restart

    result = service_server_info()
    schedule_server_restart()
    return contracts.ServerRestartResult(
        scheduled=True,
        version=result.version,
        state_dir=result.state_dir,
    )


def workflow_lock(
    instance_id: str,
    force: bool = False,
) -> contracts.WorkflowLockResult:
    """Generate a workflow lock through the governed service layer."""
    check_permission("cruxible_lock_workflow", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_lock(instance, force=force)
    return contracts.WorkflowLockResult(
        lock_path=result.lock_path,
        config_digest=result.config_digest,
        providers_locked=result.providers_locked,
        artifacts_locked=result.artifacts_locked,
    )


def workflow_plan(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
) -> contracts.WorkflowPlanResult:
    """Compile a workflow plan through the governed service layer."""
    check_permission("cruxible_plan_workflow", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_plan(instance, workflow_name, input_payload or {})
    return contracts.WorkflowPlanResult(plan=result.plan.model_dump(mode="json"))


def workflow_run(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
    *,
    decision_record_id: str | None = None,
    surface: str = "local",
    actor_context: Any | None = None,
) -> contracts.WorkflowRunResult:
    """Execute a workflow through the governed service layer."""
    check_permission("cruxible_run_workflow", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_run(
        instance,
        workflow_name,
        input_payload or {},
        context=_operation_context(decision_record_id, surface=surface, actor_context=actor),
    )
    return _build_workflow_execution_contract(result, contracts.WorkflowRunResult)


def workflow_apply(
    instance_id: str,
    workflow_name: str,
    expected_apply_digest: str,
    expected_head_snapshot_id: str | None,
    input_payload: dict[str, Any] | None = None,
    *,
    decision_record_id: str | None = None,
    surface: str = "local",
    actor_context: Any | None = None,
) -> contracts.WorkflowApplyResult:
    """Apply a canonical workflow through the governed service layer."""
    check_permission("cruxible_apply_workflow", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_apply_workflow(
        instance,
        workflow_name,
        input_payload or {},
        expected_apply_digest=expected_apply_digest,
        expected_head_snapshot_id=expected_head_snapshot_id,
        context=_operation_context(decision_record_id, surface=surface, actor_context=actor),
    )
    return _build_workflow_execution_contract(result, contracts.WorkflowApplyResult)


def workflow_test(
    instance_id: str,
    name: str | None = None,
    actor_context: Any | None = None,
) -> contracts.WorkflowTestResult:
    """Execute config-defined workflow tests through the governed service layer."""
    check_permission("cruxible_test_workflow", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_test(instance, test_name=name, actor_context=actor)
    return contracts.WorkflowTestResult(
        total=result.total,
        passed=result.passed,
        failed=result.failed,
        cases=[
            contracts.WorkflowTestCaseResult(
                name=case.name,
                workflow=case.workflow,
                passed=case.passed,
                output=case.output,
                receipt_id=case.receipt_id,
                error=case.error,
            )
            for case in result.cases
        ],
    )


def propose_workflow(
    instance_id: str,
    workflow_name: str,
    input_payload: dict[str, Any] | None = None,
    *,
    decision_record_id: str | None = None,
    surface: str = "local",
    actor_context: Any | None = None,
) -> contracts.WorkflowProposeResult:
    """Execute a workflow and bridge its output into a governed relationship proposal."""
    check_permission(
        "cruxible_propose_workflow",
        instance_id=instance_id,
    )
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_propose_workflow(
        instance,
        workflow_name,
        input_payload or {},
        context=_operation_context(decision_record_id, surface=surface, actor_context=actor),
    )
    return contracts.WorkflowProposeResult(
        workflow=result.workflow,
        output=result.output,
        receipt_id=result.receipt_id,
        mode=result.mode,
        workflow_type=result.workflow_type,
        canonical=result.canonical,
        group_id=result.group_id,
        group_status=result.group_status,
        review_priority=result.review_priority,
        suppressed=result.suppressed,
        suppressed_members=[
            contracts.SuppressedProposalMember(**item.__dict__)
            for item in result.suppressed_members
        ],
        query_receipt_ids=result.query_receipt_ids,
        read_metadata=result.read_metadata,
        trace_ids=result.trace_ids,
        prior_resolution=(
            result.prior_resolution.model_dump(mode="json")
            if result.prior_resolution is not None
            else None
        ),
        policy_summary=result.policy_summary,
        receipt=result.receipt.model_dump(mode="json") if result.receipt else None,
        traces=[trace.model_dump(mode="json") for trace in result.traces],
    )


def create_snapshot(
    instance_id: str,
    label: str | None = None,
    actor_context: Any | None = None,
) -> contracts.SnapshotCreateResult:
    """Create an immutable full snapshot for an instance."""
    check_permission("cruxible_create_snapshot", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_create_snapshot(instance, label=label, actor_context=actor)
    return contracts.SnapshotCreateResult(
        snapshot=contracts.SnapshotMetadata.model_validate(result.snapshot.model_dump(mode="json"))
    )


def backup_instance(
    instance_id: str,
    *,
    artifact_path: str,
    label: str | None = None,
    actor_context: Any | None = None,
) -> contracts.InstanceBackupResult:
    """Write a same-identity backup artifact for a governed instance."""
    check_permission("cruxible_instance_backup", instance_id=instance_id)
    _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_backup_instance(
        instance,
        instance_id=instance_id,
        artifact_path=artifact_path,
        label=label,
    )
    return contracts.InstanceBackupResult(
        instance_id=result.instance_id,
        artifact_path=result.artifact_path,
        manifest=contracts.InstanceBackupManifest.model_validate(
            result.manifest.model_dump(mode="json")
        ),
    )


def restore_instance(
    *,
    artifact_path: str,
    root_dir: str | None = None,
) -> contracts.InstanceRestoreResult:
    """Restore a same-identity governed instance from a backup artifact."""
    check_permission("cruxible_instance_restore", enforce_instance_scope=False)
    # Restore registers a (possibly new) instance into the shared daemon and is
    # authorized here before the target instance_id is known from the manifest.
    # That pre-manifest check cannot enforce instance scope, so an instance-scoped
    # ADMIN would otherwise pass it. Gate it as a daemon-wide operator action:
    # only an unscoped operator credential (or auth-off/local) may restore. The
    # post-manifest check below still scope-checks the resolved instance_id, which
    # is a no-op for the unscoped operator that just passed this gate.
    require_unscoped_operator("cruxible_instance_restore")
    manifest = read_instance_backup_manifest(artifact_path)
    check_permission("cruxible_instance_restore", instance_id=manifest.instance_id)
    registry = get_registry()
    manager = get_manager()
    if manifest.instance_id in manager.list_ids():
        raise ConfigError(
            f"Instance '{manifest.instance_id}' is already loaded; stop it before restore"
        )

    record = registry.get(manifest.instance_id)
    if record is not None and record.backend != GOVERNED_DAEMON_BACKEND:
        raise ConfigError(
            f"Instance '{manifest.instance_id}' is registered with unsupported backend "
            f"'{record.backend}'"
        )
    target_root = (
        Path(root_dir).expanduser()
        if root_dir is not None
        else registry.governed_instance_location(manifest.instance_id)
    )
    validate_root_dir(str(target_root))

    registry_status: Literal["registered", "repaired", "unchanged"] = "registered"
    if record is not None:
        registered_root = Path(record.location)
        if _registry_record_points_to_healthy_instance(registered_root):
            raise ConfigError(
                f"Instance '{manifest.instance_id}' already exists at {registered_root}"
            )
        registry_status = "repaired"

    result = service_restore_instance(
        artifact_path=artifact_path,
        root_dir=target_root,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
        registry_status=registry_status,
    )
    if record is None:
        created = registry.create_governed_instance_with_id(manifest.instance_id)
        if Path(created.record.location) != target_root:
            registry.update_governed_instance_location(manifest.instance_id, target_root)
    else:
        registry.update_governed_instance_location(manifest.instance_id, target_root)
    manager.register(manifest.instance_id, result.instance)
    return contracts.InstanceRestoreResult(
        instance_id=result.instance_id,
        root_dir=result.root_dir,
        manifest=contracts.InstanceBackupManifest.model_validate(
            result.manifest.model_dump(mode="json")
        ),
        registry_status=result.registry_status,
    )


def relocate_instance(
    instance_id: str,
    *,
    to_dir: str,
    remove_source: bool = False,
) -> contracts.InstanceRelocateResult:
    """Move a healthy governed instance to a new directory, preserving identity.

    Orchestrates the daemon-only steps the CLI cannot do alone: back up the
    loaded instance while healthy, restore it at *to_dir*, repoint the registry,
    and swap the manager entry to the relocated instance object. If the backup
    or restore fails the original instance stays loaded and registered; only on a
    successful restore is the registry repointed and (optionally) the old
    directory removed.
    """
    check_permission("cruxible_instance_relocate", instance_id=instance_id)
    registry = get_registry()
    manager = get_manager()

    record = registry.get(instance_id)
    if record is not None and record.backend != GOVERNED_DAEMON_BACKEND:
        raise ConfigError(
            f"Instance '{instance_id}' is registered with unsupported backend '{record.backend}'"
        )

    target_root = Path(to_dir).expanduser()
    validate_root_dir(str(target_root))
    resolved_target = target_root.resolve()

    # Refuse a target that collides with ANY other registered instance, not just
    # an exact location match. A target that equals, is nested inside, or contains
    # another instance's resolved location creates overlapping managed trees: a
    # later --remove-source of either instance could delete the other. We check
    # every governed registry row except the one being relocated.
    for other in registry.list_governed_instances():
        if other.instance_id == instance_id:
            continue
        other_root = Path(other.location).expanduser().resolve()
        overlap = paths_overlap(resolved_target, other_root)
        if overlap == "same":
            raise ConfigError(
                f"Relocate target {target_root} is the registered location of instance "
                f"'{other.instance_id}'"
            )
        if overlap == "nested_inside":
            raise ConfigError(
                f"Relocate target {target_root} is nested inside the registered location "
                f"of instance '{other.instance_id}' ({other_root})"
            )
        if overlap == "contains":
            raise ConfigError(
                f"Relocate target {target_root} contains the registered location of "
                f"instance '{other.instance_id}' ({other_root})"
            )

    # Capture the existing workspace_root BEFORE the relocate so we can preserve
    # it across the registry update. update_governed_instance_location defaults
    # workspace_root to None and writes it unconditionally, so passing it through
    # is what keeps server-mode reload / source-artifact path resolution pointing
    # at the caller's workspace rather than falling back to the daemon root.
    existing_workspace_root = record.workspace_root if record is not None else None

    # Resolve and back up the live instance while it is still healthy. A failure
    # to back up/restore raises here with the original instance untouched. The
    # service never removes the source; that happens below, only after the
    # registry and manager have been swapped to the relocated instance.
    instance = manager.get(instance_id)
    source_root = instance.get_root_path()
    result = service_relocate_instance(
        instance,
        instance_id=instance_id,
        to_dir=target_root,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    )

    # Restore succeeded: repoint the registry and swap the live manager entry so
    # the relocated instance object becomes canonical for this ID. Preserve the
    # workspace_root captured above (newly created rows have none to preserve).
    new_root = Path(result.to_dir)
    if record is None:
        created = registry.create_governed_instance_with_id(instance_id)
        if Path(created.record.location) != new_root:
            registry.update_governed_instance_location(
                instance_id, new_root, workspace_root=existing_workspace_root
            )
    else:
        registry.update_governed_instance_location(
            instance_id, new_root, workspace_root=existing_workspace_root
        )
    manager.register(instance_id, result.instance)

    # Only now, after the registry points at the new location and the manager
    # serves the relocated instance, is it safe to remove the old directory. A
    # crash before this point leaves the source as a usable fallback. The
    # relocation is already logically complete here, so a cleanup failure must
    # NOT be raised to the client: report source_removed=False and log instead of
    # propagating, rather than turning a successful relocate into a false failure.
    source_removed = False
    if remove_source and source_root.resolve() != new_root.resolve():
        try:
            shutil.rmtree(source_root, ignore_errors=False)
            source_removed = True
        except OSError:
            logger.warning(
                "Relocate of instance %s succeeded but removing the old source "
                "directory %s failed; leaving it on disk (source_removed=False)",
                instance_id,
                source_root,
                exc_info=True,
            )

    return contracts.InstanceRelocateResult(
        instance_id=result.instance_id,
        from_dir=result.from_dir,
        to_dir=result.to_dir,
        manifest=contracts.InstanceBackupManifest.model_validate(
            result.manifest.model_dump(mode="json")
        ),
        source_removed=source_removed,
        registry_status=result.registry_status,
    )


def _registry_record_points_to_healthy_instance(root: Path) -> bool:
    try:
        loaded = CruxibleInstance.load(root)
    except Exception:
        return False
    return loaded.is_governed_mode()


def create_decision_record(
    instance_id: str,
    *,
    question: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    opened_by: str = "human",
    actor_context: Any | None = None,
) -> contracts.DecisionRecordResult:
    check_permission("cruxible_create_decision_record", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_create_decision_record(
        instance,
        question=question,
        subject_type=subject_type,
        subject_id=subject_id,
        opened_by=opened_by,
        actor_context=actor,
    )
    return contracts.DecisionRecordResult(record=result.record.model_dump(mode="json"))


def get_decision_record(
    instance_id: str,
    decision_record_id: str,
    *,
    include_events: bool = True,
) -> contracts.DecisionRecordResult:
    check_permission("cruxible_get_decision_record", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_get_decision_record(
        instance,
        decision_record_id,
        include_events=include_events,
    )
    return contracts.DecisionRecordResult(
        record=result.record.model_dump(mode="json"),
        events=[event.model_dump(mode="json") for event in result.events],
    )


def list_decision_records(
    instance_id: str,
    *,
    status: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    decision_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.DecisionRecordListResult:
    check_permission("cruxible_list_decision_records", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_list_decision_records(
        instance,
        status=status,
        subject_type=subject_type,
        subject_id=subject_id,
        decision_class=decision_class,
        limit=limit,
        offset=offset,
    )
    records = [record.model_dump(mode="json") for record in result.items]
    return contracts.DecisionRecordListResult(
        items=records,
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(records) < result.total,
    )


def list_decision_events(
    instance_id: str,
    *,
    decision_record_id: str | None = None,
    receipt_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.DecisionEventListResult:
    check_permission("cruxible_list_decision_events", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_list_decision_events(
        instance,
        decision_record_id=decision_record_id,
        receipt_id=receipt_id,
        trace_id=trace_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    events = [event.model_dump(mode="json") for event in result.items]
    return contracts.DecisionEventListResult(
        items=events,
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(events) < result.total,
    )


def finalize_decision_record(
    instance_id: str,
    decision_record_id: str,
    *,
    final_decision: str,
    decision_class: str,
    rationale: str = "",
    actor_context: Any | None = None,
) -> contracts.DecisionRecordResult:
    check_permission("cruxible_finalize_decision_record", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_finalize_decision_record(
        instance,
        decision_record_id,
        final_decision=final_decision,
        decision_class=decision_class,  # type: ignore[arg-type]
        rationale=rationale,
        actor_context=actor,
    )
    return contracts.DecisionRecordResult(
        record=result.record.model_dump(mode="json"),
        events=[event.model_dump(mode="json") for event in result.events],
    )


def abandon_decision_record(
    instance_id: str,
    decision_record_id: str,
    *,
    reason: str = "",
    actor_context: Any | None = None,
) -> contracts.DecisionRecordResult:
    check_permission("cruxible_abandon_decision_record", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_abandon_decision_record(
        instance,
        decision_record_id,
        reason=reason,
        actor_context=actor,
    )
    return contracts.DecisionRecordResult(
        record=result.record.model_dump(mode="json"),
        events=[event.model_dump(mode="json") for event in result.events],
    )


def list_snapshots(
    instance_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> contracts.SnapshotListResult:
    """List immutable snapshots for an instance."""
    check_permission("cruxible_list_snapshots", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_list_snapshots(instance, limit=limit, offset=offset)
    snapshots = [
        contracts.SnapshotMetadata.model_validate(snapshot.model_dump(mode="json"))
        for snapshot in result.items
    ]
    return contracts.SnapshotListResult(
        items=snapshots,
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(snapshots) < result.total,
    )


def clone_snapshot_local(
    instance_id: str,
    snapshot_id: str,
    root_dir: str,
) -> contracts.CloneSnapshotResult:
    """Create a new local instance from a selected snapshot."""
    check_permission("cruxible_clone_snapshot", instance_id=instance_id)
    validate_root_dir(root_dir)
    instance = get_manager().get(instance_id)
    result = service_clone_snapshot(instance, snapshot_id, root_dir)
    registered = get_registry().get_or_create_local_instance(Path(root_dir))
    get_manager().register(registered.record.instance_id, result.instance)
    return contracts.CloneSnapshotResult(
        instance_id=registered.record.instance_id,
        snapshot=contracts.SnapshotMetadata.model_validate(result.snapshot.model_dump(mode="json")),
    )


def clone_snapshot_governed(
    instance_id: str,
    snapshot_id: str,
    root_dir: str,
) -> contracts.CloneSnapshotResult:
    """Create a new daemon-owned governed instance from a selected snapshot."""
    check_permission("cruxible_clone_snapshot", instance_id=instance_id)
    validate_root_dir(root_dir)
    instance = get_manager().get(instance_id)
    # Mirror hosted init: clone into the reserved location first and register the
    # row only on success, so a refused/failed clone leaves neither a stale
    # registry row nor a partial instance root behind.
    registry = get_registry()
    clone_instance_id = registry.generate_governed_instance_id()
    clone_root = registry.governed_instance_location(clone_instance_id)
    try:
        result = service_clone_snapshot(
            instance,
            snapshot_id,
            clone_root,
            instance_mode=CruxibleInstance.GOVERNED_MODE,
        )
    except Exception:
        shutil.rmtree(clone_root, ignore_errors=True)
        raise
    registered = registry.create_governed_instance_with_id(
        clone_instance_id, workspace_root=root_dir
    )
    get_manager().register(registered.record.instance_id, result.instance)
    return contracts.CloneSnapshotResult(
        instance_id=registered.record.instance_id,
        snapshot=contracts.SnapshotMetadata.model_validate(result.snapshot.model_dump(mode="json")),
    )


def query(
    instance_id: str,
    query_name: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    *,
    offset: int = 0,
    relationship_state: contracts.QueryVisibilityState | None = None,
    decision_record_id: str | None = None,
    surface: str = "local",
) -> contracts.QueryToolResult:
    """Execute a named query."""
    check_permission("cruxible_query", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_query_surface(
        instance,
        query_name,
        params or {},
        limit=limit,
        offset=offset,
        relationship_state=relationship_state,
        context=_operation_context(decision_record_id, surface=surface),
    )

    include_receipt = limit is None and offset == 0

    return _query_tool_result(result, include_receipt=include_receipt)


def query_inline(
    instance_id: str,
    definition: contracts.InlineQueryDefinition | dict[str, Any],
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    *,
    relationship_state: contracts.QueryVisibilityState | None = None,
    decision_record_id: str | None = None,
    surface: str = "local",
) -> contracts.QueryToolResult:
    """Execute a bounded inline query definition without persisting it to config."""
    check_permission("cruxible_query_inline", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    definition_payload = (
        definition.model_dump(mode="python", exclude_none=True)
        if isinstance(definition, BaseModel)
        else dict(definition)
    )
    result = service_query_inline_surface(
        instance,
        definition_payload,
        params or {},
        limit=limit,
        relationship_state=relationship_state,
        context=_operation_context(decision_record_id, surface=surface),
    )

    include_receipt = limit is None
    return _query_tool_result(result, include_receipt=include_receipt)


def _query_tool_result(
    result: QueryServiceResult,
    *,
    include_receipt: bool,
) -> contracts.QueryToolResult:
    return contracts.QueryToolResult(
        items=cast(
            list[contracts.QueryItem],
            [dump_query_row(row, include_source=True, mode="json") for row in result.items],
        ),
        receipt_id=result.receipt_id,
        receipt=(
            result.receipt.model_dump(mode="json") if result.receipt and include_receipt else None
        ),
        total=result.total,
        limit=result.limit,
        offset=result.offset,
        truncated=result.truncated,
        limit_truncated=result.limit_truncated,
        path_truncated=result.path_truncated,
        truncation_reasons=result.truncation_reasons,
        max_paths=result.max_paths,
        max_paths_per_result=result.max_paths_per_result,
        total_path_count=result.total_path_count,
        retained_path_count=result.retained_path_count,
        steps_executed=result.steps_executed,
        result_shape=result.result_shape,
        dedupe=result.dedupe,
        relationship_state=result.relationship_state,
        policy_summary=result.policy_summary,
        param_hints=(
            contracts.QueryParamHints(
                entry_point=result.param_hints.entry_point,
                required_params=result.param_hints.required_params,
                primary_key=result.param_hints.primary_key,
                example_ids=result.param_hints.example_ids,
            )
            if result.param_hints is not None
            else None
        ),
    )


def receipt(instance_id: str, receipt_id: str) -> dict[str, Any]:
    """Retrieve a stored receipt by ID."""
    check_permission("cruxible_receipt", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    receipt = service_get_receipt(instance, receipt_id)
    return receipt.model_dump(mode="json")


def explain_receipt(
    instance_id: str,
    receipt_id: str,
    *,
    format: contracts.ReceiptExplanationFormat = "markdown",
) -> contracts.ReceiptExplanationResult:
    """Render a stored receipt in a user-facing explanation format."""
    check_permission("cruxible_receipt", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_explain_receipt(instance, receipt_id, format=format)
    return contracts.ReceiptExplanationResult(
        receipt_id=result.receipt_id,
        format=result.format,
        content=result.content,
    )


def get_trace(instance_id: str, trace_id: str) -> dict[str, Any]:
    """Retrieve a stored provider execution trace by ID."""
    check_permission("cruxible_get_trace", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    trace = service_get_trace(instance, trace_id)
    return trace.model_dump(mode="json")


def list_traces(
    instance_id: str,
    *,
    workflow_name: str | None = None,
    provider_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.TraceListResult:
    """List provider execution trace summaries."""
    check_permission("cruxible_list_traces", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_list_traces(
        instance,
        workflow_name=workflow_name,
        provider_name=provider_name,
        limit=limit,
        offset=offset,
    )
    return contracts.TraceListResult(
        items=result.items,
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(result.items) < result.total,
    )


def feedback(
    instance_id: str,
    action: contracts.FeedbackAction,
    source: contracts.FeedbackSource,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
    receipt_id: str | None = None,
    actor_context: Any | None = None,
) -> contracts.FeedbackResult:
    """Record feedback on an edge."""
    check_permission("cruxible_feedback", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    _require_review_promotion_actor(action, actor)
    instance = get_manager().get(instance_id)

    target = RelationshipTargetInput(
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )
    result = service_feedback_input(
        instance,
        FeedbackItemInput(
            receipt_id=receipt_id,
            action=action,
            target=target,
            reason=reason,
            reason_code=reason_code,
            scope_hints=scope_hints,
            corrections=corrections,
            group_override=group_override,
        ),
        source=source,
        actor_context=actor,
    )
    return contracts.FeedbackResult(
        feedback_id=result.feedback_id,
        applied=result.applied,
        receipt_id=result.receipt_id,
    )


def feedback_batch(
    instance_id: str,
    items: list[contracts.FeedbackBatchItemInput],
    *,
    source: contracts.FeedbackSource,
    actor_context: Any | None = None,
) -> contracts.FeedbackBatchResult:
    """Record batch edge feedback tied to prior receipts."""
    check_permission("cruxible_feedback_batch", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    for item in items:
        _require_review_promotion_actor(item.action, actor)
    instance = get_manager().get(instance_id)
    result = service_feedback_batch_inputs(
        instance,
        [
            FeedbackItemInput(
                receipt_id=item.receipt_id,
                action=item.action,
                target=RelationshipTargetInput(
                    from_type=item.target.from_type,
                    from_id=item.target.from_id,
                    relationship_type=item.target.relationship_type,
                    to_type=item.target.to_type,
                    to_id=item.target.to_id,
                    edge_key=item.target.edge_key,
                ),
                reason=item.reason,
                reason_code=item.reason_code,
                scope_hints=item.scope_hints,
                corrections=item.corrections or {},
                group_override=item.group_override,
            )
            for item in items
        ],
        source=source,
        actor_context=actor,
    )
    return contracts.FeedbackBatchResult(
        feedback_ids=result.feedback_ids,
        applied_count=result.applied_count,
        total=result.total,
        receipt_id=result.receipt_id,
    )


def feedback_from_query(
    instance_id: str,
    *,
    receipt_id: str,
    result_index: int,
    action: contracts.FeedbackAction,
    source: contracts.FeedbackSource = "human",
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
    path_index: int | None = None,
    path_alias: str | None = None,
    actor_context: Any | None = None,
) -> contracts.FeedbackResult:
    """Record edge feedback by selecting relationship evidence from a query receipt."""
    check_permission("cruxible_feedback_from_query", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    _require_review_promotion_actor(action, actor)
    instance = get_manager().get(instance_id)
    result = service_feedback_from_query_result(
        instance,
        receipt_id=receipt_id,
        result_index=result_index,
        action=action,
        source=source,
        reason=reason,
        reason_code=reason_code,
        scope_hints=scope_hints,
        corrections=corrections,
        group_override=group_override,
        path_index=path_index,
        path_alias=path_alias,
        actor_context=actor,
    )
    return contracts.FeedbackResult(
        feedback_id=result.feedback_id,
        applied=result.applied,
        receipt_id=result.receipt_id,
    )


def outcome(
    instance_id: str,
    receipt_id: str | None,
    outcome: contracts.OutcomeValue,
    anchor_type: contracts.OutcomeAnchorType = "receipt",
    anchor_id: str | None = None,
    source: contracts.FeedbackSource = "human",
    outcome_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    outcome_profile_key: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_context: Any | None = None,
) -> contracts.OutcomeResult:
    """Record a structured outcome for a prior receipt or proposal resolution."""
    check_permission("cruxible_outcome", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_outcome(
        instance,
        receipt_id=receipt_id,
        outcome=outcome,
        anchor_type=anchor_type,
        anchor_id=anchor_id,
        source=source,
        outcome_code=outcome_code,
        scope_hints=scope_hints,
        outcome_profile_key=outcome_profile_key,
        detail=detail,
        actor_context=actor,
    )
    return contracts.OutcomeResult(outcome_id=result.outcome_id)


def list_resources(
    instance_id: str,
    resource_type: contracts.ResourceType,
    entity_type: str | None = None,
    relationship_type: str | None = None,
    query_name: str | None = None,
    receipt_id: str | None = None,
    limit: int = 50,
    property_filter: dict[str, Any] | None = None,
    where: dict[str, dict[str, Any]] | None = None,
    operation_type: str | None = None,
    fields: list[str] | None = None,
    offset: int = 0,
    relationship_state: contracts.QueryVisibilityState | None = None,
) -> contracts.ListResult:
    """List entities, edges, receipts, feedback, or outcomes."""
    check_permission("cruxible_list", instance_id=instance_id)
    instance = get_manager().get(instance_id)

    result = service_list(
        instance,
        resource_type,
        entity_type=entity_type,
        relationship_type=relationship_type,
        query_name=query_name,
        receipt_id=receipt_id,
        property_filter=property_filter,
        where=where,
        operation_type=operation_type,
        fields=fields,
        relationship_state=relationship_state,
        limit=limit,
        offset=offset,
    )

    if resource_type in ("entities", "feedback", "outcomes"):
        items = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in result.items
        ]
    else:
        items = result.items

    return contracts.ListResult(
        items=items,
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(items) < result.total,
    )


def evaluate(
    instance_id: str,
    max_findings: int = 100,
    exclude_orphan_types: list[str] | None = None,
    severity_filter: list[contracts.FindingSeverity] | None = None,
    category_filter: list[contracts.FindingCategory] | None = None,
) -> contracts.EvaluateResult:
    """Evaluate graph quality."""
    check_permission("cruxible_evaluate", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    report = service_evaluate(
        instance,
        max_findings=max_findings,
        exclude_orphan_types=exclude_orphan_types,
        severity_filter=severity_filter,
        category_filter=category_filter,
    )
    return contracts.EvaluateResult(
        entity_count=report.entity_count,
        edge_count=report.edge_count,
        findings=[finding.model_dump(mode="json") for finding in report.findings],
        summary=report.summary,
        constraint_summary=report.constraint_summary,
        quality_summary=report.quality_summary,
    )


def state_health(instance_id: str) -> contracts.StateHealthResult:
    """Aggregate read-only, deterministic state-health maintenance signals."""
    check_permission("cruxible_state_health", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_state_health(instance)
    return contracts.StateHealthResult(
        captured_at=result.captured_at,
        head_snapshot_id=result.head_snapshot_id,
        groups=contracts.StateHealthGroupsSection(
            pending_review_count=result.groups.pending_review_count,
            applying_count=result.groups.applying_count,
            auto_resolved_count=result.groups.auto_resolved_count,
            resolved_count=result.groups.resolved_count,
            total_count=result.groups.total_count,
            oldest_unresolved_age_seconds=result.groups.oldest_unresolved_age_seconds,
            newest_unresolved_age_seconds=result.groups.newest_unresolved_age_seconds,
        ),
        signals=contracts.StateHealthSignalsSection(
            unevidenced_support_by_source=result.signals.unevidenced_support_by_source,
        ),
        provenance=contracts.StateHealthProvenanceSection(
            direct_write_edge_count=result.provenance.direct_write_edge_count,
            group_backed_edge_count=result.provenance.group_backed_edge_count,
            other_source_edge_count=result.provenance.other_source_edge_count,
            total_edge_count=result.provenance.total_edge_count,
        ),
        freshness=contracts.StateHealthFreshnessSection(
            source_artifact_count=result.freshness.source_artifact_count,
            oldest_source_artifact_age_seconds=(
                result.freshness.oldest_source_artifact_age_seconds
            ),
            provider_trace_count=result.freshness.provider_trace_count,
            oldest_provider_trace_age_seconds=(result.freshness.oldest_provider_trace_age_seconds),
            config_compatible=result.freshness.config_compatible,
            config_warnings=result.freshness.config_warnings,
        ),
        integrity=contracts.StateHealthIntegritySection(
            orphan_entity_count=result.integrity.orphan_entity_count,
            unused_entity_types=result.integrity.unused_entity_types,
            unused_relationship_types=result.integrity.unused_relationship_types,
            configuration_locked=result.integrity.configuration_locked,
        ),
    )


def lint(
    instance_id: str,
    *,
    max_findings: int = 100,
    analysis_limit: int = 200,
    min_support: int = 5,
    exclude_orphan_types: list[str] | None = None,
) -> contracts.LintResult:
    """Run the aggregate read-only lint pass."""
    check_permission("cruxible_lint", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_lint(
        instance,
        max_findings=max_findings,
        analysis_limit=analysis_limit,
        min_support=min_support,
        exclude_orphan_types=exclude_orphan_types,
    )
    report = result.evaluation
    return contracts.LintResult(
        config_name=result.config_name,
        config_warnings=result.config_warnings,
        compatibility_warnings=result.compatibility_warnings,
        evaluation=contracts.EvaluateResult(
            entity_count=report.entity_count,
            edge_count=report.edge_count,
            findings=[f.model_dump(mode="json") for f in report.findings],
            summary=report.summary,
            constraint_summary=report.constraint_summary,
            quality_summary=report.quality_summary,
        ),
        feedback_reports=[_analyze_feedback_contract(report) for report in result.feedback_reports],
        outcome_reports=[_analyze_outcomes_contract(report) for report in result.outcome_reports],
        summary=contracts.LintSummary(
            config_warning_count=result.summary.config_warning_count,
            compatibility_warning_count=result.summary.compatibility_warning_count,
            evaluation_finding_count=result.summary.evaluation_finding_count,
            feedback_report_count=result.summary.feedback_report_count,
            feedback_issue_count=result.summary.feedback_issue_count,
            outcome_report_count=result.summary.outcome_report_count,
            outcome_issue_count=result.summary.outcome_issue_count,
        ),
        has_issues=result.has_issues,
    )


def schema(instance_id: str) -> dict[str, Any]:
    """Get config schema details."""
    check_permission("cruxible_schema", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    config = service_schema(instance)
    return schema_wire_payload(config)


def list_queries(
    instance_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> contracts.QueryListResult:
    """List named-query definitions for an instance, ordered by name."""
    check_permission("cruxible_list_queries", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    queries = service_list_queries(instance)
    total = len(queries)
    end = None if limit is None else offset + limit
    page = queries[offset:end]
    return contracts.QueryListResult(
        total=total,
        limit=limit,
        offset=offset,
        truncated=offset + len(page) < total,
        items=[
            contracts.NamedQueryInfoResult(
                name=query.name,
                mode=query.mode,
                entry_point=query.entry_point,
                required_params=query.required_params,
                returns=query.returns,
                result_shape=query.result_shape,
                dedupe=query.dedupe,
                relationship_state=query.relationship_state,
                allow_relationship_state_override=query.allow_relationship_state_override,
                select=query.select,
                order_by=query.order_by,
                include=query.include,
                limit=query.limit,
                max_paths=query.max_paths,
                max_paths_per_result=query.max_paths_per_result,
                description=query.description,
                example_ids=query.example_ids,
            )
            for query in page
        ],
    )


def describe_query(
    instance_id: str,
    query_name: str,
) -> contracts.NamedQueryInfoResult:
    """Describe one named-query surface for an instance."""
    check_permission("cruxible_describe_query", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    query = service_describe_query(instance, query_name)
    return contracts.NamedQueryInfoResult(
        name=query.name,
        mode=query.mode,
        entry_point=query.entry_point,
        required_params=query.required_params,
        returns=query.returns,
        result_shape=query.result_shape,
        dedupe=query.dedupe,
        relationship_state=query.relationship_state,
        allow_relationship_state_override=query.allow_relationship_state_override,
        select=query.select,
        order_by=query.order_by,
        include=query.include,
        limit=query.limit,
        max_paths=query.max_paths,
        max_paths_per_result=query.max_paths_per_result,
        description=query.description,
        example_ids=query.example_ids,
    )


def get_feedback_profile(
    instance_id: str,
    relationship_type: str,
) -> contracts.FeedbackProfileResult:
    """Return one configured feedback profile, if present."""
    check_permission("cruxible_get_feedback_profile", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    profile = service_get_feedback_profile(instance, relationship_type)
    if profile is None:
        return contracts.FeedbackProfileResult(
            found=False,
            relationship_type=relationship_type,
        )
    return contracts.FeedbackProfileResult(
        found=True,
        relationship_type=relationship_type,
        profile=profile.model_dump(mode="json"),
    )


def get_outcome_profile(
    instance_id: str,
    *,
    anchor_type: contracts.OutcomeAnchorType,
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
) -> contracts.OutcomeProfileResult:
    """Return one configured outcome profile for an anchor context, if present."""
    check_permission("cruxible_get_outcome_profile", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    profile_key, profile = service_get_outcome_profile(
        instance,
        anchor_type=anchor_type,
        relationship_type=relationship_type,
        workflow_name=workflow_name,
        surface_type=surface_type,
        surface_name=surface_name,
    )
    if profile is None:
        return contracts.OutcomeProfileResult(
            found=False,
            profile_key=None,
            anchor_type=anchor_type,
        )
    return contracts.OutcomeProfileResult(
        found=True,
        profile_key=profile_key,
        anchor_type=anchor_type,
        profile=profile.model_dump(mode="json"),
    )


def analyze_feedback(
    instance_id: str,
    relationship_type: str,
    *,
    limit: int = 200,
    min_support: int = 5,
    decision_surface_type: str | None = None,
    decision_surface_name: str | None = None,
    property_pairs: list[contracts.PropertyPairInput] | None = None,
) -> contracts.AnalyzeFeedbackResult:
    """Analyze structured feedback into deterministic remediation suggestions."""
    check_permission("cruxible_analyze_feedback", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_analyze_feedback(
        instance,
        relationship_type,
        limit=limit,
        min_support=min_support,
        decision_surface_type=decision_surface_type,
        decision_surface_name=decision_surface_name,
        property_pairs=(
            [(pair.from_property, pair.to_property) for pair in property_pairs]
            if property_pairs
            else None
        ),
    )
    return _analyze_feedback_contract(result)


def _analyze_feedback_contract(result: AnalyzeFeedbackResult) -> contracts.AnalyzeFeedbackResult:
    """Convert a service feedback analysis result into the shared daemon contract."""
    return contracts.AnalyzeFeedbackResult(
        relationship_type=result.relationship_type,
        feedback_count=result.feedback_count,
        action_counts=result.action_counts,
        source_counts=result.source_counts,
        reason_code_counts=result.reason_code_counts,
        coded_groups=[
            contracts.FeedbackGroupSummary(
                relationship_type=group.relationship_type,
                reason_code=group.reason_code,
                remediation_hint=group.remediation_hint,
                decision_context=group.decision_context,
                scope_hints=group.scope_hints,
                feedback_count=group.feedback_count,
                feedback_ids=group.feedback_ids,
                sample_reasons=group.sample_reasons,
            )
            for group in result.coded_groups
        ],
        uncoded_feedback_count=result.uncoded_feedback_count,
        uncoded_examples=[
            contracts.UncodedFeedbackExample(
                feedback_id=example.feedback_id,
                relationship_type=example.relationship_type,
                reason=example.reason,
                decision_context=example.decision_context,
                scope_hints=example.scope_hints,
                target=example.target.model_dump(mode="json"),
            )
            for example in result.uncoded_examples
        ],
        constraint_suggestions=[
            contracts.ConstraintSuggestion(
                name=suggestion.name,
                description=suggestion.description,
                relationship_type=suggestion.relationship_type,
                rule=suggestion.rule,
                severity=suggestion.severity,
                support_count=suggestion.support_count,
                feedback_ids=suggestion.feedback_ids,
                sample_value_pairs=suggestion.sample_value_pairs,
            )
            for suggestion in result.constraint_suggestions
        ],
        decision_policy_suggestions=[
            contracts.DecisionPolicySuggestion(
                name=suggestion.name,
                description=suggestion.description,
                relationship_type=suggestion.relationship_type,
                applies_to=suggestion.applies_to,
                effect=suggestion.effect,
                rationale=suggestion.rationale,
                match=suggestion.match,
                query_name=suggestion.query_name,
                workflow_name=suggestion.workflow_name,
                support_count=suggestion.support_count,
                feedback_ids=suggestion.feedback_ids,
            )
            for suggestion in result.decision_policy_suggestions
        ],
        quality_check_candidates=[
            contracts.QualityCheckCandidate(
                relationship_type=candidate.relationship_type,
                reason_code=candidate.reason_code,
                support_count=candidate.support_count,
                description=candidate.description,
                feedback_ids=candidate.feedback_ids,
            )
            for candidate in result.quality_check_candidates
        ],
        provider_fix_candidates=[
            contracts.ProviderFixCandidate(
                relationship_type=candidate.relationship_type,
                reason_code=candidate.reason_code,
                support_count=candidate.support_count,
                description=candidate.description,
                feedback_ids=candidate.feedback_ids,
            )
            for candidate in result.provider_fix_candidates
        ],
        warnings=result.warnings,
    )


def analyze_outcomes(
    instance_id: str,
    *,
    anchor_type: contracts.OutcomeAnchorType,
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    query_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
    limit: int = 200,
    min_support: int = 5,
) -> contracts.AnalyzeOutcomesResult:
    """Analyze structured outcomes into trust and debugging suggestions."""
    check_permission("cruxible_analyze_outcomes", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_analyze_outcomes(
        instance,
        anchor_type=anchor_type,
        relationship_type=relationship_type,
        workflow_name=workflow_name,
        query_name=query_name,
        surface_type=surface_type,
        surface_name=surface_name,
        limit=limit,
        min_support=min_support,
    )
    return _analyze_outcomes_contract(result)


def _analyze_outcomes_contract(result: AnalyzeOutcomesResult) -> contracts.AnalyzeOutcomesResult:
    """Convert a service outcome analysis result into the shared daemon contract."""
    return contracts.AnalyzeOutcomesResult(
        anchor_type=result.anchor_type,
        outcome_count=result.outcome_count,
        outcome_counts=result.outcome_counts,
        outcome_code_counts=result.outcome_code_counts,
        coded_groups=[
            contracts.OutcomeGroupSummary(
                anchor_type=group.anchor_type,
                outcome_code=group.outcome_code,
                remediation_hint=group.remediation_hint,
                decision_context=group.decision_context,
                scope_hints=group.scope_hints,
                outcome_count=group.outcome_count,
                outcome_counts=group.outcome_counts,
                outcome_ids=group.outcome_ids,
            )
            for group in result.coded_groups
        ],
        uncoded_outcome_count=result.uncoded_outcome_count,
        uncoded_examples=[
            contracts.UncodedOutcomeExample(
                outcome_id=example.outcome_id,
                anchor_type=example.anchor_type,
                anchor_id=example.anchor_id,
                outcome=example.outcome,
                detail=example.detail,
                decision_context=example.decision_context,
                scope_hints=example.scope_hints,
            )
            for example in result.uncoded_examples
        ],
        trust_adjustment_suggestions=[
            contracts.TrustAdjustmentSuggestion(
                resolution_id=suggestion.resolution_id,
                relationship_type=suggestion.relationship_type,
                group_signature=suggestion.group_signature,
                current_trust_status=suggestion.current_trust_status,
                suggested_trust_status=suggestion.suggested_trust_status,
                support_count=suggestion.support_count,
                rationale=suggestion.rationale,
                outcome_ids=suggestion.outcome_ids,
            )
            for suggestion in result.trust_adjustment_suggestions
        ],
        workflow_review_policy_suggestions=[
            contracts.OutcomeDecisionPolicySuggestion(
                name=suggestion.name,
                description=suggestion.description,
                relationship_type=suggestion.relationship_type,
                applies_to=suggestion.applies_to,
                effect=suggestion.effect,
                rationale=suggestion.rationale,
                match=suggestion.match,
                query_name=suggestion.query_name,
                workflow_name=suggestion.workflow_name,
                support_count=suggestion.support_count,
                outcome_ids=suggestion.outcome_ids,
            )
            for suggestion in result.workflow_review_policy_suggestions
        ],
        query_policy_suggestions=[
            contracts.QueryPolicySuggestion(
                surface_name=suggestion.surface_name,
                outcome_code=suggestion.outcome_code,
                support_count=suggestion.support_count,
                description=suggestion.description,
                outcome_ids=suggestion.outcome_ids,
            )
            for suggestion in result.query_policy_suggestions
        ],
        provider_fix_candidates=[
            contracts.OutcomeProviderFixCandidate(
                surface_type=candidate.surface_type,
                surface_name=candidate.surface_name,
                outcome_code=candidate.outcome_code,
                support_count=candidate.support_count,
                description=candidate.description,
                outcome_ids=candidate.outcome_ids,
            )
            for candidate in result.provider_fix_candidates
        ],
        debug_packages=[
            contracts.DebugPackage(
                anchor_id=package.anchor_id,
                outcome_count=package.outcome_count,
                outcome_breakdown=package.outcome_breakdown,
                outcome_code_breakdown=package.outcome_code_breakdown,
                sample_outcome_ids=package.sample_outcome_ids,
                lineage_summary=package.lineage_summary,
                common_providers=package.common_providers,
                common_trace_patterns=package.common_trace_patterns,
            )
            for package in result.debug_packages
        ],
        workflow_debug_packages=[
            contracts.DebugPackage(
                anchor_id=package.anchor_id,
                outcome_count=package.outcome_count,
                outcome_breakdown=package.outcome_breakdown,
                outcome_code_breakdown=package.outcome_code_breakdown,
                sample_outcome_ids=package.sample_outcome_ids,
                lineage_summary=package.lineage_summary,
                common_providers=package.common_providers,
                common_trace_patterns=package.common_trace_patterns,
            )
            for package in result.workflow_debug_packages
        ],
        warnings=result.warnings,
    )


def stats(instance_id: str) -> contracts.StatsResult:
    """Return grouped entity and relationship counts."""
    check_permission("cruxible_stats", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_stats(instance)
    return contracts.StatsResult(
        entity_count=result.entity_count,
        edge_count=result.edge_count,
        entity_counts=result.entity_counts,
        relationship_counts=result.relationship_counts,
        status_counts=result.status_counts,
        head_snapshot_id=result.head_snapshot_id,
    )


def inspect_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
    *,
    direction: str = "both",
    relationship_type: str | None = None,
    limit: int | None = None,
) -> contracts.InspectEntityResult:
    """Inspect an entity and its immediate neighbors."""
    check_permission("cruxible_inspect_entity", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_inspect_entity(
        instance,
        entity_type,
        entity_id,
        direction=direction,  # type: ignore[arg-type]
        relationship_type=relationship_type,
        limit=limit,
    )
    return contracts.InspectEntityResult(
        found=result.found,
        entity_type=result.entity_type,
        entity_id=result.entity_id,
        properties=result.properties,
        metadata=result.metadata,
        neighbors=[
            contracts.InspectNeighborResult(
                direction=neighbor.direction,
                relationship_type=neighbor.relationship_type,
                edge_key=neighbor.edge_key,
                properties=neighbor.properties,
                metadata=neighbor.metadata,
                entity=neighbor.entity.model_dump(mode="json") if neighbor.entity else {},
            )
            for neighbor in result.neighbors
        ],
        total_neighbors=result.total_neighbors,
    )


def inspect_entity_history(
    instance_id: str,
    entity_type: str,
    *,
    entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> contracts.EntityChangeHistoryResult:
    """Inspect receipt-derived entity change history for one entity type or entity."""
    check_permission("cruxible_inspect_entity_history", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_get_entity_change_history(
        instance,
        entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    return contracts.EntityChangeHistoryResult(
        entity_type=result.entity_type,
        entity_id=result.entity_id,
        items=[
            contracts.EntityChangeHistoryItem(
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                change_kind=item.change_kind,
                property_changes=[
                    contracts.PropertyChangeItem(
                        property=change.property,
                        from_value=change.from_value,
                        to_value=change.to_value,
                    )
                    for change in item.property_changes
                ],
                changed_at=item.changed_at,
                receipt_id=item.receipt_id,
                operation_type=item.operation_type,
                actor_context=item.actor_context,
            )
            for item in result.items
        ],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
        truncated=result.truncated,
        legacy_entity_write_count=result.legacy_entity_write_count,
        warnings=result.warnings,
    )


def inspect_view(
    instance_id: str,
    view: str,
    *,
    limit: int = 200,
) -> contracts.CanonicalViewResult:
    """Build a canonical structured inspect view."""
    check_permission(f"cruxible_inspect_{view}", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_inspect_view(instance, view, limit=limit)  # type: ignore[arg-type]
    return contracts.CanonicalViewResult(view=result.view, payload=result.payload)


def reload_config(
    instance_id: str,
    config_path: str | None = None,
    config_yaml: str | None = None,
    allow_orphans: bool = False,
) -> contracts.ReloadConfigResult:
    """Validate the current config or repoint the instance to a new config path."""
    check_permission("cruxible_reload_config", instance_id=instance_id)
    config_base_dir: Path | None = None
    if config_yaml is not None:
        record = get_registry().get(instance_id)
        if (
            record is not None
            and record.backend == GOVERNED_DAEMON_BACKEND
            and record.workspace_root is not None
        ):
            config_base_dir = Path(record.workspace_root)
    instance = get_manager().get(instance_id)
    result = service_reload_config(
        instance,
        config_path=config_path,
        config_yaml=config_yaml,
        config_base_dir=config_base_dir,
        allow_orphans=allow_orphans,
    )
    return contracts.ReloadConfigResult(
        config_path=result.config_path,
        updated=result.updated,
        warnings=result.warnings,
        type_delta=contracts.ConfigTypeDelta(**vars(result.type_delta)),
        strandings=contracts.ConfigStrandingReport(**vars(result.strandings)),
    )


def sample(
    instance_id: str,
    entity_type: str,
    limit: int = 5,
    fields: list[str] | None = None,
) -> contracts.SampleResult:
    """Sample entities of a given type."""
    check_permission("cruxible_sample", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    sampled = service_sample(instance, entity_type, limit=limit, fields=fields)
    return contracts.SampleResult(
        items=[entity.model_dump(mode="json") for entity in sampled],
        entity_type=entity_type,
        total=len(sampled),
        limit=limit,
    )


def _direct_write_group_interaction_to_contract(
    interaction: Any,
) -> contracts.DirectWriteGroupInteraction:
    return contracts.DirectWriteGroupInteraction(
        relationship_type=interaction.relationship_type,
        from_type=interaction.from_type,
        from_id=interaction.from_id,
        to_type=interaction.to_type,
        to_id=interaction.to_id,
        group_id=interaction.group_id,
        group_status=interaction.group_status,
        group_signature=interaction.group_signature,
        source_workflow_name=interaction.source_workflow_name,
        edge_key=interaction.edge_key,
    )


def add_relationships_with_provenance(
    instance_id: str,
    relationships: list[contracts.RelationshipInput],
    *,
    provenance_source: str,
    provenance_source_ref: str,
    dry_run: bool = False,
    actor_context: Any | None = None,
) -> contracts.AddRelationshipResult:
    """Add or update one or more relationships in the graph (upsert)."""
    check_permission("cruxible_add_relationship", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)

    inputs = [_relationship_input_to_service(edge) for edge in relationships]
    result = service_add_relationship_inputs(
        instance,
        inputs,
        source=provenance_source,
        source_ref=provenance_source_ref,
        dry_run=dry_run,
        actor_context=actor,
    )
    return contracts.AddRelationshipResult(
        added=result.added,
        updated=result.updated,
        pending_conflicts=[
            _direct_write_group_interaction_to_contract(item) for item in result.pending_conflicts
        ],
        updated_group_backed_edges=[
            _direct_write_group_interaction_to_contract(item)
            for item in result.updated_group_backed_edges
        ],
        receipt_id=result.receipt_id,
    )


def add_relationships(
    instance_id: str,
    relationships: list[contracts.RelationshipInput],
    *,
    dry_run: bool = False,
) -> contracts.AddRelationshipResult:
    """Add or update one or more relationships in the graph (upsert)."""
    return add_relationships_with_provenance(
        instance_id,
        relationships,
        dry_run=dry_run,
        provenance_source="mcp_add",
        provenance_source_ref=SOURCE_REF_ADD_RELATIONSHIP,
    )


def _relationship_input_to_service(
    edge: contracts.RelationshipInput,
) -> RelationshipWriteInput:
    return RelationshipWriteInput(
        from_type=edge.from_type,
        from_id=edge.from_id,
        relationship_type=edge.relationship_type,
        to_type=edge.to_type,
        to_id=edge.to_id,
        properties=edge.properties,
        pending=edge.pending,
        evidence_refs=[
            ref.model_dump(mode="python") if isinstance(ref, BaseModel) else ref
            for ref in edge.evidence_refs
        ],
        source_evidence=[ref.model_dump(mode="python") for ref in edge.source_evidence],
        evidence_rationale=edge.evidence_rationale,
        lifecycle=relationship_lifecycle_state(edge.lifecycle),
    )


def _batch_payload_to_service(
    payload: contracts.BatchDirectWritePayload,
) -> BatchDirectWriteInput:
    return BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type=entity.entity_type,
                entity_id=entity.entity_id,
                properties=entity.properties,
                metadata=entity_metadata_with_lifecycle(entity.metadata, entity.lifecycle),
            )
            for entity in payload.entities
        ],
        relationships=[
            BatchRelationshipWriteInput(
                from_type=edge.from_type,
                from_id=edge.from_id,
                relationship_type=edge.relationship_type,
                to_type=edge.to_type,
                to_id=edge.to_id,
                properties=edge.properties,
                pending=edge.pending,
                evidence_refs=[
                    ref.model_dump(mode="python") if isinstance(ref, BaseModel) else ref
                    for ref in edge.evidence_refs
                ],
                source_evidence=[ref.model_dump(mode="python") for ref in edge.source_evidence],
                evidence_rationale=edge.evidence_rationale,
                shared_evidence_keys=list(edge.shared_evidence_keys),
                lifecycle=relationship_lifecycle_state(edge.lifecycle),
            )
            for edge in payload.relationships
        ],
        shared_evidence={
            key: SharedEvidenceInput(
                evidence_refs=[
                    ref.model_dump(mode="python") if isinstance(ref, BaseModel) else ref
                    for ref in evidence.evidence_refs
                ],
                source_evidence=[ref.model_dump(mode="python") for ref in evidence.source_evidence],
            )
            for key, evidence in payload.shared_evidence.items()
        },
    )


def batch_direct_write(
    instance_id: str,
    payload: contracts.BatchDirectWritePayload,
    *,
    dry_run: bool = False,
    provenance_source: str = "mcp_add",
    provenance_source_ref: str = SOURCE_REF_BATCH_DIRECT_WRITE,
    actor_context: Any | None = None,
) -> contracts.BatchDirectWriteResult:
    """Validate or apply one direct entity/relationship write payload."""
    check_permission("cruxible_add_relationship", instance_id=instance_id)
    check_permission("cruxible_add_entity", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_batch_direct_write(
        instance,
        _batch_payload_to_service(payload),
        dry_run=dry_run,
        source=provenance_source,
        source_ref=provenance_source_ref,
        actor_context=actor,
    )
    return contracts.BatchDirectWriteResult(
        dry_run=result.dry_run,
        valid=result.valid,
        entities_added=result.entities_added,
        entities_updated=result.entities_updated,
        relationships_added=result.relationships_added,
        relationships_updated=result.relationships_updated,
        validation_errors=result.validation_errors,
        validation_warnings=result.validation_warnings,
        evidence_sources_used=result.evidence_sources_used,
        pending_conflicts=[
            _direct_write_group_interaction_to_contract(item) for item in result.pending_conflicts
        ],
        updated_group_backed_edges=[
            _direct_write_group_interaction_to_contract(item)
            for item in result.updated_group_backed_edges
        ],
        receipt_id=result.receipt_id,
    )


def add_entities(
    instance_id: str,
    entities: list[contracts.EntityInput],
    *,
    dry_run: bool = False,
    actor_context: Any | None = None,
) -> contracts.AddEntityResult:
    """Add or update one or more entities in the graph (upsert)."""
    check_permission("cruxible_add_entity", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)

    inputs = [
        EntityWriteInput(
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            properties=entity.properties,
            metadata={
                **entity_metadata_with_lifecycle(entity.metadata, entity.lifecycle),
                **({"actor_context": dump_actor_context(actor)} if actor is not None else {}),
            },
        )
        for entity in entities
    ]
    result = service_add_entity_inputs(
        instance,
        inputs,
        dry_run=dry_run,
        actor_context=actor,
    )
    return contracts.AddEntityResult(
        entities_added=result.added,
        entities_updated=result.updated,
        receipt_id=result.receipt_id,
    )


def add_constraint(
    instance_id: str,
    name: str,
    rule: str,
    severity: contracts.ConstraintSeverity = "warning",
    description: str | None = None,
    actor_context: Any | None = None,
) -> contracts.AddConstraintResult:
    """Add a constraint rule to the config and write back to YAML."""
    check_permission("cruxible_add_constraint", instance_id=instance_id)
    _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_add_constraint(
        instance,
        name=name,
        rule=rule,
        severity=severity,
        description=description,
    )
    return contracts.AddConstraintResult(
        name=result.name,
        added=result.added,
        config_updated=result.config_updated,
        warnings=result.warnings,
    )


def add_decision_policy(
    instance_id: str,
    *,
    name: str,
    applies_to: contracts.DecisionPolicyAppliesTo,
    relationship_type: str,
    effect: contracts.DecisionPolicyEffect,
    match: contracts.DecisionPolicyMatchInput | None = None,
    description: str | None = None,
    rationale: str = "",
    query_name: str | None = None,
    workflow_name: str | None = None,
    expires_at: str | None = None,
    actor_context: Any | None = None,
) -> contracts.AddDecisionPolicyResult:
    """Add a decision policy to the config and write back to YAML."""
    check_permission("cruxible_add_decision_policy", instance_id=instance_id)
    _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_add_decision_policy(
        instance,
        name=name,
        applies_to=applies_to,
        relationship_type=relationship_type,
        effect=effect,
        match=match.model_dump(mode="json", by_alias=True) if match is not None else None,
        description=description,
        rationale=rationale,
        query_name=query_name,
        workflow_name=workflow_name,
        expires_at=expires_at,
    )
    return contracts.AddDecisionPolicyResult(
        name=result.name,
        added=result.added,
        config_updated=result.config_updated,
        warnings=result.warnings,
    )


def get_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
) -> contracts.GetEntityResult:
    """Look up a specific entity by type and ID."""
    check_permission("cruxible_get_entity", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    entity = service_get_entity(instance, entity_type, entity_id)
    if entity is None:
        return contracts.GetEntityResult(found=False, entity_type=entity_type, entity_id=entity_id)
    return contracts.GetEntityResult(
        found=True,
        entity_type=entity.entity_type,
        entity_id=entity.entity_id,
        properties=entity.properties,
        metadata=entity.metadata.to_metadata_dict(),
    )


def get_relationship(
    instance_id: str,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> contracts.GetRelationshipResult:
    """Look up a specific relationship by its endpoints and type."""
    check_permission("cruxible_get_relationship", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    relationship = service_get_relationship(
        instance,
        from_type,
        from_id,
        relationship_type,
        to_type,
        to_id,
        edge_key=edge_key,
    )
    if relationship is None:
        return contracts.GetRelationshipResult(
            found=False,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
        )
    return contracts.GetRelationshipResult(
        found=True,
        from_type=relationship.from_type,
        from_id=relationship.from_id,
        relationship_type=relationship.relationship_type,
        to_type=relationship.to_type,
        to_id=relationship.to_id,
        edge_key=relationship.edge_key,
        properties=relationship.properties,
        metadata=relationship.metadata.model_dump(mode="json", exclude_none=True),
    )


def get_relationship_lineage(
    instance_id: str,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> contracts.RelationshipLineageResult:
    """Look up a relationship and follow group provenance when available."""
    check_permission("cruxible_relationship_lineage", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_get_relationship_lineage(
        instance,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )
    return contracts.RelationshipLineageResult(
        found=result.found,
        relationship=(
            result.relationship.model_dump(mode="json") if result.relationship is not None else None
        ),
        provenance=result.provenance,
        group=result.group.model_dump(mode="json") if result.group is not None else None,
        resolution=(
            result.resolution.model_dump(mode="json") if result.resolution is not None else None
        ),
        source_workflow_receipt_id=result.source_workflow_receipt_id,
        source_trace_ids=result.source_trace_ids,
        warnings=result.warnings,
    )


def propose_group(
    instance_id: str,
    relationship_type: str,
    members: list[contracts.MemberInput],
    thesis_text: str = "",
    thesis_facts: dict[str, Any] | None = None,
    analysis_state: dict[str, Any] | None = None,
    signal_sources_used: list[str] | None = None,
    proposed_by: contracts.GroupProposedBy = "agent",
    suggested_priority: str | None = None,
    actor_context: Any | None = None,
) -> contracts.ProposeGroupToolResult:
    """Propose a candidate group for batch edge review."""
    check_permission("cruxible_propose_group", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)

    service_members = [
        GroupMemberInput(
            from_type=member.from_type,
            from_id=member.from_id,
            to_type=member.to_type,
            to_id=member.to_id,
            relationship_type=member.relationship_type,
            signals=[
                GroupSignalInput(
                    signal_source=signal.signal_source,
                    signal=signal.signal,
                    evidence=signal.evidence,
                    evidence_refs=[
                        ref.model_dump(mode="python") if isinstance(ref, BaseModel) else ref
                        for ref in signal.evidence_refs
                    ],
                    source_evidence=[
                        ref.model_dump(mode="python") for ref in signal.source_evidence
                    ],
                    basis=signal.basis.model_dump(mode="python") if signal.basis else None,
                )
                for signal in member.signals
            ],
            properties=member.properties,
            evidence_refs=[
                ref.model_dump(mode="python") if isinstance(ref, BaseModel) else ref
                for ref in member.evidence_refs
            ],
            source_evidence=[ref.model_dump(mode="python") for ref in member.source_evidence],
            evidence_rationale=member.evidence_rationale,
        )
        for member in members
    ]

    result = service_propose_group_inputs(
        instance,
        relationship_type,
        service_members,
        thesis_text=thesis_text,
        thesis_facts=thesis_facts,
        analysis_state=analysis_state,
        signal_sources_used=signal_sources_used,
        proposed_by=proposed_by,
        suggested_priority=suggested_priority,
        actor_context=actor,
    )
    return contracts.ProposeGroupToolResult(
        group_id=result.group_id,
        signature=result.signature,
        status=result.status,
        review_priority=result.review_priority,
        member_count=result.member_count,
        prior_resolution=(
            result.prior_resolution.model_dump(mode="json")
            if result.prior_resolution is not None
            else None
        ),
        suppressed=result.suppressed,
        suppressed_members=[
            contracts.SuppressedProposalMember(**item.__dict__)
            for item in result.suppressed_members
        ],
        policy_summary=result.policy_summary,
        receipt_id=result.receipt_id,
    )


def list_source_artifacts(
    instance_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> contracts.SourceArtifactListResult:
    """List registered source artifacts for UI browsing."""
    check_permission("cruxible_list_source_artifacts", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_list_source_artifacts(instance, limit=limit, offset=offset)
    return contracts.SourceArtifactListResult.model_validate(result.model_dump(mode="json"))


def get_source_artifact(
    instance_id: str,
    source_artifact_id: str,
) -> contracts.SourceArtifactReadResult:
    """Return one source artifact with ordered chunks and available text."""
    check_permission("cruxible_get_source_artifact", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_get_source_artifact(
        instance,
        source_artifact_id=source_artifact_id,
    )
    return contracts.SourceArtifactReadResult.model_validate(result.model_dump(mode="json"))


def register_source_artifact(
    instance_id: str,
    source_path: str,
    source_kind: contracts.SourceKind = "markdown",
    source_retention: contracts.SourceRetention = "manifest_only",
    original_uri: str | None = None,
    label: str | None = None,
    actor_context: Any | None = None,
    source_artifact_id: str | None = None,
) -> contracts.RegisterSourceArtifactResult:
    """Register a local source artifact for source-backed proposal evidence.

    The resolved source path must stay within the registered workspace root (or
    one of the ``CRUXIBLE_ALLOWED_ROOTS`` if configured). Containment is
    default-deny: an absolute ``source_path`` that escapes the workspace is
    rejected even when ``CRUXIBLE_ALLOWED_ROOTS`` is unset, and the check is
    performed after resolving symlinks and ``..`` traversal.
    """
    check_permission("cruxible_register_source_artifact", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    record = get_registry().get(instance_id)
    workspace_root = (
        Path(record.workspace_root)
        if record is not None and record.workspace_root is not None
        else instance.get_root_path()
    ).resolve()
    # Default-deny containment against the workspace root (and any explicitly
    # configured allowed roots). Covers both absolute and relative source paths
    # and resolves symlinks / ``..`` before the check.
    path = resolve_contained_source_path(source_path, allowed_source_roots=[workspace_root])
    resolved_original_uri = original_uri
    if resolved_original_uri is None:
        try:
            resolved_original_uri = path.relative_to(workspace_root).as_posix()
        except ValueError:
            resolved_original_uri = path.name
    validate_root_dir(str(path))
    result = service_register_source_artifact(
        instance,
        source_path=str(path),
        source_kind=source_kind,
        source_retention=source_retention,
        original_uri=resolved_original_uri,
        label=label,
        actor_context=actor,
        allowed_source_roots=[workspace_root],
        source_artifact_id=source_artifact_id,
    )
    return contracts.RegisterSourceArtifactResult.model_validate(result.model_dump(mode="json"))


def dereference_source_evidence(
    instance_id: str,
    source_artifact_id: str,
    chunk_id: str | None = None,
    heading_path: list[str] | None = None,
    block_selector: str | None = None,
    expected_content_hash: str | None = None,
) -> contracts.DereferenceSourceEvidenceResult:
    """Dereference registered source evidence with drift reporting."""
    check_permission("cruxible_dereference_source_evidence", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_dereference_source_evidence(
        instance,
        source_artifact_id=source_artifact_id,
        chunk_id=chunk_id,
        heading_path=heading_path,
        block_selector=block_selector,
        expected_content_hash=expected_content_hash,
    )
    return contracts.DereferenceSourceEvidenceResult.model_validate(result.model_dump(mode="json"))


def resolve_group(
    instance_id: str,
    group_id: str,
    action: contracts.GroupAction,
    rationale: str = "",
    resolved_by: contracts.GroupResolvedBy = "human",
    expected_pending_version: int | None = None,
    actor_context: Any | None = None,
    stamp_existing: bool = False,
) -> contracts.ResolveGroupToolResult:
    """Resolve a candidate group (approve or reject)."""
    check_permission("cruxible_resolve_group", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)

    result = service_resolve_group(
        instance,
        group_id,
        action,
        rationale=rationale,
        resolved_by=resolved_by,
        expected_pending_version=expected_pending_version,
        actor_context=actor,
        stamp_existing=stamp_existing,
    )
    return contracts.ResolveGroupToolResult(
        group_id=result.group_id,
        action=result.action,
        edges_created=result.edges_created,
        edges_skipped=result.edges_skipped,
        resolution_id=result.resolution_id,
        receipt_id=result.receipt_id,
        skipped_members=result.skipped_members,
        edges_stamped=result.edges_stamped,
    )


def update_trust_status(
    instance_id: str,
    resolution_id: str,
    trust_status: contracts.GroupTrustStatus,
    reason: str = "",
    actor_context: Any | None = None,
) -> contracts.UpdateTrustStatusToolResult:
    """Update trust status on a resolution."""
    check_permission("cruxible_update_trust_status", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)

    result = service_update_trust_status(
        instance,
        resolution_id,
        trust_status,
        reason=reason,
        actor_context=actor,
    )
    return contracts.UpdateTrustStatusToolResult(
        resolution_id=result.resolution_id,
        trust_status=result.trust_status,
        receipt_id=result.receipt_id,
    )


def get_group(
    instance_id: str,
    group_id: str,
) -> contracts.GetGroupToolResult:
    """Get a candidate group with its members."""
    check_permission("cruxible_get_group", instance_id=instance_id)
    instance = get_manager().get(instance_id)

    result = service_get_group(instance, group_id)
    return contracts.GetGroupToolResult(
        group=result.group.model_dump(mode="json"),
        members=[member.model_dump(mode="json") for member in result.members],
        resolution=(
            result.resolution.model_dump(mode="json") if result.resolution is not None else None
        ),
        bucket_status=asdict(result.bucket_status) if result.bucket_status is not None else None,
        member_review=[asdict(item) for item in result.member_review],
    )


def list_groups(
    instance_id: str,
    relationship_type: str | None = None,
    status: contracts.GroupStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> contracts.ListGroupsToolResult:
    """List candidate groups with optional filters."""
    check_permission("cruxible_list_groups", instance_id=instance_id)
    instance = get_manager().get(instance_id)

    result = service_list_groups(
        instance,
        relationship_type=relationship_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    return contracts.ListGroupsToolResult(
        items=[group.model_dump(mode="json") for group in result.items],
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(result.items) < result.total,
    )


def get_group_status(
    instance_id: str,
    *,
    group_id: str | None = None,
    signature: str | None = None,
) -> contracts.GroupBucketStatusToolResult:
    """Return bucket lifecycle status for a group signature."""
    check_permission("cruxible_get_group", instance_id=instance_id)
    instance = get_manager().get(instance_id)

    result = service_group_status(instance, group_id=group_id, signature=signature)
    return contracts.GroupBucketStatusToolResult(
        signature=result.signature,
        relationship_type=result.relationship_type,
        thesis_text=result.thesis_text,
        thesis_facts=result.thesis_facts,
        latest_trust_status=result.latest_trust_status,
        accepted_tuple_count=result.accepted_tuple_count,
        pending_delta_count=result.pending_delta_count,
        pending_group_id=result.pending_group_id,
        pending_version=result.pending_version,
        latest_approved_resolution_id=result.latest_approved_resolution_id,
        approved_history=[
            contracts.GroupStatusHistoryItem(
                resolution_id=item.resolution_id,
                action=item.action,
                trust_status=item.trust_status,
                confirmed=item.confirmed,
                resolved_at=item.resolved_at,
                tuple_count=item.tuple_count,
                rationale=item.rationale,
                resolved_by=item.resolved_by,
                resolved_actor=item.resolved_actor,
            )
            for item in result.approved_history
        ],
    )


def list_resolutions(
    instance_id: str,
    relationship_type: str | None = None,
    action: contracts.GroupAction | None = None,
    limit: int = 50,
    offset: int = 0,
) -> contracts.ListResolutionsToolResult:
    """List group resolutions with optional filters."""
    check_permission("cruxible_list_resolutions", instance_id=instance_id)
    instance = get_manager().get(instance_id)

    result = service_list_resolutions(
        instance,
        relationship_type=relationship_type,
        action=action,
        limit=limit,
        offset=offset,
    )
    return contracts.ListResolutionsToolResult(
        items=[r.model_dump(mode="json") for r in result.items],
        total=result.total,
        limit=limit,
        offset=offset,
        truncated=offset + len(result.items) < result.total,
    )


def state_publish(
    instance_id: str,
    transport_ref: str,
    state_id: str,
    release_id: str,
    compatibility: contracts.StateCompatibility,
) -> contracts.StatePublishResult:
    """Publish a root state instance as an immutable release bundle."""
    check_permission("cruxible_state_publish", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_publish_state(
        instance,
        transport_ref=transport_ref,
        state_id=state_id,
        release_id=release_id,
        compatibility=compatibility,
    )
    return contracts.StatePublishResult(
        manifest=contracts.PublishedStateManifest.model_validate(
            result.manifest.model_dump(mode="json")
        )
    )


def create_state_overlay_local(
    transport_ref: str | None,
    state_ref: str | None,
    kit: str | None,
    no_kit: bool,
    root_dir: str,
) -> contracts.StateOverlayResult:
    """Create a new local overlay from a published state release."""
    check_permission("cruxible_state_create_overlay", instance_id=root_dir)
    validate_root_dir(root_dir)
    result = service_create_state_overlay(
        transport_ref=transport_ref,
        state_ref=state_ref,
        kit=kit,
        no_kit=no_kit,
        root_dir=root_dir,
    )
    registered = get_registry().get_or_create_local_instance(Path(root_dir))
    get_manager().register(registered.record.instance_id, result.instance)
    return contracts.StateOverlayResult(
        instance_id=registered.record.instance_id,
        manifest=contracts.PublishedStateManifest.model_validate(
            result.manifest.model_dump(mode="json")
        ),
    )


def create_state_overlay_governed(
    transport_ref: str | None,
    state_ref: str | None,
    kit: str | None,
    no_kit: bool,
    root_dir: str,
) -> contracts.StateOverlayResult:
    """Create a daemon-owned governed overlay from a published state release."""
    check_permission("cruxible_state_create_overlay", instance_id=root_dir)
    validate_root_dir(root_dir)
    # Mirror hosted init: build the overlay at the reserved location first and
    # register the row only on success, so a refused/failed overlay leaves neither
    # a stale registry row nor a partial instance root behind.
    registry = get_registry()
    instance_id = registry.generate_governed_instance_id()
    instance_root = registry.governed_instance_location(instance_id)
    try:
        result = service_create_state_overlay(
            transport_ref=transport_ref,
            state_ref=state_ref,
            kit=kit,
            no_kit=no_kit,
            root_dir=instance_root,
            instance_mode=CruxibleInstance.GOVERNED_MODE,
        )
    except Exception:
        shutil.rmtree(instance_root, ignore_errors=True)
        raise
    registered = registry.create_governed_instance_with_id(instance_id, workspace_root=root_dir)
    get_manager().register(registered.record.instance_id, result.instance)
    return contracts.StateOverlayResult(
        instance_id=registered.record.instance_id,
        manifest=contracts.PublishedStateManifest.model_validate(
            result.manifest.model_dump(mode="json")
        ),
    )


def state_status(instance_id: str) -> contracts.StateStatusResult:
    """Return upstream tracking metadata for a release-backed overlay."""
    check_permission("cruxible_state_status", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_state_status(instance)
    upstream = (
        contracts.UpstreamMetadataResult.model_validate(result.upstream.model_dump(mode="json"))
        if result.upstream is not None
        else None
    )
    return contracts.StateStatusResult(upstream=upstream)


def state_pull_preview(instance_id: str) -> contracts.StatePullPreviewResult:
    """Preview pulling a newer upstream release into an overlay."""
    check_permission("cruxible_state_pull_preview", instance_id=instance_id)
    instance = get_manager().get(instance_id)
    result = service_pull_state_preview(instance)
    return contracts.StatePullPreviewResult(
        current_release_id=result.current_release_id,
        target_release_id=result.target_release_id,
        compatibility=result.compatibility,
        apply_digest=result.apply_digest,
        warnings=result.warnings,
        conflicts=result.conflicts,
        lock_changed=result.lock_changed,
        upstream_entity_delta=result.upstream_entity_delta,
        upstream_edge_delta=result.upstream_edge_delta,
    )


def state_pull_apply(
    instance_id: str,
    expected_apply_digest: str,
    actor_context: Any | None = None,
) -> contracts.StatePullApplyResult:
    """Apply a previewed upstream pull to a tracked overlay."""
    check_permission("cruxible_state_pull_apply", instance_id=instance_id)
    actor = _hosted_actor_context(actor_context)
    instance = get_manager().get(instance_id)
    result = service_pull_state_apply(
        instance,
        expected_apply_digest=expected_apply_digest,
        actor_context=actor,
    )
    return contracts.StatePullApplyResult(
        release_id=result.release_id,
        apply_digest=result.apply_digest,
        pre_pull_snapshot_id=result.pre_pull_snapshot_id,
    )
