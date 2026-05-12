"""Workflow lock generation and compilation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import compute_kit_provider_sha256, is_kit_provider_ref
from cruxible_core.provider.registry import get_provider_entrypoint_path, resolve_provider
from cruxible_core.workflow.artifacts import resolve_local_artifact_path
from cruxible_core.workflow.contracts import contract_reference_label, validate_contract_payload
from cruxible_core.workflow.refs import preview_value
from cruxible_core.workflow.types import (
    CompiledPlan,
    CompiledPlanStep,
    LockedArtifact,
    LockedProvider,
    WorkflowLock,
)

LOCK_FILE_NAME = "cruxible.lock.yaml"


def compute_lock_config_digest(config: CoreConfig) -> str:
    """Compute a stable config digest for lock generation."""
    dumped = json.dumps(
        config.model_dump(mode="python", by_alias=True, exclude_none=True),
        sort_keys=True,
        default=str,
    )
    return f"sha256:{hashlib.sha256(dumped.encode()).hexdigest()}"


def get_lock_path(instance: InstanceProtocol) -> Path:
    """Return the workflow lock path for an instance."""
    return instance.get_instance_dir() / LOCK_FILE_NAME


def get_legacy_lock_path(instance: InstanceProtocol) -> Path:
    """Return the legacy config-adjacent workflow lock path for an instance."""
    return instance.get_config_path().parent / LOCK_FILE_NAME


def resolve_lock_path(instance: InstanceProtocol) -> Path:
    """Resolve the active workflow lock path, preferring the instance-local location."""
    current = get_lock_path(instance)
    if current.exists():
        return current
    legacy = get_legacy_lock_path(instance)
    if legacy.exists():
        return legacy
    return current


def build_lock(
    config: CoreConfig,
    config_base_path: Path | None = None,
    *,
    force: bool = False,
) -> WorkflowLock:
    """Generate a workflow lock from config/provider/artifact declarations."""
    for provider_name, provider in config.providers.items():
        resolve_provider(provider_name, provider, config_base_path=config_base_path)

    canonical_artifact_names = _collect_canonical_artifact_names(config)
    locked_artifacts: dict[str, LockedArtifact] = {}
    for name, artifact in config.artifacts.items():
        locked_sha256 = artifact.sha256 or ""
        if name in canonical_artifact_names and config_base_path is not None:
            artifact_path = resolve_local_artifact_path(artifact.uri, config_base_path)
            if artifact_path is not None:
                actual_sha256 = compute_path_sha256(artifact_path)
                if artifact.sha256 and artifact.sha256 != actual_sha256:
                    if not force:
                        raise ConfigError(
                            _artifact_hash_mismatch_message(
                                name,
                                artifact.sha256,
                                actual_sha256,
                            )
                        )
                locked_sha256 = actual_sha256
        locked_artifacts[name] = LockedArtifact(
            kind=artifact.kind,
            uri=artifact.uri,
            sha256=locked_sha256,
            metadata=artifact.metadata,
        )

    lock = WorkflowLock(
        config_digest=compute_lock_config_digest(config),
        artifacts=locked_artifacts,
        providers={
            name: LockedProvider(
                version=provider.version,
                ref=provider.ref,
                provider_entrypoint_sha256=_compute_provider_entrypoint_sha256(
                    provider_name=name,
                    config=config,
                    config_base_path=config_base_path,
                ),
                runtime=provider.runtime,
                deterministic=provider.deterministic,
                side_effects=provider.side_effects,
                artifact=provider.artifact,
                config=provider.config,
            )
            for name, provider in config.providers.items()
        },
    )
    lock.lock_digest = compute_lock_digest(lock)
    return lock


def compute_lock_digest(lock: WorkflowLock) -> str:
    """Compute a stable digest for a lock file, excluding volatile timestamps."""
    dumped = lock.model_dump(
        mode="python",
        exclude_none=True,
        exclude={"generated_at", "lock_digest"},
    )
    encoded = json.dumps(dumped, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def write_lock(lock: WorkflowLock, path: Path) -> None:
    """Write a generated workflow lock to disk."""
    if lock.lock_digest is None:
        lock.lock_digest = compute_lock_digest(lock)
    data = lock.model_dump(mode="python", exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def load_lock(path: Path) -> WorkflowLock:
    """Load a workflow lock from disk."""
    if not path.exists():
        raise ConfigError(f"Lock file not found: {path}. Run `cruxible lock` first.")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"Lock file at {path} must contain a YAML mapping")
    return WorkflowLock.model_validate(raw)


def compile_workflow(
    config: CoreConfig,
    lock: WorkflowLock,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    config_base_path: Path | None = None,
) -> CompiledPlan:
    """Compile a workflow and validate input against its contract."""
    digest = compute_lock_config_digest(config)
    if lock.config_digest != digest:
        raise ConfigError(
            "Lock file config digest does not match current config. Run `cruxible lock`."
        )
    expected_lock_digest = compute_lock_digest(lock)
    if lock.lock_digest != expected_lock_digest:
        raise ConfigError(
            "Lock file digest does not match current lock contents. "
            "Run `cruxible lock`."
        )

    workflow = config.workflows.get(workflow_name)
    if workflow is None:
        raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
    workflow_type = workflow.type
    is_canonical = workflow_type == "canonical"

    normalized_input = validate_contract_payload(
        config,
        workflow.contract_in,
        input_payload,
        subject=f"Workflow '{workflow_name}' input",
        error_factory=ConfigError,
        empty_payload_hint="Use --input or --input-file to provide workflow input.",
    )

    compiled_steps: list[CompiledPlanStep] = []
    for step in workflow.steps:
        if step.query is not None:
            if step.query not in config.named_queries:
                raise ConfigError(
                    f"Workflow '{workflow_name}' references unknown query '{step.query}'"
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="query",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    query_name=step.query,
                    params_template=step.params,
                    params_preview=preview_value(step.params, normalized_input),
                )
            )
            continue

        if step.provider is not None:
            locked = lock.providers.get(step.provider)
            if locked is None:
                raise ConfigError(
                    f"Provider '{step.provider}' missing from lock file. Run `cruxible lock`."
                )
            provider_schema = config.providers[step.provider]
            current_entrypoint_sha = _compute_provider_entrypoint_sha256(
                provider_name=step.provider,
                config=config,
                config_base_path=config_base_path,
            )
            if current_entrypoint_sha != locked.provider_entrypoint_sha256:
                raise ConfigError(
                    f"Provider '{step.provider}' entrypoint changed since lock generation. "
                    "Run `cruxible lock`."
                )
            if is_canonical:
                if provider_schema.runtime != "python":
                    raise ConfigError(
                        f"Canonical workflow '{workflow_name}' requires python providers"
                    )
                if not provider_schema.deterministic or provider_schema.side_effects:
                    raise ConfigError(
                        f"Canonical workflow '{workflow_name}' requires deterministic, "
                        "side-effect-free providers"
                    )
                if locked.artifact is None:
                    raise ConfigError(
                        f"Canonical workflow '{workflow_name}' provider '{step.provider}' "
                        "must declare an artifact bundle"
                    )
                if config_base_path is None:
                    raise ConfigError(
                        f"Canonical workflow '{workflow_name}' requires config_base_path for "
                        "artifact verification"
                    )
                locked_artifact = lock.artifacts[locked.artifact]
                _verify_local_artifact_hash(
                    locked.artifact,
                    locked_artifact.uri,
                    locked_artifact.sha256,
                    config_base_path,
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="provider",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    provider_name=step.provider,
                    provider_ref=locked.ref,
                    provider_version=locked.version,
                    provider_entrypoint_sha256=locked.provider_entrypoint_sha256,
                    artifact_name=locked.artifact,
                    artifact_sha256=(
                        lock.artifacts[locked.artifact].sha256 if locked.artifact else None
                    ),
                    input_template=step.input,
                    input_preview=preview_value(step.input, normalized_input),
                )
            )
            continue

        if step.list_entities is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="list_entities",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    list_entities_spec=step.list_entities,
                )
            )
            continue

        if step.list_relationships is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="list_relationships",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    list_relationships_spec=step.list_relationships,
                )
            )
            continue

        if step.shape_items is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="shape_items",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    shape_items_spec=step.shape_items,
                )
            )
            continue

        if step.join_items is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="join_items",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    join_items_spec=step.join_items,
                )
            )
            continue

        if step.filter_items is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="filter_items",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    filter_items_spec=step.filter_items,
                )
            )
            continue

        if step.dedupe_items is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="dedupe_items",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    dedupe_items_spec=step.dedupe_items,
                )
            )
            continue

        if step.make_candidates is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="make_candidates",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    make_candidates_spec=step.make_candidates,
                )
            )
            continue

        if step.map_signals is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="map_signals",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    map_signals_spec=step.map_signals,
                )
            )
            continue

        if step.propose_relationship_group is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="propose_relationship_group",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    propose_relationship_group_spec=step.propose_relationship_group,
                )
            )
            continue

        if step.make_entities is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="make_entities",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    make_entities_spec=step.make_entities,
                )
            )
            continue

        if step.make_relationships is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="make_relationships",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    make_relationships_spec=step.make_relationships,
                )
            )
            continue

        if step.apply_entities is not None:
            if not is_canonical:
                raise ConfigError(
                    f"Workflow '{workflow_name}' must be type: canonical to use apply_entities"
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="apply_entities",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    apply_entities_spec=step.apply_entities,
                )
            )
            continue

        if step.apply_relationships is not None:
            if not is_canonical:
                raise ConfigError(
                    f"Workflow '{workflow_name}' must be type: canonical to use apply_relationships"
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="apply_relationships",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    apply_relationships_spec=step.apply_relationships,
                )
            )
            continue

        assert step.assert_spec is not None
        compiled_steps.append(
            CompiledPlanStep(
                step_id=step.id,
                kind="assert",
                workflow_type=workflow_type,
                assert_spec=step.assert_spec,
            )
        )

    return CompiledPlan(
        workflow=workflow_name,
        contract_in=contract_reference_label(workflow.contract_in),
        config_digest=digest,
        lock_digest=lock.lock_digest,
        workflow_type=workflow_type,
        steps=compiled_steps,
        returns=workflow.returns,
        input_payload=normalized_input,
    )


def _compute_provider_entrypoint_sha256(
    provider_name: str,
    config: CoreConfig,
    *,
    config_base_path: Path | None = None,
) -> str | None:
    provider = config.providers[provider_name]
    if is_kit_provider_ref(provider.ref):
        if config_base_path is None:
            raise ConfigError(
                f"Provider '{provider_name}' uses kit:// ref '{provider.ref}', but no config "
                "base path was provided for lock generation"
            )
        return compute_kit_provider_sha256(provider.ref, config_base_path)
    path = get_provider_entrypoint_path(provider_name, provider, config_base_path=config_base_path)
    if path is None:
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _collect_canonical_artifact_names(config: CoreConfig) -> set[str]:
    artifact_names: set[str] = set()
    for workflow in config.workflows.values():
        if workflow.type != "canonical":
            continue
        for step in workflow.steps:
            if step.provider is None:
                continue
            provider = config.providers.get(step.provider)
            if provider is not None and provider.artifact is not None:
                artifact_names.add(provider.artifact)
    return artifact_names


def _verify_local_artifact_hash(
    name: str,
    uri: str,
    expected_sha256: str,
    config_base_path: Path,
) -> None:
    if not expected_sha256:
        raise ConfigError("Canonical workflow artifact is missing sha256")
    artifact_path = resolve_local_artifact_path(uri, config_base_path)
    if artifact_path is None:
        raise ConfigError("Canonical workflows require local file or directory artifacts")
    if not artifact_path.exists():
        raise ConfigError(f"Artifact path does not exist: {artifact_path}")
    actual_sha256 = compute_path_sha256(artifact_path)
    if actual_sha256 != expected_sha256:
        raise ConfigError(_artifact_hash_mismatch_message(name, expected_sha256, actual_sha256))


def _artifact_hash_mismatch_message(name: str, expected_sha256: str, actual_sha256: str) -> str:
    return (
        f"Artifact '{name}' sha256 mismatch.\n"
        f"  expected (config): {expected_sha256}\n"
        f"  actual (on disk):  {actual_sha256}\n"
        "Run 'cruxible lock --force' to accept the on-disk hash, or restore the expected artifact."
    )


def compute_path_sha256(path: Path) -> str:
    if path.is_file():
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            relative = child.relative_to(path).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"
    raise ConfigError(f"Unsupported artifact path type: {path}")
