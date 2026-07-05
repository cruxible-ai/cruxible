"""Workflow lock generation and compilation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import compute_kit_provider_sha256, is_kit_provider_ref
from cruxible_core.provider.registry import get_provider_entrypoint_path, resolve_provider
from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported
from cruxible_core.workflow.artifacts import resolve_local_artifact_path
from cruxible_core.workflow.contracts import (
    contract_reference_label,
    resolve_contract,
    validate_contract_payload,
)
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


def resolve_lock_path(instance: InstanceProtocol) -> Path:
    """Resolve the active workflow lock path."""
    return get_lock_path(instance)


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
        locked_digest = artifact.digest or ""
        if name in canonical_artifact_names and config_base_path is not None:
            artifact_path = resolve_local_artifact_path(artifact.uri, config_base_path)
            if artifact_path is not None:
                actual_digest = compute_path_sha256(artifact_path)
                if artifact.digest and artifact.digest != actual_digest:
                    if not force:
                        raise ConfigError(
                            _artifact_hash_mismatch_message(
                                name,
                                artifact.digest,
                                actual_digest,
                            )
                        )
                locked_digest = actual_digest
        locked_artifacts[name] = LockedArtifact(
            kind=artifact.kind,
            uri=artifact.uri,
            digest=locked_digest,
            metadata=artifact.metadata,
        )

    lock = WorkflowLock(
        config_digest=compute_lock_config_digest(config),
        artifacts=locked_artifacts,
        providers={
            name: LockedProvider(
                version=provider.version,
                ref=provider.ref,
                provider_entrypoint_digest=_compute_provider_entrypoint_sha256(
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


def _prior_step_aliases_by_index(steps: Sequence[Any]) -> list[frozenset[str]]:
    """Return, per step index, the set of aliases declared by earlier steps.

    Used to tell ``preview_value`` which ``$steps.<alias>`` references are valid
    (execution-time-deferred) versus genuinely unresolvable, so the preview can
    fail closed on unknown step aliases without breaking valid forward refs.
    """
    per_index: list[frozenset[str]] = []
    seen: set[str] = set()
    for step in steps:
        per_index.append(frozenset(seen))
        alias = getattr(step, "as_", None)
        if alias is not None:
            seen.add(alias)
    return per_index


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
            "Lock file digest does not match current lock contents. Run `cruxible lock`."
        )

    workflow = config.workflows.get(workflow_name)
    if workflow is None:
        raise ConfigError(f"Workflow '{workflow_name}' not found in workflows")
    workflow_type = workflow.type
    is_canonical = workflow_type == "canonical"
    if (
        workflow.contract_out is not None
        and resolve_contract(config, workflow.contract_out) is None
    ):
        contract_label = contract_reference_label(workflow.contract_out)
        raise ConfigError(
            f"Workflow '{workflow_name}' references unknown contract_out '{contract_label}'"
        )

    normalized_input = validate_contract_payload(
        config,
        workflow.contract_in,
        input_payload,
        subject=f"Workflow '{workflow_name}' input",
        error_factory=ConfigError,
        empty_payload_hint="Use --input or --input-file to provide workflow input.",
        strip_reserved_source_metadata=True,
    )

    compiled_steps: list[CompiledPlanStep] = []
    # Aliases of steps that appear before each step index. A `$steps.<alias>`
    # ref in a step's preview is a valid (execution-time-deferred) reference
    # only when <alias> names one of these prior steps; preview_value fails
    # closed otherwise. Any step kind (query/provider/transform/...) may declare
    # an alias and be referenced downstream, so all prior aliases are tracked.
    prior_aliases_by_index = _prior_step_aliases_by_index(workflow.steps)
    for step_index, step in enumerate(workflow.steps):
        prior_step_aliases = prior_aliases_by_index[step_index]
        if step.query is not None:
            if isinstance(step.query, str) and step.query not in config.named_queries:
                raise ConfigError(
                    f"Workflow '{workflow_name}' references unknown query '{step.query}'"
                )
            query_name = step.query if isinstance(step.query, str) else None
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="query",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    query_name=query_name,
                    inline_query=None if isinstance(step.query, str) else step.query,
                    params_template=step.params,
                    params_preview=preview_value(
                        step.params, normalized_input, step_aliases=prior_step_aliases
                    ),
                    relationship_state_template=step.relationship_state,
                    include_source=step.include_source,
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
            if current_entrypoint_sha != locked.provider_entrypoint_digest:
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
                if locked.artifact is not None:
                    if config_base_path is None:
                        raise ConfigError(
                            f"Canonical workflow '{workflow_name}' requires config_base_path for "
                            "artifact verification"
                        )
                    locked_artifact = lock.artifacts[locked.artifact]
                    _verify_local_artifact_hash(
                        locked.artifact,
                        locked_artifact.uri,
                        locked_artifact.digest,
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
                    provider_entrypoint_digest=locked.provider_entrypoint_digest,
                    artifact_name=locked.artifact,
                    artifact_digest=(
                        lock.artifacts[locked.artifact].digest if locked.artifact else None
                    ),
                    input_template=step.input,
                    input_preview=preview_value(
                        step.input, normalized_input, step_aliases=prior_step_aliases
                    ),
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

        if step.aggregate_items is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="aggregate_items",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    aggregate_items_spec=step.aggregate_items,
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

        if step.apply_all is not None:
            if not is_canonical:
                raise ConfigError(
                    f"Workflow '{workflow_name}' must be type: canonical to use apply_all"
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="apply_all",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    apply_all_spec=step.apply_all,
                )
            )
            continue

        if step.assert_not_truncated is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="assert_not_truncated",
                    workflow_type=workflow_type,
                    assert_not_truncated_spec=step.assert_not_truncated,
                )
            )
            continue

        if step.assert_count is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="assert_count",
                    workflow_type=workflow_type,
                    assert_count_spec=step.assert_count,
                )
            )
            continue

        if step.assert_exists is not None:
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="assert_exists",
                    workflow_type=workflow_type,
                    assert_exists_spec=step.assert_exists,
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
        contract_out=(
            contract_reference_label(workflow.contract_out)
            if workflow.contract_out is not None
            else None
        ),
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
    enforce_customer_code_execution_supported()
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
    expected_digest: str,
    config_base_path: Path,
) -> None:
    if not expected_digest:
        raise ConfigError("Canonical workflow artifact is missing digest")
    artifact_path = resolve_local_artifact_path(uri, config_base_path)
    if artifact_path is None:
        raise ConfigError("Canonical workflows require local file or directory artifacts")
    if not artifact_path.exists():
        raise ConfigError(f"Artifact path does not exist: {artifact_path}")
    actual_digest = compute_path_sha256(artifact_path)
    if actual_digest != expected_digest:
        raise ConfigError(_artifact_hash_mismatch_message(name, expected_digest, actual_digest))


def _artifact_hash_mismatch_message(name: str, expected_digest: str, actual_digest: str) -> str:
    return (
        f"Artifact '{name}' digest mismatch.\n"
        f"  expected (config): {expected_digest}\n"
        f"  actual (on disk):  {actual_digest}\n"
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
