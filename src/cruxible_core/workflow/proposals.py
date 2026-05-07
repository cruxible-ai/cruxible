"""Built-in workflow steps for relationship proposals and signals."""

from __future__ import annotations

from typing import Any

from cruxible_core.config.schema import (
    CoreConfig,
    MakeCandidatesSpec,
    MapSignalsSpec,
    ProposeRelationshipGroupSpec,
)
from cruxible_core.errors import QueryExecutionError
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal, SignalValue
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import _resolve_step_items
from cruxible_core.workflow.types import (
    CandidateSet,
    RelationshipGroupProposalArtifact,
    SignalBatch,
    SignalBatchSignal,
)


def _make_candidate_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeCandidatesSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> CandidateSet:
    relationship_type = spec.relationship_type
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{relationship_type}'"
        )

    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: set[tuple[str, str, str, str]] = set()
    candidates: list[RelationshipInstance] = []

    for item in items:
        member = RelationshipInstance.model_validate(
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
            continue
        seen.add(key)
        candidates.append(member)

    return CandidateSet(relationship_type=relationship_type, candidates=candidates)


def _map_signal_batch(
    step_id: str,
    spec: MapSignalsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> SignalBatch:
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
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
            elif numeric_score >= float(score_spec.unsure_gte):
                signal = "unsure"
            else:
                signal = "contradict"
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

        signals.append(
            SignalBatchSignal(
                from_id=from_id,
                to_id=to_id,
                signal=signal,
                evidence=evidence,
            )
        )
        seen_pairs.add(key)

    return SignalBatch(signal_source=spec.signal_source, signals=signals)


def _build_relationship_group_proposal(
    step_id: str,
    spec: ProposeRelationshipGroupSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipGroupProposalArtifact:
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
        )
        for candidate in candidate_set.candidates
    }

    signal_sources_used: list[str] = []
    for alias in spec.signals_from:
        signal_batch = SignalBatch.model_validate(step_outputs[alias])
        signal_sources_used.append(signal_batch.signal_source)
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
                )
            )

    return RelationshipGroupProposalArtifact.model_validate(
        {
            "relationship_type": relationship_type,
            "members": [member.model_dump(mode="python") for member in members_by_pair.values()],
            "thesis_text": resolve_value(
                spec.thesis_text,
                input_payload,
                step_outputs,
            ),
            "thesis_facts": resolve_value(
                spec.thesis_facts,
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
            "suggested_priority": resolve_value(
                spec.suggested_priority,
                input_payload,
                step_outputs,
            ),
            "proposed_by": spec.proposed_by,
        }
    )
