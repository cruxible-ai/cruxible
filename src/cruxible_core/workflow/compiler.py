"""Workflow lock generation and compilation."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import yaml

from cruxible_core.config.schema import CoreConfig, ProviderSchema, WorkflowType
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.kits import compute_kit_provider_sha256, is_kit_provider_ref
from cruxible_core.procedure.analysis import (
    ProcedureGraph,
    build_procedure_graph,
    declared_control_targets,
)
from cruxible_core.procedure.types import (
    ABORT_TARGET,
    ProcedureDefinition,
    ProcedureGuardStepSchema,
    ProcedureProjectStepSchema,
    unwrap_procedure_step,
)
from cruxible_core.provider.registry import (
    get_provider_entrypoint_path,
    resolve_command_provider_target,
    resolve_provider,
)
from cruxible_core.workflow.artifacts import resolve_local_artifact_path
from cruxible_core.workflow.contracts import (
    contract_reference_label,
    resolve_contract,
    validate_contract_payload,
)
from cruxible_core.workflow.refs import preview_definition_value, preview_value
from cruxible_core.workflow.types import (
    CompiledPlan,
    CompiledPlanStep,
    LockedArtifact,
    LockedProvider,
    WorkflowLock,
)

LOCK_FILE_NAME = "cruxible.lock.yaml"
_PROCEDURE_OUTPUT_STEP_KINDS = frozenset(
    {
        "query",
        "provider",
        "project",
        "repeat",
        "shape_items",
        "join_items",
        "filter_items",
        "aggregate_items",
        "dedupe_items",
    }
)

# Env toggle that lets kit:// provider refs resolve against the kit directory
# itself instead of an installed kit cache. Kit-root lock generation always
# needs it: the kit dir IS the source of truth being pinned.
_KIT_DEV_RESOLVE_ENV = "CRUXIBLE_KIT_DEV_RESOLVE"


def build_kit_root_lock(kit_root: Path, *, force: bool = False) -> WorkflowLock:
    """Build the canonical kit-root lock for a kit directory.

    This is THE generation path for a committed ``kits/<id>/cruxible.lock.yaml``
    (the CLI's ``cruxible lock --kit-dir`` and the CI freshness check both call
    it). It locks the kit's own config LAYER only — deliberately no manifest
    ``target_state`` composition — so the lock pins exactly what the kit
    directory distributes: its own providers and artifacts, with URIs preserved
    as written in ``config.yaml`` (relative to the kit root, portable across
    machines). Base-layer content is pinned by the base kit's own lock.
    """
    kit_root = kit_root.resolve()
    config_path = kit_root / "config.yaml"
    if not config_path.exists():
        raise ConfigError(f"kit root has no config.yaml: {config_path}")

    from cruxible_core.config.loader import load_config

    previous = os.environ.get(_KIT_DEV_RESOLVE_ENV)
    os.environ[_KIT_DEV_RESOLVE_ENV] = "1"
    try:
        config = load_config(config_path)
        return build_lock(config, kit_root, force=force)
    finally:
        if previous is None:
            os.environ.pop(_KIT_DEV_RESOLVE_ENV, None)
        else:
            os.environ[_KIT_DEV_RESOLVE_ENV] = previous


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
                provider_command_path=compute_provider_command_path(
                    provider_name=name,
                    provider=provider,
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


def _prior_step_aliases_by_index(
    steps: Sequence[Any],
    *,
    initial_aliases: frozenset[str] = frozenset(),
) -> list[frozenset[str]]:
    """Return, per step index, the set of aliases declared by earlier steps.

    Used to tell ``preview_value`` which ``$steps.<alias>`` references are valid
    (execution-time-deferred) versus genuinely unresolvable, so the preview can
    fail closed on unknown step aliases without breaking valid forward refs.
    """
    per_index: list[frozenset[str]] = []
    seen = set(initial_aliases)
    for wrapper in steps:
        # UNWRAP. A flow wrapper has only `step` and `next`, so reading `as_`
        # off it would make a wrapped step's alias invisible to every
        # downstream reference check.
        step = unwrap_procedure_step(wrapper)
        per_index.append(frozenset(seen))
        alias = getattr(step, "as_", None)
        if alias is not None:
            seen.add(alias)
    return per_index


def _resolved_control_edges(workflow: Any, definition_label: str) -> dict[str, dict[str, str]]:
    """Resolve every control edge once, refusing R1/R2/R3/R15 in the process.

    Only procedures have a control graph. A configured workflow cannot parse a
    guard node or a flow wrapper -- the type system already refused it -- so
    running the graph analysis over one would be checking a property that
    cannot be violated.
    """
    if definition_label != "Procedure":
        return {}
    if not isinstance(workflow, ProcedureDefinition):
        return {}
    graph = build_procedure_graph(workflow)
    _refuse_unresolvable_parameter_operands(graph, workflow)
    return graph.edges


def _refuse_unresolvable_parameter_operands(
    graph: ProcedureGraph,
    definition: ProcedureDefinition,
) -> None:
    """Refuse `@param` until governed parameters exist.

    The grammar admits the operand form so the parser and the digest are stable
    across the batch that introduces parameters. Admitting it into a COMPILED
    plan before there is anything to resolve it against would produce a
    procedure that accepts and then fails at run time, which is the one outcome
    acceptance is supposed to rule out.
    """
    for step in definition.steps:
        if not isinstance(step, ProcedureGuardStepSchema):
            continue
        for operand in step.guard.operands():
            if operand.form == "param":
                raise ConfigError(
                    f"Procedure guard step '{step.id}' references governed parameter "
                    f"'{operand.parameter_name}', which this core cannot resolve. "
                    "Governed scalar parameters are not available yet; use a literal."
                )


def _compile_guard_step(
    step: ProcedureGuardStepSchema,
    *,
    workflow_name: str,
    definition_label: str,
    control: dict[str, str],
) -> CompiledPlanStep:
    for operand in step.guard.operands():
        if operand.form in {"count", "truncated"} and operand.alias is None:
            raise ConfigError(
                f"{definition_label} '{workflow_name}' guard step '{step.id}' has an "
                "accessor with no alias"
            )
    return CompiledPlanStep(
        step_id=step.id,
        kind="guard",
        workflow_type="utility",
        guard_spec=step.guard,
        guard_message=step.message,
        on_true_step_id=control.get("on_true"),
        on_false_step_id=control.get("on_false", ABORT_TARGET),
    )


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
    return compile_plan_definition(
        config,
        lock,
        workflow_name,
        workflow,
        input_payload,
        config_base_path=config_base_path,
        definition_label="Workflow",
        _validated_config_digest=digest,
    )


def compile_plan_definition(
    config: CoreConfig,
    lock: WorkflowLock,
    definition_name: str,
    definition: Any,
    input_payload: dict[str, Any] | None,
    *,
    config_base_path: Path | None = None,
    definition_label: str = "Plan",
    _validated_config_digest: str | None = None,
    _initial_step_aliases: frozenset[str] = frozenset(),
) -> CompiledPlan:
    """Compile a supplied plan definition without resolving it from config.

    ``input_payload=None`` is the definition-time mode used when procedures are
    proposed or accepted. It validates contract references and step-reference
    structure while preserving unresolved ``$input`` references. Configured
    workflows always pass a concrete payload through :func:`compile_workflow`,
    retaining their existing validation and compiled output byte-for-byte.
    """
    digest = _validated_config_digest
    if digest is None:
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

    workflow = definition
    workflow_name = definition_name
    workflow_type = cast("WorkflowType", getattr(workflow, "type", "utility"))
    is_canonical = workflow_type == "canonical"
    if (
        workflow.contract_out is not None
        and resolve_contract(config, workflow.contract_out) is None
    ):
        contract_label = contract_reference_label(workflow.contract_out)
        raise ConfigError(
            f"{definition_label} '{workflow_name}' references unknown "
            f"contract_out '{contract_label}'"
        )

    if input_payload is None:
        if resolve_contract(config, workflow.contract_in) is None:
            contract_label = contract_reference_label(workflow.contract_in)
            raise ConfigError(
                f"{definition_label} '{workflow_name}' references unknown "
                f"contract_in '{contract_label}'"
            )
        normalized_input: dict[str, Any] = {}
    else:
        normalized_input = validate_contract_payload(
            config,
            workflow.contract_in,
            input_payload,
            subject=f"{definition_label} '{workflow_name}' input",
            error_factory=ConfigError,
            empty_payload_hint="Use --input or --input-file to provide workflow input.",
            strip_reserved_source_metadata=True,
        )

    def preview(template: Any, *, step_aliases: frozenset[str]) -> Any:
        if input_payload is None:
            return preview_definition_value(template, step_aliases=step_aliases)
        return preview_value(template, normalized_input, step_aliases=step_aliases)

    compiled_steps: list[CompiledPlanStep] = []
    # Aliases of steps that appear before each step index. A `$steps.<alias>`
    # ref in a step's preview is a valid (execution-time-deferred) reference
    # only when <alias> names one of these prior steps; preview_value fails
    # closed otherwise. Any step kind (query/provider/transform/...) may declare
    # an alias and be referenced downstream, so all prior aliases are tracked.
    prior_aliases_by_index = _prior_step_aliases_by_index(
        workflow.steps,
        initial_aliases=_initial_step_aliases,
    )
    control_edges = _resolved_control_edges(workflow, definition_label)
    for step_index, step in enumerate(workflow.steps):
        prior_step_aliases = prior_aliases_by_index[step_index]
        # Read control targets BEFORE unwrapping: the wrapper owns `next`, a
        # guard owns `on_true`/`on_false`.
        control = control_edges.get(str(step.id), {})
        if isinstance(step, ProcedureProjectStepSchema):
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="project",
                    workflow_type="utility",
                    as_name=step.as_,
                    project_spec=step.project,
                    params_preview=preview(
                        dict(step.project.fields), step_aliases=prior_step_aliases
                    ),
                )
            )
            continue
        if isinstance(step, ProcedureGuardStepSchema):
            compiled_steps.append(
                _compile_guard_step(
                    step,
                    workflow_name=workflow_name,
                    definition_label=definition_label,
                    control=control,
                )
            )
            continue
        # UNWRAP. After this the existing field-by-field chain is unchanged,
        # which is why the wrapper design costs one line rather than a parallel
        # compile path.
        step = unwrap_procedure_step(step)
        repeat_spec = getattr(step, "repeat", None)
        if repeat_spec is not None:
            nested_aliases = [nested.as_ for nested in repeat_spec.steps if nested.as_ is not None]
            preview(
                repeat_spec.until.model_dump(mode="python"),
                step_aliases=frozenset(nested_aliases),
            )
            nested_definition = SimpleNamespace(
                type="utility",
                contract_in=workflow.contract_in,
                contract_out=None,
                steps=repeat_spec.steps,
                returns=nested_aliases[-1] if nested_aliases else step.id,
            )
            nested_plan = compile_plan_definition(
                config,
                lock,
                f"{workflow_name}.{step.id}",
                nested_definition,
                input_payload,
                config_base_path=config_base_path,
                definition_label=definition_label,
                _validated_config_digest=digest,
                _initial_step_aliases=prior_step_aliases,
            )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="repeat",
                    workflow_type="utility",
                    as_name=step.as_,
                    repeat_max_attempts=repeat_spec.max_attempts,
                    repeat_until_spec=repeat_spec.until,
                    repeat_steps=nested_plan.steps,
                )
            )
            continue
        if step.query is not None:
            if isinstance(step.query, str) and step.query not in config.named_queries:
                raise ConfigError(
                    f"{definition_label} '{workflow_name}' references unknown query '{step.query}'"
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
                    params_preview=preview(step.params, step_aliases=prior_step_aliases),
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
            current_command_path = compute_provider_command_path(
                provider_name=step.provider,
                provider=provider_schema,
                config_base_path=config_base_path,
            )
            if current_command_path != locked.provider_command_path:
                raise ConfigError(
                    f"Provider '{step.provider}' command ref resolves to "
                    f"{current_command_path or '(none)'}, not the "
                    f"{locked.provider_command_path or '(none)'} the lock recorded. "
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
                    input_preview=preview(step.input, step_aliases=prior_step_aliases),
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

        if step.register_source_artifacts is not None:
            if not is_canonical:
                raise ConfigError(
                    f"Workflow '{workflow_name}' must be type: canonical to use "
                    "register_source_artifacts"
                )
            compiled_steps.append(
                CompiledPlanStep(
                    step_id=step.id,
                    kind="register_source_artifacts",
                    workflow_type=workflow_type,
                    as_name=step.as_,
                    register_source_artifacts_spec=step.register_source_artifacts,
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

    compiled_by_id = {compiled.step_id: compiled for compiled in compiled_steps}
    for step in workflow.steps:
        # Only a DECLARED edge is carried. Implicit fallthrough stays implicit,
        # so a compiled linear plan is byte-identical to the one this compiler
        # produced before the graph existed.
        declared = declared_control_targets(step)
        if "next" not in declared:
            continue
        compiled = compiled_by_id.get(str(step.id))
        if compiled is not None:
            compiled.next_step_id = declared["next"]

    if input_payload is None and definition_label == "Procedure":
        produced_outputs = {
            step.as_name or step.step_id
            for step in compiled_steps
            if step.kind in _PROCEDURE_OUTPUT_STEP_KINDS
        }
        if workflow.returns not in produced_outputs:
            raise ConfigError(
                f"Procedure '{workflow_name}' returns alias '{workflow.returns}' "
                "not produced by any output step"
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
    # Imported lazily: a module-level import of runtime.execution_policy
    # executes runtime/__init__ (the full instance stack), which imports this
    # module back — a cycle for entry points reaching workflow before runtime.
    from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported

    enforce_customer_code_execution_supported()
    provider = config.providers[provider_name]
    if provider.runtime == "command":
        return _compute_command_provider_sha256(
            provider_name=provider_name,
            provider=provider,
            config_base_path=config_base_path,
        )
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


def _compute_command_provider_sha256(
    *,
    provider_name: str,
    provider: ProviderSchema,
    config_base_path: Path | None,
) -> str | None:
    """Hash a command provider's executable when it is the instance's to pin.

    Workspace-relative commands are hashed. System executables are not (they are
    the OS trust boundary; hashing them would invalidate every lock on every
    system update) -- their path identity is recorded separately in
    ``LockedProvider.provider_command_path``.

    A command exported to procedures gets no unhashed workspace path: procedures
    run unattended under a budget, so a workspace script that cannot be pinned
    is refused rather than executed on the strength of its filename.
    """
    target = resolve_command_provider_target(
        provider_name,
        provider,
        config_base_path=config_base_path,
    )
    if target.workspace_path is not None:
        return f"sha256:{hashlib.sha256(target.workspace_path.read_bytes()).hexdigest()}"
    if target.declared_workspace_relative and provider.procedure_access != "disabled":
        raise ConfigError(
            f"Provider '{provider_name}' is exported to procedures "
            f"(procedure_access: {provider.procedure_access}) and its command ref "
            f"'{provider.ref}' names a path inside the instance, but no file is there to "
            "pin. A procedure-exported command must be a real file whose contents can be "
            "hashed into the lock. Add the script at that path and re-run `cruxible "
            "lock`, or point the provider at an absolute system executable if the OS "
            "binary is what you mean."
        )
    return None


def compute_provider_command_path(
    *,
    provider_name: str,
    provider: ProviderSchema,
    config_base_path: Path | None,
) -> str | None:
    """Record which system executable a command ref resolved to, for later comparison."""
    if provider.runtime != "command":
        return None
    target = resolve_command_provider_target(
        provider_name,
        provider,
        config_base_path=config_base_path,
    )
    return str(target.system_path) if target.system_path is not None else None


def verify_provider_entrypoint_digest(
    provider_name: str,
    config: CoreConfig,
    *,
    expected_digest: str | None,
    expected_command_path: str | None = None,
    config_base_path: Path | None = None,
) -> None:
    """Refuse a provider whose entrypoint no longer matches what the lock pinned.

    Compilation pins each provider's entrypoint digest into the plan, but the
    plan is executed later -- after other steps have run, after repeat attempts,
    and for procedures under a wall-clock budget measured in minutes. The file
    that will actually be imported and called is only known at invocation, so
    the locked digest is compared again here, immediately before the call.

    For a command provider resolving to a system executable there is no digest
    to compare -- see the posture note on
    ``resolve_command_provider_target`` -- but the path the ref resolves to is
    recorded, and a ref that now resolves somewhere else (a PATH entry inserted
    ahead of the locked one, a re-pointed absolute path) is refused.
    """
    current_digest = _compute_provider_entrypoint_sha256(
        provider_name=provider_name,
        config=config,
        config_base_path=config_base_path,
    )
    if current_digest != expected_digest:
        raise ConfigError(
            f"Provider '{provider_name}' entrypoint does not match its locked digest at "
            f"invocation: lock records {expected_digest or '(none)'}, found "
            f"{current_digest or '(none)'}. The provider code changed after the plan was "
            "compiled, so the call is refused rather than executed against unpinned code. "
            "Re-run `cruxible lock` to pin the current provider code, or restore the "
            "locked entrypoint, then re-run."
        )
    current_command_path = compute_provider_command_path(
        provider_name=provider_name,
        provider=config.providers[provider_name],
        config_base_path=config_base_path,
    )
    if current_command_path != expected_command_path:
        raise ConfigError(
            f"Provider '{provider_name}' command ref "
            f"'{config.providers[provider_name].ref}' no longer resolves to the executable "
            f"the lock recorded: lock records {expected_command_path or '(none)'}, now "
            f"resolves to {current_command_path or '(none)'}. System executables are not "
            "hashed -- their path identity is what is pinned -- so a ref that changes "
            "target is refused rather than run. Restore the environment the lock was "
            "generated in (PATH, virtualenv, installed packages), or re-run `cruxible "
            "lock` to pin the executable this environment resolves."
        )


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
