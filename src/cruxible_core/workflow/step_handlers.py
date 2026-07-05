"""Step-handler registry for compiled workflow execution."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, get_args

from cruxible_core.config.schema import StepKind
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.workflow.apply import (
    apply_entity_set,
    apply_relationship_set,
    make_entity_set,
    make_relationship_set,
)
from cruxible_core.workflow.execution_context import WorkflowExecutionContext
from cruxible_core.workflow.io import (
    execute_assert_count_step,
    execute_assert_exists_step,
    execute_assert_not_truncated_step,
    execute_assert_step,
    execute_provider_step,
    execute_query_step,
)
from cruxible_core.workflow.proposals import (
    build_relationship_group_proposal,
    make_candidate_set,
    map_signal_batch,
    signal_mapping_snapshot,
)
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import SOURCE_METADATA_KEY, resolve_step_items
from cruxible_core.workflow.transforms import (
    aggregate_items,
    dedupe_items,
    filter_items,
    join_items,
    shape_items,
)
from cruxible_core.workflow.types import CompiledPlanStep, EntitySet, RelationshipSet

VALID_STEP_KINDS: frozenset[str] = frozenset(str(kind) for kind in get_args(StepKind))


class WorkflowStepHandler(Protocol):
    """Callable that executes one compiled workflow step against shared context."""

    def __call__(
        self,
        context: WorkflowExecutionContext,
        compiled_step: CompiledPlanStep,
    ) -> None: ...


class WorkflowStepRegistry:
    """Deterministic registry mapping every StepKind to one handler."""

    def __init__(
        self,
        registrations: list[tuple[str, WorkflowStepHandler]] | None = None,
    ) -> None:
        self._handlers: dict[str, WorkflowStepHandler] = {}
        for kind, handler in registrations or []:
            self.register(kind, handler)

    @property
    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def register(self, kind: str, handler: WorkflowStepHandler) -> None:
        if kind not in VALID_STEP_KINDS:
            valid = ", ".join(sorted(VALID_STEP_KINDS))
            raise ValueError(f"Unknown workflow step kind '{kind}'. Valid kinds: {valid}")
        if kind in self._handlers:
            raise ValueError(f"Duplicate workflow step handler for kind '{kind}'")
        self._handlers[kind] = handler

    def validate_complete(self) -> None:
        missing = sorted(VALID_STEP_KINDS.difference(self._handlers))
        if missing:
            raise ValueError(f"Missing workflow step handler(s): {missing}")

    def execute(
        self,
        context: WorkflowExecutionContext,
        compiled_step: CompiledPlanStep,
    ) -> None:
        handler = self._handlers[compiled_step.kind]
        handler(context, compiled_step)


def execute_query_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_query_step(
        context.instance,
        context.config,
        context.graph,
        context.plan,
        compiled_step,
        context.step_outputs,
        context.alias_step_ids,
        context.query_receipt_ids,
        context.receipt_builder,
        persist_receipt=context.persist_query_receipts,
    )


def execute_provider_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_provider_step(
        context.instance,
        context.config,
        context.lock,
        context.plan,
        compiled_step,
        context.step_outputs,
        context.alias_step_ids,
        context.traces,
        context.step_trace_ids,
        context.receipt_builder,
        workflow_name=context.workflow_name,
        persist_traces=context.persist_traces,
        config_base_path=context.config_base_path,
    )


def execute_shape_items_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.shape_items_spec is not None
    shaped_items = shape_items(
        compiled_step.step_id,
        compiled_step.shape_items_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, shaped_items)
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "shape_items",
        detail={
            "input_count": shaped_items["input_count"],
            "output_count": shaped_items["output_count"],
            "dropped_count": shaped_items["dropped_count"],
        },
    )


def execute_join_items_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.join_items_spec is not None
    joined_items = join_items(
        compiled_step.step_id,
        compiled_step.join_items_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, joined_items)
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "join_items",
        detail={
            "left_count": joined_items["left_count"],
            "right_count": joined_items["right_count"],
            "skipped_right_count": joined_items["skipped_right_count"],
            "matched_left_count": joined_items["matched_left_count"],
            "output_count": joined_items["output_count"],
        },
    )


def execute_filter_items_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.filter_items_spec is not None
    filtered_items = filter_items(
        compiled_step.step_id,
        compiled_step.filter_items_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, filtered_items)
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "filter_items",
        detail={
            "input_count": filtered_items["input_count"],
            "output_count": filtered_items["output_count"],
            "filtered_count": filtered_items["filtered_count"],
        },
    )


def execute_aggregate_items_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.aggregate_items_spec is not None
    aggregated_items = aggregate_items(
        compiled_step.step_id,
        compiled_step.aggregate_items_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, aggregated_items)
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "aggregate_items",
        detail={
            "input_count": aggregated_items["input_count"],
            "group_count": aggregated_items["group_count"],
            "output_count": aggregated_items["output_count"],
            "measures": {
                name: measure.operation
                for name, measure in compiled_step.aggregate_items_spec.measures.items()
            },
            SOURCE_METADATA_KEY: aggregated_items.get(SOURCE_METADATA_KEY, {}),
        },
    )


def execute_dedupe_items_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.dedupe_items_spec is not None
    deduped_items = dedupe_items(
        compiled_step.step_id,
        compiled_step.dedupe_items_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, deduped_items)
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "dedupe_items",
        detail={
            "input_count": deduped_items["input_count"],
            "output_count": deduped_items["output_count"],
            "duplicate_count": deduped_items["duplicate_count"],
        },
    )


def execute_make_candidates_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.make_candidates_spec is not None
    candidate_set = make_candidate_set(
        context.config,
        compiled_step.step_id,
        compiled_step.make_candidates_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, candidate_set.model_dump(mode="python"))
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "make_candidates",
        detail={
            "relationship_type": candidate_set.relationship_type,
            "candidate_count": len(candidate_set.candidates),
            "duplicate_input_count": candidate_set.duplicate_input_count,
            "conflicting_duplicate_count": candidate_set.conflicting_duplicate_count,
            "duplicate_examples": candidate_set.duplicate_examples,
            "item_count": len(
                resolve_step_items(
                    compiled_step.make_candidates_spec.items,
                    context.plan.input_payload,
                    context.step_outputs,
                )
            ),
        },
    )


def execute_map_signals_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.map_signals_spec is not None
    signal_batch = map_signal_batch(
        compiled_step.step_id,
        compiled_step.map_signals_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, signal_batch.model_dump(mode="python"))
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "map_signals",
        detail={
            "signal_source": signal_batch.signal_source,
            "signal_count": len(signal_batch.signals),
            "mapping": signal_mapping_snapshot(compiled_step.map_signals_spec),
            "item_count": len(
                resolve_step_items(
                    compiled_step.map_signals_spec.items,
                    context.plan.input_payload,
                    context.step_outputs,
                )
            ),
        },
    )


def execute_propose_relationship_group_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.propose_relationship_group_spec is not None
    proposal = build_relationship_group_proposal(
        compiled_step.step_id,
        compiled_step.propose_relationship_group_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, proposal.model_dump(mode="python"))
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "propose_relationship_group",
        detail={
            "relationship_type": proposal.relationship_type,
            "candidates_from": compiled_step.propose_relationship_group_spec.candidates_from,
            "signals_from": compiled_step.propose_relationship_group_spec.signals_from,
            "member_count": len(proposal.members),
            "candidate_count": proposal.candidate_count,
            "on_empty": proposal.on_empty,
            "group_created": proposal.group_created,
            "status": proposal.status,
            "signal_sources_used": proposal.signal_sources_used,
        },
    )


def execute_make_entities_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.make_entities_spec is not None
    entity_set = make_entity_set(
        context.config,
        compiled_step.step_id,
        compiled_step.make_entities_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, entity_set.model_dump(mode="python"))
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "make_entities",
        detail={
            "entity_type": entity_set.entity_type,
            "entity_count": len(entity_set.entities),
            "item_count": len(
                resolve_step_items(
                    compiled_step.make_entities_spec.items,
                    context.plan.input_payload,
                    context.step_outputs,
                )
            ),
            "duplicate_input_count": entity_set.duplicate_input_count,
            "conflicting_duplicate_count": entity_set.conflicting_duplicate_count,
        },
    )


def execute_make_relationships_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.make_relationships_spec is not None
    relationship_set = make_relationship_set(
        context.config,
        compiled_step.step_id,
        compiled_step.make_relationships_spec,
        context.plan.input_payload,
        context.step_outputs,
    )
    context.set_step_output(compiled_step, relationship_set.model_dump(mode="python"))
    context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "make_relationships",
        detail={
            "relationship_type": relationship_set.relationship_type,
            "relationship_count": len(relationship_set.relationships),
            "item_count": len(
                resolve_step_items(
                    compiled_step.make_relationships_spec.items,
                    context.plan.input_payload,
                    context.step_outputs,
                )
            ),
            "duplicate_input_count": relationship_set.duplicate_input_count,
            "conflicting_duplicate_count": relationship_set.conflicting_duplicate_count,
        },
    )


def execute_register_source_artifacts_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    """Register source artifacts from in-memory workflow row data.

    Security boundary: this handler only resolves workflow data expressions and
    passes resolved string content to the source-artifact service. It never reads
    files, paths, or URLs; ``original_uri`` is stored as metadata only. If an
    artifact already exists with identical content, registration is a noop;
    differing label/original_uri/retention are NOT applied. Re-register under a
    new id to change retention.
    """
    from cruxible_core.service.source_artifacts import service_register_source_artifact

    assert compiled_step.register_source_artifacts_spec is not None
    spec = compiled_step.register_source_artifacts_spec
    items = resolve_step_items(
        spec.items,
        context.plan.input_payload,
        context.step_outputs,
    )
    step_node = context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "register_source_artifacts",
        detail={
            "item_count": len(items),
            "kind": spec.kind,
            "retention": spec.retention or "manifest_only",
        },
    )

    registered = 0
    noops = 0
    artifact_ids: set[str] = set()
    planned_digests: dict[str, str] = {}
    store = context.instance.get_source_artifact_store()

    try:
        for index, item in enumerate(items):
            artifact_id = _resolve_artifact_id(
                spec.artifact_id,
                context.plan.input_payload,
                context.step_outputs,
                item,
                index,
            )
            artifact_ids.add(artifact_id)
            content = _resolve_source_content(
                spec.content,
                context.plan.input_payload,
                context.step_outputs,
                item,
                index,
                artifact_id,
            )
            content_hash = _sha256_text(content)

            existing = store.get_artifact(artifact_id)
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise QueryExecutionError(
                        "register_source_artifacts row "
                        f"{index} artifact_id '{artifact_id}' already exists with "
                        "different content digest"
                    )
                noops += 1
                continue

            planned_hash = planned_digests.get(artifact_id)
            if planned_hash is not None:
                if planned_hash != content_hash:
                    raise QueryExecutionError(
                        "register_source_artifacts row "
                        f"{index} artifact_id '{artifact_id}' duplicates an earlier row "
                        "with different content digest"
                    )
                noops += 1
                continue

            label = _resolve_optional_string(
                "label",
                spec.label,
                context.plan.input_payload,
                context.step_outputs,
                item,
                index,
                artifact_id,
            )
            original_uri = _resolve_optional_string(
                "original_uri",
                spec.original_uri,
                context.plan.input_payload,
                context.step_outputs,
                item,
                index,
                artifact_id,
            )
            try:
                result = service_register_source_artifact(
                    context.instance,
                    source_content=content,
                    source_kind=spec.kind,
                    source_retention=spec.retention or "manifest_only",
                    original_uri=original_uri,
                    label=label,
                    actor_context=context.actor_context,
                    source_artifact_id=artifact_id,
                    persist=context.execution_action == "apply",
                )
            except ConfigError as exc:
                raise QueryExecutionError(
                    f"register_source_artifacts row {index} artifact_id '{artifact_id}' failed: {exc}"
                ) from exc
            planned_digests[artifact_id] = result.content_hash
            registered += 1
    finally:
        store.close()

    output = {
        "registered": registered,
        "noops": noops,
        "artifact_ids": sorted(artifact_ids),
    }
    context.set_step_output(compiled_step, output)
    context.apply_previews[compiled_step.step_id] = output
    context.receipt_builder.record_validation(True, detail=output, parent_id=step_node)


def _resolve_artifact_id(
    template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    item: Any,
    index: int,
) -> str:
    value = resolve_value(
        template,
        input_payload,
        step_outputs,
        item_payload=item,
        allow_item=True,
    )
    from cruxible_core.service.source_artifacts import _SOURCE_ARTIFACT_ID_RE

    if not isinstance(value, str) or not _SOURCE_ARTIFACT_ID_RE.fullmatch(value):
        raise QueryExecutionError(
            "register_source_artifacts row "
            f"{index} invalid artifact_id {value!r}: source_artifact_id must be "
            "3-64 chars of [A-Za-z0-9._-] starting with an alphanumeric"
        )
    return value


def _resolve_source_content(
    template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    item: Any,
    index: int,
    artifact_id: str,
) -> str:
    value = resolve_value(
        template,
        input_payload,
        step_outputs,
        item_payload=item,
        allow_item=True,
    )
    if not isinstance(value, str) or value == "":
        raise QueryExecutionError(
            "register_source_artifacts row "
            f"{index} artifact_id '{artifact_id}' content must resolve to a "
            "non-empty string"
        )
    return value


def _resolve_optional_string(
    field_name: str,
    template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    item: Any,
    index: int,
    artifact_id: str,
) -> str | None:
    if template is None:
        return None
    value = resolve_value(
        template,
        input_payload,
        step_outputs,
        item_payload=item,
        allow_item=True,
    )
    if value is None:
        return None
    if not isinstance(value, str):
        raise QueryExecutionError(
            "register_source_artifacts row "
            f"{index} artifact_id '{artifact_id}' {field_name} must resolve to "
            "a string or null"
        )
    return value


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def execute_apply_entities_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.apply_entities_spec is not None
    step_node = context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "apply_entities",
        detail={},
    )
    entity_preview = apply_entity_set(
        context.instance,
        context.graph,
        compiled_step.step_id,
        context.step_outputs[compiled_step.apply_entities_spec.entities_from],
        context.receipt_builder,
        persist_writes=context.execution_action == "apply",
        parent_id=step_node,
        actor_context=context.actor_context,
    )
    preview_payload = entity_preview.model_dump(mode="python")
    context.set_step_output(compiled_step, preview_payload)
    context.apply_previews[compiled_step.step_id] = preview_payload
    context.receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
    if context.execution_action == "apply":
        _collect_entity_delta(
            context.graph,
            context.step_outputs[compiled_step.apply_entities_spec.entities_from],
            context.applied_entities,
        )


def execute_apply_relationships_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.apply_relationships_spec is not None
    step_node = context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "apply_relationships",
        detail={},
    )
    relationship_preview = apply_relationship_set(
        context.instance,
        context.graph,
        context.workflow_name,
        compiled_step.step_id,
        context.step_outputs[compiled_step.apply_relationships_spec.relationships_from],
        context.receipt_builder,
        persist_writes=context.execution_action == "apply",
        parent_id=step_node,
        actor_context=context.actor_context,
    )
    preview_payload = relationship_preview.model_dump(mode="python")
    context.set_step_output(compiled_step, preview_payload)
    context.apply_previews[compiled_step.step_id] = preview_payload
    context.receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
    if context.execution_action == "apply":
        _collect_relationship_delta(
            context.graph,
            context.step_outputs[compiled_step.apply_relationships_spec.relationships_from],
            context.applied_relationships,
        )


def execute_apply_all_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    assert compiled_step.apply_all_spec is not None
    spec = compiled_step.apply_all_spec
    step_node = context.receipt_builder.record_plan_step(
        compiled_step.step_id,
        "apply_all",
        detail={
            "entities_from": spec.entities_from,
            "relationships_from": spec.relationships_from,
        },
    )
    entity_results: dict[str, Any] = {}
    relationship_results: dict[str, Any] = {}
    for alias in spec.entities_from:
        entity_preview = apply_entity_set(
            context.instance,
            context.graph,
            compiled_step.step_id,
            context.step_outputs[alias],
            context.receipt_builder,
            persist_writes=context.execution_action == "apply",
            parent_id=step_node,
            actor_context=context.actor_context,
        )
        entity_results[alias] = entity_preview.model_dump(mode="python")
        if context.execution_action == "apply":
            _collect_entity_delta(
                context.graph,
                context.step_outputs[alias],
                context.applied_entities,
            )
    for alias in spec.relationships_from:
        relationship_preview = apply_relationship_set(
            context.instance,
            context.graph,
            context.workflow_name,
            compiled_step.step_id,
            context.step_outputs[alias],
            context.receipt_builder,
            persist_writes=context.execution_action == "apply",
            parent_id=step_node,
            actor_context=context.actor_context,
        )
        relationship_results[alias] = relationship_preview.model_dump(mode="python")
        if context.execution_action == "apply":
            _collect_relationship_delta(
                context.graph,
                context.step_outputs[alias],
                context.applied_relationships,
            )
    preview_payload = _apply_all_preview_payload(
        spec.entities_from,
        spec.relationships_from,
        entity_results,
        relationship_results,
    )
    context.set_step_output(compiled_step, preview_payload)
    context.apply_previews[compiled_step.step_id] = preview_payload
    context.receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)


def execute_assert_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_assert_step(
        compiled_step,
        context.plan.input_payload,
        context.step_outputs,
        context.receipt_builder,
    )


def execute_assert_not_truncated_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_assert_not_truncated_step(
        compiled_step,
        context.step_outputs,
        context.receipt_builder,
    )


def execute_assert_count_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_assert_count_step(
        compiled_step,
        context.plan.input_payload,
        context.step_outputs,
        context.receipt_builder,
    )


def execute_assert_exists_handler(
    context: WorkflowExecutionContext,
    compiled_step: CompiledPlanStep,
) -> None:
    execute_assert_exists_step(
        compiled_step,
        context.plan.input_payload,
        context.step_outputs,
        context.receipt_builder,
    )


def _collect_entity_delta(
    graph: EntityGraph,
    raw_entity_set: Any,
    target: dict[tuple[str, str], EntityInstance],
) -> None:
    entity_set = EntitySet.model_validate(raw_entity_set)
    for entity in entity_set.entities:
        persisted = graph.get_entity(entity_set.entity_type, entity.entity_id)
        if persisted is not None:
            target[(persisted.entity_type, persisted.entity_id)] = persisted


def _collect_relationship_delta(
    graph: EntityGraph,
    raw_relationship_set: Any,
    target: dict[int, RelationshipInstance],
) -> None:
    relationship_set = RelationshipSet.model_validate(raw_relationship_set)
    for relationship in relationship_set.relationships:
        persisted = graph.get_relationship(
            relationship.from_type,
            relationship.from_id,
            relationship.to_type,
            relationship.to_id,
            relationship_set.relationship_type,
        )
        if persisted is not None and persisted.edge_key is not None:
            target[persisted.edge_key] = persisted


def _apply_all_preview_payload(
    entities_from: list[str],
    relationships_from: list[str],
    entity_results: dict[str, Any],
    relationship_results: dict[str, Any],
) -> dict[str, Any]:
    previews = [entity_results[alias] for alias in entities_from]
    previews.extend(relationship_results[alias] for alias in relationships_from)
    return {
        "entities_from": list(entities_from),
        "relationships_from": list(relationships_from),
        "entity_results": entity_results,
        "relationship_results": relationship_results,
        "create_count": sum(int(preview.get("create_count", 0)) for preview in previews),
        "update_count": sum(int(preview.get("update_count", 0)) for preview in previews),
        "noop_count": sum(int(preview.get("noop_count", 0)) for preview in previews),
        "duplicate_input_count": sum(
            int(preview.get("duplicate_input_count", 0)) for preview in previews
        ),
        "conflicting_duplicate_count": sum(
            int(preview.get("conflicting_duplicate_count", 0)) for preview in previews
        ),
    }


DEFAULT_STEP_HANDLER_REGISTRY = WorkflowStepRegistry(
    [
        ("query", execute_query_handler),
        ("provider", execute_provider_handler),
        ("assert", execute_assert_handler),
        ("assert_not_truncated", execute_assert_not_truncated_handler),
        ("assert_count", execute_assert_count_handler),
        ("assert_exists", execute_assert_exists_handler),
        ("shape_items", execute_shape_items_handler),
        ("join_items", execute_join_items_handler),
        ("filter_items", execute_filter_items_handler),
        ("aggregate_items", execute_aggregate_items_handler),
        ("dedupe_items", execute_dedupe_items_handler),
        ("make_candidates", execute_make_candidates_handler),
        ("map_signals", execute_map_signals_handler),
        ("propose_relationship_group", execute_propose_relationship_group_handler),
        ("make_entities", execute_make_entities_handler),
        ("make_relationships", execute_make_relationships_handler),
        ("register_source_artifacts", execute_register_source_artifacts_handler),
        ("apply_entities", execute_apply_entities_handler),
        ("apply_relationships", execute_apply_relationships_handler),
        ("apply_all", execute_apply_all_handler),
    ]
)
DEFAULT_STEP_HANDLER_REGISTRY.validate_complete()
