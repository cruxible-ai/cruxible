"""Built-in workflow steps that assemble governed relationship proposals.

These helpers do not write graph state. They turn workflow rows into candidate
relationship facts, attach tri-state evidence signals, and package the result
for the service layer to bridge into a governed candidate group.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from cruxible_core.config.schema import (
    CoreConfig,
    MakeCandidatesSpec,
    MapSignalsSpec,
    ProposeRelationshipGroupSpec,
)
from cruxible_core.errors import QueryExecutionError
from cruxible_core.group.types import (
    CandidateMember,
    CandidateSignal,
    QuerySourceEvidence,
    SignalBucketBasis,
    SignalValue,
)
from cruxible_core.primitives import ordered_unique
from cruxible_core.temporal import format_datetime
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    MAX_DUPLICATE_EXAMPLES,
    query_result_index,
    resolve_step_items,
    source_read_metadata,
)
from cruxible_core.workflow.types import (
    CandidateSet,
    RelationshipGroupProposalArtifact,
    SignalBatch,
    SignalBatchSignal,
)

MAX_QUERY_EVIDENCE_PER_MEMBER = 3
MAX_QUERY_EVIDENCE_PATH_SEGMENTS = 8
MAX_QUERY_EVIDENCE_LIST_ITEMS = 20
MAX_QUERY_EVIDENCE_DICT_ITEMS = 40
MAX_QUERY_EVIDENCE_STRING_LENGTH = 500


def make_candidate_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeCandidatesSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> CandidateSet:
    """Build the set of relationship edges a workflow wants reviewed.

    The ``make_candidates`` step maps each resolved item into a
    ``RelationshipInstance`` for one configured relationship type. It validates
    that the produced endpoint types match the relationship schema, then returns
    an in-memory ``CandidateSet`` artifact for later proposal assembly.

    Candidate creation is intentionally not a graph write. The produced edges
    remain proposed facts until the service proposal/resolve flow accepts them.
    Duplicate candidate rows are deduped, but duplicate diagnostics are retained
    for workflow receipts and debugging.
    """
    relationship_type = spec.relationship_type
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{relationship_type}'"
        )

    items = resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    source_metadata = source_read_metadata(spec.items, step_outputs)
    query_receipt_id = source_metadata.get("receipt_id")
    query_receipt_ids = [query_receipt_id] if isinstance(query_receipt_id, str) else []
    candidates: list[CandidateMember] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []

    for item in items:
        member = CandidateMember.model_validate(
            {
                "relationship_type": relationship_type,
                "from_type": resolve_value(
                    spec.from_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "from_id": resolve_value(
                    spec.from_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_type": resolve_value(
                    spec.to_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_id": resolve_value(
                    spec.to_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "properties": resolve_value(
                    spec.properties,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "source_query_evidence": _query_source_evidence(
                    item,
                    source_metadata=source_metadata,
                ),
            }
        )
        if member.from_type != rel_schema.from_entity or member.to_type != rel_schema.to_entity:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced candidate types "
                f"{member.from_type}->{member.to_type} which do not match "
                "relationship "
                f"'{relationship_type}' "
                f"({rel_schema.from_entity}->{rel_schema.to_entity})"
            )
        key = (member.from_type, member.from_id, member.to_type, member.to_id)
        if key in seen:
            duplicate_input_count += 1
            conflicting = seen[key] != member.properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
                example = {
                    "from_type": member.from_type,
                    "from_id": member.from_id,
                    "to_type": member.to_type,
                    "to_id": member.to_id,
                    "relationship_type": relationship_type,
                    "conflicting": conflicting,
                }
                if conflicting:
                    example["first_properties"] = seen[key]
                    example["duplicate_properties"] = member.properties
                duplicate_examples.append(example)
            continue
        seen[key] = member.properties
        candidates.append(member)

    return CandidateSet(
        relationship_type=relationship_type,
        candidates=candidates,
        query_receipt_ids=query_receipt_ids,
        duplicate_input_count=duplicate_input_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
        duplicate_examples=duplicate_examples,
    )


def _query_source_evidence(
    item: Any,
    *,
    source_metadata: dict[str, Any],
) -> list[QuerySourceEvidence]:
    receipt_id = source_metadata.get("receipt_id")
    if not isinstance(receipt_id, str) or not isinstance(item, dict):
        return []

    source = item.get("source")
    row: dict[str, Any] = source if isinstance(source, dict) else item
    evidence: dict[str, Any] = {"query_receipt_id": receipt_id}
    original_row_index = query_result_index(item)
    if original_row_index is not None:
        evidence["row_index"] = original_row_index
    else:
        evidence["feedback_addressable"] = False
    if isinstance(source_metadata.get("source_step"), str):
        evidence["source_step"] = source_metadata["source_step"]

    row_evidence = _query_row_evidence(row)
    if row_evidence:
        evidence.update(row_evidence)
    else:
        evidence["row_shape"] = "unknown"
    return [QuerySourceEvidence.model_validate(evidence)]


def _query_row_evidence(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("path"), list):
        return {
            "row_shape": "path",
            "entry": _entity_identity(row.get("entry")),
            "result": _entity_identity(row.get("result")),
            "path": [
                _relationship_evidence(segment)
                for segment in row["path"][:MAX_QUERY_EVIDENCE_PATH_SEGMENTS]
                if isinstance(segment, dict)
            ],
        }
    if _has_relationship_identity(row):
        return {
            "row_shape": "relationship",
            "relationship": _relationship_evidence(row),
            "entry": _entity_identity(row.get("entry")),
        }
    if _has_entity_identity(row):
        return {
            "row_shape": "entity",
            "entity": _entity_identity(row),
        }
    return {}


def _relationship_evidence(row: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        key: row.get(key)
        for key in (
            "alias",
            "relationship_type",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "edge_key",
        )
        if key in row
    }
    if isinstance(row.get("properties"), dict):
        evidence["properties"] = _bounded_json_value(row["properties"])
    if isinstance(row.get("metadata"), dict):
        evidence["metadata"] = _bounded_json_value(row["metadata"])
    return evidence


def _entity_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _has_entity_identity(value):
        return None
    return {
        "entity_type": value.get("entity_type"),
        "entity_id": value.get("entity_id"),
    }


def _has_relationship_identity(row: dict[str, Any]) -> bool:
    return all(
        isinstance(row.get(key), str)
        for key in ("relationship_type", "from_type", "from_id", "to_type", "to_id")
    )


def _has_entity_identity(row: dict[str, Any]) -> bool:
    return isinstance(row.get("entity_type"), str) and isinstance(row.get("entity_id"), str)


def _bounded_json_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_QUERY_EVIDENCE_STRING_LENGTH:
            return value
        return f"{value[:MAX_QUERY_EVIDENCE_STRING_LENGTH]}..."
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _bounded_json_value(value[key])
            for key in sorted(value, key=str)[:MAX_QUERY_EVIDENCE_DICT_ITEMS]
        }
    if isinstance(value, list):
        return [
            _bounded_json_value(item)
            for item in value[:MAX_QUERY_EVIDENCE_LIST_ITEMS]
        ]
    return value


def _append_member_query_evidence(
    member: CandidateMember,
    evidence: list[QuerySourceEvidence],
) -> None:
    if not evidence:
        return
    member.source_query_evidence = [
        *member.source_query_evidence,
        *evidence,
    ][:MAX_QUERY_EVIDENCE_PER_MEMBER]


def map_signal_batch(
    step_id: str,
    spec: MapSignalsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> SignalBatch:
    """Map source rows into tri-state evidence for candidate pairs.

    The ``map_signals`` step resolves a pair identity from each item, derives a
    ``support``, ``unsure``, or ``contradict`` value from either a numeric score
    threshold or an enum mapping, and records optional evidence text. Each batch
    represents one named signal source and may contain at most one signal for a
    given pair.

    Signals are evidence about candidates, not candidates themselves. Pair
    membership is checked later when the proposal artifact is assembled.
    """
    items = resolve_step_items(spec.items, input_payload, step_outputs)
    source_metadata = source_read_metadata(spec.items, step_outputs)
    query_receipt_id = source_metadata.get("receipt_id")
    query_receipt_ids = [query_receipt_id] if isinstance(query_receipt_id, str) else []
    seen_pairs: set[tuple[str, str]] = set()
    signals: list[SignalBatchSignal] = []

    for item in items:
        from_id = str(
            resolve_value(
                spec.from_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        to_id = str(
            resolve_value(
                spec.to_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        key = (from_id, to_id)
        if key in seen_pairs:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced duplicate signal for pair {from_id}->{to_id}"
            )

        evidence = ""
        if spec.evidence is not None:
            resolved_evidence = resolve_value(
                spec.evidence,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if resolved_evidence is not None:
                evidence = str(resolved_evidence)

        signal: SignalValue
        basis: SignalBucketBasis
        if spec.score is not None:
            score_spec = spec.score
            score_value = resolve_value(
                f"$item.{score_spec.path}",
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if isinstance(score_value, bool) or not isinstance(score_value, (int, float)):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' score path '{score_spec.path}' "
                    "must resolve to a number"
                )
            numeric_score = float(score_value)
            if numeric_score >= float(score_spec.support_gte):
                signal = "support"
                matched = "support_gte"
            elif numeric_score >= float(score_spec.unsure_gte):
                signal = "unsure"
                matched = "unsure_gte"
            else:
                signal = "contradict"
                matched = "below_unsure_gte"
            basis = SignalBucketBasis(
                mode="score",
                path=score_spec.path,
                value=score_value,
                matched=matched,
            )
        else:
            assert spec.enum is not None
            enum_spec = spec.enum
            enum_value = resolve_value(
                f"$item.{enum_spec.path}",
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
            if not isinstance(enum_value, str):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' enum path '{enum_spec.path}' "
                    "must resolve to a string"
                )
            if enum_value not in enum_spec.map:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' enum path '{enum_spec.path}' returned "
                    f"unknown value '{enum_value}'"
                )
            signal = enum_spec.map[enum_value]
            basis = SignalBucketBasis(
                mode="enum",
                path=enum_spec.path,
                value=enum_value,
                matched=enum_value,
            )

        signals.append(
            SignalBatchSignal(
                from_id=from_id,
                to_id=to_id,
                signal=signal,
                evidence=evidence,
                basis=basis,
                source_query_evidence=_query_source_evidence(
                    item,
                    source_metadata=source_metadata,
                ),
            )
        )
        seen_pairs.add(key)

    return SignalBatch(
        signal_source=spec.signal_source,
        signals=signals,
        query_receipt_ids=query_receipt_ids,
    )


def signal_mapping_snapshot(spec: MapSignalsSpec) -> dict[str, Any]:
    """Return the stable mapping snapshot recorded in workflow receipts."""
    if spec.score is not None:
        return {
            "mode": "score",
            "path": spec.score.path,
            "support_gte": spec.score.support_gte,
            "unsure_gte": spec.score.unsure_gte,
        }
    assert spec.enum is not None
    return {
        "mode": "enum",
        "path": spec.enum.path,
        "map": dict(spec.enum.map),
    }


def build_relationship_group_proposal(
    step_id: str,
    spec: ProposeRelationshipGroupSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipGroupProposalArtifact:
    """Combine candidates, signals, and thesis metadata into a proposal artifact.

    The ``propose_relationship_group`` step joins a prior ``CandidateSet`` with
    one or more ``SignalBatch`` artifacts by candidate pair. It rejects signals
    for unknown candidates and duplicate signal sources for the same pair, then
    returns the single artifact consumed by ``service_propose_workflow``.

    This is the packaging boundary between workflow execution and governed group
    review. Persistence, policy checks, and eventual graph writes remain owned
    by the service layer.
    """
    candidate_set = CandidateSet.model_validate(step_outputs[spec.candidates_from])
    relationship_type = spec.relationship_type
    if candidate_set.relationship_type != relationship_type:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' expected candidate relationship '{relationship_type}' "
            f"but received '{candidate_set.relationship_type}'"
        )

    members_by_pair: dict[tuple[str, str], CandidateMember] = {
        (candidate.from_id, candidate.to_id): CandidateMember(
            from_type=candidate.from_type,
            from_id=candidate.from_id,
            to_type=candidate.to_type,
            to_id=candidate.to_id,
            relationship_type=relationship_type,
            properties=candidate.properties,
            source_query_evidence=candidate.source_query_evidence[
                :MAX_QUERY_EVIDENCE_PER_MEMBER
            ],
        )
        for candidate in candidate_set.candidates
    }

    signal_sources_used: list[str] = []
    query_receipt_ids = list(candidate_set.query_receipt_ids)
    for alias in spec.signals_from:
        signal_batch = SignalBatch.model_validate(step_outputs[alias])
        signal_sources_used.append(signal_batch.signal_source)
        query_receipt_ids.extend(signal_batch.query_receipt_ids)
        for signal in signal_batch.signals:
            key = (signal.from_id, signal.to_id)
            if key not in members_by_pair:
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' received signal for unknown candidate pair "
                    f"{signal.from_id}->{signal.to_id}"
                )
            member = members_by_pair[key]
            if any(
                existing.signal_source == signal_batch.signal_source
                for existing in member.signals
            ):
                raise QueryExecutionError(
                    f"Workflow step '{step_id}' produced duplicate signal source "
                    f"'{signal_batch.signal_source}' for pair {signal.from_id}->{signal.to_id}"
                )
            member.signals.append(
                CandidateSignal(
                    signal_source=signal_batch.signal_source,
                    signal=signal.signal,
                    evidence=signal.evidence,
                    basis=signal.basis,
                )
            )
            _append_member_query_evidence(member, signal.source_query_evidence)

    candidate_count = len(members_by_pair)
    if candidate_count == 0 and spec.on_empty != "complete":
        raise QueryExecutionError(
            f"Workflow step '{step_id}' propose_relationship_group produced no candidates"
        )

    return RelationshipGroupProposalArtifact.model_validate(
        {
            "relationship_type": relationship_type,
            "proposal_step_id": step_id,
            "candidates_from": spec.candidates_from,
            "members": [member.model_dump(mode="python") for member in members_by_pair.values()],
            "status": "no_candidates" if candidate_count == 0 else "ready",
            "candidate_count": candidate_count,
            "on_empty": spec.on_empty,
            "group_created": False if candidate_count == 0 else None,
            "thesis_text": resolve_value(
                spec.thesis_text,
                input_payload,
                step_outputs,
            ),
            "pending_refresh_mode": spec.pending_refresh_mode,
            "analysis_state": resolve_value(
                spec.analysis_state,
                input_payload,
                step_outputs,
            ),
            "signal_sources_used": signal_sources_used,
            "query_receipt_ids": ordered_unique(query_receipt_ids),
            "suggested_priority": resolve_value(
                spec.suggested_priority,
                input_payload,
                step_outputs,
            ),
            "proposed_by": spec.proposed_by,
        }
    )
