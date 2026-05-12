"""Workflow execution runtime."""

from __future__ import annotations

from typing import Any, Literal

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.apply import (
    apply_entity_set,
    apply_relationship_set,
    compute_apply_digest,
    make_entity_set,
    make_relationship_set,
)
from cruxible_core.workflow.compiler import compile_workflow, load_lock, resolve_lock_path
from cruxible_core.workflow.io import (
    execute_assert_step,
    execute_provider_step,
    execute_query_step,
    list_entities_step,
    list_relationships_step,
)
from cruxible_core.workflow.proposals import (
    build_relationship_group_proposal,
    make_candidate_set,
    map_signal_batch,
    signal_mapping_snapshot,
)
from cruxible_core.workflow.step_helpers import resolve_step_items
from cruxible_core.workflow.tracing import persist_receipt as persist_workflow_receipt
from cruxible_core.workflow.transforms import (
    dedupe_items,
    filter_items,
    join_items,
    shape_items,
)
from cruxible_core.workflow.types import WorkflowExecutionResult


def execute_workflow(
    instance: InstanceProtocol,
    config: CoreConfig,
    workflow_name: str,
    input_payload: dict[str, Any],
    *,
    mode: Literal["run", "preview", "apply"] = "run",
    persist_receipt: bool = True,
    persist_traces: bool = True,
) -> WorkflowExecutionResult:
    """Execute a workflow against the current instance and persist traces/receipts."""
    lock = load_lock(resolve_lock_path(instance))
    plan = compile_workflow(
        config,
        lock,
        workflow_name,
        input_payload,
        config_base_path=instance.get_config_path().parent,
    )
    workflow = config.workflows[workflow_name]
    workflow_type = workflow.type
    is_canonical = workflow_type == "canonical"
    if is_canonical and mode == "run":
        raise ConfigError("canonical workflows must be executed in preview or apply mode")
    if not is_canonical and mode != "run":
        raise ConfigError("only canonical workflows support preview or apply mode")
    execution_mode: Literal["run", "preview", "apply", "proposal"] = (
        "proposal" if workflow_type == "proposal" else mode
    )
    head_snapshot_id = instance.get_head_snapshot_id()
    base_graph = instance.load_graph()
    graph = _clone_graph(base_graph) if is_canonical else base_graph
    receipt_builder = ReceiptBuilder(
        query_name=workflow_name,
        parameters=plan.input_payload,
        operation_type="workflow",
        head_snapshot_id=head_snapshot_id,
        workflow_mode=execution_mode,
    )

    step_outputs: dict[str, Any] = {}
    alias_step_ids: dict[str, str] = {}
    step_trace_ids: dict[str, list[str]] = {}
    query_receipt_ids: list[str] = []
    traces: list[ExecutionTrace] = []
    apply_previews: dict[str, Any] = {}

    for compiled_step in plan.steps:
        if compiled_step.kind == "query":
            execute_query_step(
                instance,
                config,
                graph,
                plan,
                compiled_step,
                step_outputs,
                alias_step_ids,
                query_receipt_ids,
                receipt_builder,
                persist_receipt=persist_receipt,
            )
            continue

        if compiled_step.kind == "provider":
            execute_provider_step(
                instance,
                config,
                lock,
                plan,
                compiled_step,
                step_outputs,
                alias_step_ids,
                traces,
                step_trace_ids,
                receipt_builder,
                workflow_name=workflow_name,
                persist_traces=persist_traces,
                config_base_path=instance.get_config_path().parent,
            )
            continue

        if compiled_step.kind == "list_entities":
            assert compiled_step.list_entities_spec is not None
            entity_list = list_entities_step(
                config,
                graph,
                compiled_step.step_id,
                compiled_step.list_entities_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = entity_list
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "list_entities",
                detail={
                    "entity_type": compiled_step.list_entities_spec.entity_type,
                    "item_count": len(entity_list["items"]),
                    "total": entity_list["total"],
                },
            )
            continue

        if compiled_step.kind == "list_relationships":
            assert compiled_step.list_relationships_spec is not None
            relationship_list = list_relationships_step(
                graph,
                compiled_step.step_id,
                compiled_step.list_relationships_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = relationship_list
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "list_relationships",
                detail={
                    "relationship_type": (compiled_step.list_relationships_spec.relationship_type),
                    "item_count": len(relationship_list["items"]),
                    "total": relationship_list["total"],
                },
            )
            continue

        if compiled_step.kind == "shape_items":
            assert compiled_step.shape_items_spec is not None
            shaped_items = shape_items(
                compiled_step.step_id,
                compiled_step.shape_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = shaped_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "shape_items",
                detail={
                    "input_count": shaped_items["input_count"],
                    "output_count": shaped_items["output_count"],
                    "dropped_count": shaped_items["dropped_count"],
                },
            )
            continue

        if compiled_step.kind == "join_items":
            assert compiled_step.join_items_spec is not None
            joined_items = join_items(
                compiled_step.step_id,
                compiled_step.join_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = joined_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
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
            continue

        if compiled_step.kind == "filter_items":
            assert compiled_step.filter_items_spec is not None
            filtered_items = filter_items(
                compiled_step.step_id,
                compiled_step.filter_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = filtered_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "filter_items",
                detail={
                    "input_count": filtered_items["input_count"],
                    "output_count": filtered_items["output_count"],
                    "filtered_count": filtered_items["filtered_count"],
                },
            )
            continue

        if compiled_step.kind == "dedupe_items":
            assert compiled_step.dedupe_items_spec is not None
            deduped_items = dedupe_items(
                compiled_step.step_id,
                compiled_step.dedupe_items_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = deduped_items
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "dedupe_items",
                detail={
                    "input_count": deduped_items["input_count"],
                    "output_count": deduped_items["output_count"],
                    "duplicate_count": deduped_items["duplicate_count"],
                },
            )
            continue

        if compiled_step.kind == "make_candidates":
            assert compiled_step.make_candidates_spec is not None
            candidate_set = make_candidate_set(
                config,
                compiled_step.step_id,
                compiled_step.make_candidates_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = candidate_set.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
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
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                },
            )
            continue

        if compiled_step.kind == "map_signals":
            assert compiled_step.map_signals_spec is not None
            signal_batch = map_signal_batch(
                compiled_step.step_id,
                compiled_step.map_signals_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = signal_batch.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "map_signals",
                detail={
                    "signal_source": signal_batch.signal_source,
                    "signal_count": len(signal_batch.signals),
                    "mapping": signal_mapping_snapshot(compiled_step.map_signals_spec),
                    "item_count": len(
                        resolve_step_items(
                            compiled_step.map_signals_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                },
            )
            continue

        if compiled_step.kind == "propose_relationship_group":
            assert compiled_step.propose_relationship_group_spec is not None
            proposal = build_relationship_group_proposal(
                compiled_step.step_id,
                compiled_step.propose_relationship_group_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = proposal.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "propose_relationship_group",
                detail={
                    "relationship_type": proposal.relationship_type,
                    "candidates_from": (
                        compiled_step.propose_relationship_group_spec.candidates_from
                    ),
                    "signals_from": (compiled_step.propose_relationship_group_spec.signals_from),
                    "member_count": len(proposal.members),
                    "signal_sources_used": proposal.signal_sources_used,
                },
            )
            continue

        if compiled_step.kind == "make_entities":
            assert compiled_step.make_entities_spec is not None
            entity_set = make_entity_set(
                config,
                compiled_step.step_id,
                compiled_step.make_entities_spec,
                plan.input_payload,
                step_outputs,
            )
            step_outputs[compiled_step.as_name or compiled_step.step_id] = entity_set.model_dump(
                mode="python"
            )
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "make_entities",
                detail={
                    "entity_type": entity_set.entity_type,
                    "entity_count": len(entity_set.entities),
                    "item_count": len(
                        resolve_step_items(
                            compiled_step.make_entities_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                    "duplicate_input_count": entity_set.duplicate_input_count,
                    "conflicting_duplicate_count": entity_set.conflicting_duplicate_count,
                },
            )
            continue

        if compiled_step.kind == "make_relationships":
            assert compiled_step.make_relationships_spec is not None
            relationship_set = make_relationship_set(
                config,
                compiled_step.step_id,
                compiled_step.make_relationships_spec,
                plan.input_payload,
                step_outputs,
            )
            alias = compiled_step.as_name or compiled_step.step_id
            step_outputs[alias] = relationship_set.model_dump(mode="python")
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_plan_step(
                compiled_step.step_id,
                "make_relationships",
                detail={
                    "relationship_type": relationship_set.relationship_type,
                    "relationship_count": len(relationship_set.relationships),
                    "item_count": len(
                        resolve_step_items(
                            compiled_step.make_relationships_spec.items,
                            plan.input_payload,
                            step_outputs,
                        )
                    ),
                    "duplicate_input_count": relationship_set.duplicate_input_count,
                    "conflicting_duplicate_count": relationship_set.conflicting_duplicate_count,
                },
            )
            continue

        if compiled_step.kind == "apply_entities":
            assert compiled_step.apply_entities_spec is not None
            step_node = receipt_builder.record_plan_step(
                compiled_step.step_id,
                "apply_entities",
                detail={},
            )
            entity_preview = apply_entity_set(
                instance,
                graph,
                compiled_step.step_id,
                step_outputs[compiled_step.apply_entities_spec.entities_from],
                receipt_builder,
                persist_writes=execution_mode == "apply",
                parent_id=step_node,
            )
            preview_payload = entity_preview.model_dump(mode="python")
            step_outputs[compiled_step.as_name or compiled_step.step_id] = preview_payload
            apply_previews[compiled_step.step_id] = preview_payload
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
            continue

        if compiled_step.kind == "apply_relationships":
            assert compiled_step.apply_relationships_spec is not None
            step_node = receipt_builder.record_plan_step(
                compiled_step.step_id,
                "apply_relationships",
                detail={},
            )
            relationship_preview = apply_relationship_set(
                instance,
                graph,
                workflow_name,
                compiled_step.step_id,
                step_outputs[compiled_step.apply_relationships_spec.relationships_from],
                receipt_builder,
                persist_writes=execution_mode == "apply",
                parent_id=step_node,
            )
            preview_payload = relationship_preview.model_dump(mode="python")
            step_outputs[compiled_step.as_name or compiled_step.step_id] = preview_payload
            apply_previews[compiled_step.step_id] = preview_payload
            if compiled_step.as_name is not None:
                alias_step_ids[compiled_step.as_name] = compiled_step.step_id
            receipt_builder.record_validation(True, detail=preview_payload, parent_id=step_node)
            continue

        assert compiled_step.assert_spec is not None
        execute_assert_step(
            instance,
            compiled_step,
            plan.input_payload,
            step_outputs,
            receipt_builder,
            persist_receipt=persist_receipt,
        )

    output = step_outputs[plan.returns]
    receipt_builder.record_results([{"output": output}])
    receipt = receipt_builder.build(results=[{"output": output}])
    apply_digest = compute_apply_digest(plan, head_snapshot_id, apply_previews)
    committed_snapshot_id: str | None = None
    receipt.nodes[0].detail.update(
        {
            "mode": execution_mode,
            "config_digest": plan.config_digest,
            "lock_digest": plan.lock_digest,
            "apply_digest": apply_digest,
        }
    )

    if is_canonical and execution_mode == "apply":
        snapshot = instance.commit_graph_snapshot(graph)
        committed_snapshot_id = snapshot.snapshot_id
        receipt.nodes[0].detail["committed_snapshot_id"] = committed_snapshot_id
        receipt.committed = True

    if persist_receipt:
        persist_workflow_receipt(instance, receipt)

    return WorkflowExecutionResult(
        workflow=workflow_name,
        output=output,
        receipt=receipt,
        mode=execution_mode,
        workflow_type=workflow_type,
        apply_digest=apply_digest,
        head_snapshot_id=head_snapshot_id,
        committed_snapshot_id=committed_snapshot_id,
        apply_previews=apply_previews,
        query_receipt_ids=query_receipt_ids,
        traces=traces,
        step_outputs=step_outputs,
        alias_step_ids=alias_step_ids,
        step_trace_ids=step_trace_ids,
    )


def _clone_graph(graph: EntityGraph) -> EntityGraph:
    return EntityGraph.from_dict(graph.to_dict())
