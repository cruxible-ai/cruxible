"""Mutation service functions — add_entities and add_relationships."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    DataValidationError,
    GroupApprovedContentWriteRefusedError,
)
from cruxible_core.governance.actors import GovernedActorContext, dump_actor_context
from cruxible_core.graph.assertion_state import (
    GroupApprovalDrift,
    RelationshipLifecycleState,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import EvidenceRef, RelationshipEvidence
from cruxible_core.graph.operations import (
    ValidatedEntity,
    ValidatedRelationship,
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.provenance import SOURCE_REF_BATCH_DIRECT_WRITE, provenance_group_id
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.group.types import CandidateGroup
from cruxible_core.instance_protocol import (
    GroupStoreProtocol,
    InstanceProtocol,
    ResolutionContractStoreProtocol,
)
from cruxible_core.service.direct_write_policy import (
    effective_relationship_write_policy,
    is_governed_source,
)
from cruxible_core.service.evidence import resolve_evidence_refs
from cruxible_core.service.mutation_guards import (
    ContractActivationIntent,
    GuardEvaluation,
    build_guard_write_delta,
    evaluate_mutation_guards,
    evaluate_relationship_mutation_guards,
    receipt_creation_actor_resolver,
    record_contract_activations,
    record_guard_evaluation,
)
from cruxible_core.service.mutation_proposals import (
    batch_direct_write_proposal,
    build_proposal,
    entity_instance_member,
    relationship_instance_member,
)
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.property_diffs import property_delta, property_value_changes
from cruxible_core.service.types import (
    AddEntityResult,
    AddRelationshipResult,
    BatchDirectWriteInput,
    BatchDirectWriteResult,
    BatchRelationshipWriteInput,
    DirectWriteGroupInteraction,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
)
from cruxible_core.temporal import format_datetime, utc_now

_DIRECT_WRITE_CONFLICTS_KEY = "direct_write_conflicts"
_DIRECT_WRITE_CONFLICT_SUMMARY_KEY = "direct_write_conflict_summary"
_DIRECT_WRITE_CONFLICT_REVIEW_HINT = "live_state_changed_since_proposal"

# Per-tuple retention cap on the append-only conflict log.
#
# The log is append-only because the reviewer needs the HISTORY of how many
# times live state moved under their proposal, not just the latest move —
# replacing the record erased the earlier ``detected_at``/``receipt_id``. Append
# without a bound, though, lets a hot tuple grow the group's analysis_state
# without limit.
#
# The cap is PER TUPLE rather than per group on purpose: the summary counts
# DISTINCT conflicted tuples, so a global cap could evict every record for a
# tuple and silently drop its count. Bounding per tuple guarantees each
# conflicted tuple keeps at least one record, which keeps the summary exact by
# construction. Total growth is therefore bounded by member_count * this cap.
_MAX_CONFLICT_RECORDS_PER_TUPLE = 20


@dataclass
class _PreparedBatchRelationship:
    validated: ValidatedRelationship
    relationship: RelationshipInstance
    evidence_refs: list[EvidenceRef]
    pending: bool = False
    # Typed, review-SAFE lifecycle override; applied to ``assertion.lifecycle``
    # only (see ``apply_relationship``). ``None`` leaves lifecycle at its add/
    # update default.
    lifecycle: RelationshipLifecycleState | None = None


@dataclass
class _PreparedBatchDirectWrite:
    graph: EntityGraph
    entities: list[ValidatedEntity]
    entity_write_details: dict[tuple[str, str], dict[str, Any]]
    relationships: list[_PreparedBatchRelationship]
    validation_errors: list[str]
    validation_warnings: list[str]
    evidence_sources_used: list[str]
    interactions: _DirectWriteGroupInteractions
    contract_activations: tuple[ContractActivationIntent, ...] = ()


@dataclass
class _ContentDrift:
    """One group-approved edge whose content this write changes.

    Held keyed by relationship identity so the stamping pass after
    ``apply_relationship`` can find it without re-diffing the (already
    overwritten) edge.
    """

    group_id: str
    changed_properties: list[str]
    approved_values: dict[str, Any]


@dataclass
class _DirectWriteGroupInteractions:
    pending_conflicts: list[DirectWriteGroupInteraction]
    updated_group_backed_edges: list[DirectWriteGroupInteraction]
    # Identity -> drift, populated only for group-backed edges whose content the
    # incoming write actually changes. Deliberately NOT surfaced on
    # ``DirectWriteGroupInteraction``: the dataclass -> contract mapper lives in
    # ``runtime/api.py``, and a contract field the mapper never fills would read
    # as "no drift ever" over HTTP. The drift is recorded where the ruling
    # requires it — on the edge and on the receipt.
    content_drift: dict[tuple[str, str, str, str, str], _ContentDrift] = field(default_factory=dict)


def _entity_property_change_detail(
    graph: EntityGraph,
    validated: ValidatedEntity,
    *,
    actor_context: GovernedActorContext | None = None,
) -> dict[str, Any]:
    entity = validated.entity
    dumped_actor = dump_actor_context(actor_context)
    previous = graph.get_entity(entity.entity_type, entity.entity_id)
    previous_properties = previous.properties if previous is not None else {}
    if validated.is_update:
        property_changes = property_value_changes(
            entity.properties,
            previous_properties,
            include_added=True,
            include_removed=False,
        )
        change_kind = "updated"
    else:
        property_changes = property_value_changes(
            entity.properties,
            {},
            include_added=True,
            include_removed=False,
        )
        change_kind = "created"

    detail: dict[str, Any] = {
        "change_kind": change_kind,
        "property_changes": [
            {
                "property": change.property,
                "from_value": change.from_value,
                "to_value": change.to_value,
            }
            for change in property_changes
        ],
    }
    if dumped_actor is not None:
        detail["actor_context"] = dumped_actor
    return detail


def _group_interaction_payload(
    interaction: DirectWriteGroupInteraction,
) -> dict[str, Any]:
    return {
        "relationship_type": interaction.relationship_type,
        "from_type": interaction.from_type,
        "from_id": interaction.from_id,
        "to_type": interaction.to_type,
        "to_id": interaction.to_id,
        "group_id": interaction.group_id,
        "group_status": interaction.group_status,
        "group_signature": interaction.group_signature,
        "source_workflow_name": interaction.source_workflow_name,
        "edge_key": interaction.edge_key,
    }


def _group_interaction_from_relationship(
    relationship: RelationshipInstance,
    *,
    group_id: str,
    group: CandidateGroup | None,
    edge_key: int | None,
) -> DirectWriteGroupInteraction:
    return DirectWriteGroupInteraction(
        relationship_type=relationship.relationship_type,
        from_type=relationship.from_type,
        from_id=relationship.from_id,
        to_type=relationship.to_type,
        to_id=relationship.to_id,
        group_id=group_id,
        group_status=group.status if group is not None else None,
        group_signature=group.signature if group is not None else None,
        source_workflow_name=group.source_workflow_name if group is not None else None,
        edge_key=edge_key,
    )


def _content_change(
    incoming: RelationshipInstance,
    existing: RelationshipInstance,
) -> list[str]:
    """Return the property names this write changes on ``existing``.

    ``incoming`` must be the VALIDATED relationship, not the raw payload: the
    update branch of ``apply_relationship`` replaces properties wholesale with
    the validated (merged onto the existing edge, coerced, defaulted) values, so
    that is what actually lands. Diffing the raw payload instead would report a
    property-less touch as "removed everything" and a ``"5"``-for-``5`` restate
    as a change — an over-eager marker, which is worse than no marker.
    """
    delta = property_delta(dict(incoming.properties), dict(existing.properties))
    return sorted({*delta.changed, *delta.added, *delta.removed})


def _detect_direct_write_group_interactions(
    instance: InstanceProtocol,
    graph: EntityGraph,
    relationships: Sequence[RelationshipInstance],
    *,
    config: CoreConfig,
    source: str,
    pending_flags: Sequence[bool],
    group_store: GroupStoreProtocol | None = None,
) -> _DirectWriteGroupInteractions:
    if not relationships:
        return _DirectWriteGroupInteractions(
            pending_conflicts=[],
            updated_group_backed_edges=[],
        )

    store = group_store if group_store is not None else instance.get_group_store()
    close_store = group_store is None
    try:
        pending_conflicts: list[DirectWriteGroupInteraction] = []
        tuples_by_type: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
        for relationship in relationships:
            tuples_by_type[relationship.relationship_type].append(relationship.identity_tuple())

        pending_by_type: dict[str, dict[tuple[str, str, str, str, str], CandidateGroup]] = {}
        for relationship_type, tuples in tuples_by_type.items():
            pending_by_type[relationship_type] = store.find_pending_groups_for_tuples(
                relationship_type,
                tuples,
                statuses=("pending_review", "applying"),
            )

        group_cache: dict[str, CandidateGroup | None] = {}
        updated_group_backed_edges: list[DirectWriteGroupInteraction] = []
        content_drift: dict[tuple[str, str, str, str, str], _ContentDrift] = {}
        for index, relationship in enumerate(relationships):
            pending_group = pending_by_type.get(relationship.relationship_type, {}).get(
                relationship.identity_tuple()
            )
            if pending_group is not None:
                pending_conflicts.append(
                    _group_interaction_from_relationship(
                        relationship,
                        group_id=pending_group.group_id,
                        group=pending_group,
                        edge_key=None,
                    )
                )

            existing = graph.get_relationship(
                relationship.from_type,
                relationship.from_id,
                relationship.to_type,
                relationship.to_id,
                relationship.relationship_type,
            )
            if existing is None or existing.metadata.provenance is None:
                continue
            group_id = provenance_group_id(existing.metadata.provenance)
            if group_id is None:
                continue
            if group_id not in group_cache:
                group_cache[group_id] = store.get_group(group_id)
            updated_group_backed_edges.append(
                _group_interaction_from_relationship(
                    relationship,
                    group_id=group_id,
                    group=group_cache[group_id],
                    edge_key=existing.edge_key,
                )
            )

            # CONTENT-BINDING RULING: a group approval accepted this edge's
            # CONTENT, not just its existence. A write that changes nothing is
            # not drift and must be left alone entirely.
            changed = _content_change(relationship, existing)
            if not changed:
                continue
            policy = effective_relationship_write_policy(config, relationship.relationship_type)
            if policy == "proposal_only":
                pending = pending_flags[index] if index < len(pending_flags) else False
                if is_governed_source(source) or pending:
                    # Only refuse where the chokepoint in ``graph/operations.py``
                    # would let the write through — it exempts governed sources
                    # and pending stages. Everything else is already refused
                    # there, and shadowing it would swap a settled error for a
                    # new one on paths that have no gap.
                    raise GroupApprovedContentWriteRefusedError(
                        relationship.relationship_type,
                        relationship.from_type,
                        relationship.from_id,
                        relationship.to_type,
                        relationship.to_id,
                        group_id=group_id,
                        changed_properties=changed,
                    )
                continue
            content_drift[relationship.identity_tuple()] = _ContentDrift(
                group_id=group_id,
                changed_properties=changed,
                approved_values={
                    name: existing.properties[name]
                    for name in changed
                    if name in existing.properties
                },
            )
    finally:
        if close_store:
            store.close()

    return _DirectWriteGroupInteractions(
        pending_conflicts=pending_conflicts,
        updated_group_backed_edges=updated_group_backed_edges,
        content_drift=content_drift,
    )


def _record_group_interaction_validation(
    builder: Any | None,
    interactions: _DirectWriteGroupInteractions,
) -> None:
    if builder is None:
        return
    detail: dict[str, Any] = {}
    if interactions.pending_conflicts:
        detail["pending_conflicts"] = [
            _group_interaction_payload(interaction)
            for interaction in interactions.pending_conflicts
        ]
    if interactions.updated_group_backed_edges:
        detail["updated_group_backed_edges"] = [
            _group_interaction_payload(interaction)
            for interaction in interactions.updated_group_backed_edges
        ]
    if detail:
        builder.record_validation(passed=True, detail=detail)


def _relationship_group_interaction_detail(
    relationship: RelationshipInstance,
    interactions: _DirectWriteGroupInteractions,
) -> dict[str, Any]:
    identity = relationship.identity_tuple()
    detail: dict[str, Any] = {}
    pending_conflicts = [
        _group_interaction_payload(interaction)
        for interaction in interactions.pending_conflicts
        if (
            interaction.from_type,
            interaction.from_id,
            interaction.to_type,
            interaction.to_id,
            interaction.relationship_type,
        )
        == identity
    ]
    if pending_conflicts:
        detail["pending_conflicts"] = pending_conflicts
    updated_group_backed_edges = [
        _group_interaction_payload(interaction)
        for interaction in interactions.updated_group_backed_edges
        if (
            interaction.from_type,
            interaction.from_id,
            interaction.to_type,
            interaction.to_id,
            interaction.relationship_type,
        )
        == identity
    ]
    if updated_group_backed_edges:
        detail["updated_group_backed_edges"] = updated_group_backed_edges
    drift = interactions.content_drift.get(identity)
    if drift is not None:
        # The act of overwriting group-approved content must be auditable on the
        # receipt, not only discoverable by reading the edge afterwards.
        detail["group_approval_drift"] = _content_drift_payload(drift)
    return detail


def _content_drift_payload(drift: _ContentDrift) -> dict[str, Any]:
    return {
        "group_id": drift.group_id,
        "changed_properties": list(drift.changed_properties),
        "approved_values": dict(drift.approved_values),
    }


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str, str, str, str] | None:
    try:
        return RelationshipInstance.model_validate(record).identity_tuple()
    except ValidationError:
        return None


def _direct_write_conflict_record(
    *,
    graph: EntityGraph,
    interaction: DirectWriteGroupInteraction,
    receipt_id: str | None,
    detected_at: str,
    source: str,
    source_ref: str,
    actor_context: GovernedActorContext | None,
) -> dict[str, Any]:
    persisted = graph.get_relationship(
        interaction.from_type,
        interaction.from_id,
        interaction.to_type,
        interaction.to_id,
        interaction.relationship_type,
    )
    record: dict[str, Any] = {
        "relationship_type": interaction.relationship_type,
        "from_type": interaction.from_type,
        "from_id": interaction.from_id,
        "to_type": interaction.to_type,
        "to_id": interaction.to_id,
        "receipt_id": receipt_id,
        "edge_key": persisted.edge_key if persisted is not None else interaction.edge_key,
        "detected_at": detected_at,
        "source": source,
        "source_ref": source_ref,
    }
    dumped_actor = dump_actor_context(actor_context)
    if dumped_actor is not None:
        # Every other governed write record names its actor; the conflict record
        # alone said WHAT moved under the reviewer's proposal but never WHO.
        record["actor_context"] = dumped_actor
    return record


def _bounded_conflict_log(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Apply the per-tuple retention cap, returning the log and the drop count.

    Retention keeps the FIRST record for a tuple and the most recent
    ``_MAX_CONFLICT_RECORDS_PER_TUPLE - 1``. Both ends carry distinct review
    value: the first ``detected_at`` says when live state first moved under the
    proposal, and the latest records say what it is doing now. Dropping is from
    the middle, and never silent — the count lands in the summary.
    """
    positions_by_identity: dict[tuple[str, str, str, str, str], list[int]] = defaultdict(list)
    for position, record in enumerate(records):
        identity = _record_identity(record)
        if identity is None:
            continue
        positions_by_identity[identity].append(position)

    keep: set[int] = set()
    dropped = 0
    for positions in positions_by_identity.values():
        if len(positions) <= _MAX_CONFLICT_RECORDS_PER_TUPLE:
            keep.update(positions)
            continue
        retained = {positions[0], *positions[-(_MAX_CONFLICT_RECORDS_PER_TUPLE - 1) :]}
        keep.update(retained)
        dropped += len(positions) - len(retained)

    # Rebuild in original append order so the log stays chronological across
    # tuples, not grouped by tuple.
    return [dict(records[position]) for position in sorted(keep)], dropped


def _merge_direct_write_conflict_state(
    *,
    group: CandidateGroup,
    members: Sequence[RelationshipInstance],
    new_records: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis_state = deepcopy(group.analysis_state)
    existing_records = analysis_state.get(_DIRECT_WRITE_CONFLICTS_KEY, [])

    # APPEND-ONLY: a second conflict on the same tuple is a second event, not a
    # correction of the first. Keying by identity and overwriting destroyed the
    # earlier detected_at/receipt_id, so the reviewer could not see that live
    # state had moved under the proposal repeatedly.
    log: list[Mapping[str, Any]] = []
    if isinstance(existing_records, list):
        log.extend(item for item in existing_records if isinstance(item, Mapping))
    log.extend(new_records)

    retained, dropped = _bounded_conflict_log(log)
    # Cumulative: each merge only sees the records it drops NOW, but the count
    # must stay a truthful total of what the reviewer can no longer read.
    previous_summary = analysis_state.get(_DIRECT_WRITE_CONFLICT_SUMMARY_KEY)
    if isinstance(previous_summary, Mapping):
        previously_dropped = previous_summary.get("dropped_record_count")
        if isinstance(previously_dropped, int):
            dropped += previously_dropped

    # DISTINCT tuples, not raw record count: the summary answers "how much of
    # this proposal has live state moved under?", which append-only records
    # would otherwise inflate on every repeat write to one member.
    conflicted_identities = {
        identity
        for identity in (_record_identity(record) for record in retained)
        if identity is not None
    }
    member_identities = {member.identity_tuple() for member in members}
    conflicted_member_identities = member_identities.intersection(conflicted_identities)
    member_count = len(member_identities) if member_identities else group.member_count
    conflicted_member_count = len(conflicted_member_identities)
    coverage = "full" if member_count > 0 and conflicted_member_count >= member_count else "partial"
    last_receipt_id = new_records[-1].get("receipt_id") if new_records else None
    summary = {
        "conflicted_member_count": conflicted_member_count,
        "member_count": member_count,
        "coverage": coverage,
        "last_receipt_id": last_receipt_id,
        "review_hint": _DIRECT_WRITE_CONFLICT_REVIEW_HINT,
        "record_count": len(retained),
        "dropped_record_count": dropped,
    }
    analysis_state[_DIRECT_WRITE_CONFLICTS_KEY] = retained
    analysis_state[_DIRECT_WRITE_CONFLICT_SUMMARY_KEY] = summary
    return analysis_state, summary


def _annotate_direct_write_conflict_groups(
    *,
    graph: EntityGraph,
    group_store: GroupStoreProtocol,
    interactions: _DirectWriteGroupInteractions,
    receipt_id: str | None,
    source: str,
    source_ref: str,
    actor_context: GovernedActorContext | None,
    builder: Any | None,
) -> list[dict[str, Any]]:
    if not interactions.pending_conflicts:
        return []

    detected_at = format_datetime(utc_now())
    assert detected_at is not None
    conflicts_by_group: dict[str, list[DirectWriteGroupInteraction]] = defaultdict(list)
    for interaction in interactions.pending_conflicts:
        conflicts_by_group[interaction.group_id].append(interaction)

    annotations: list[dict[str, Any]] = []
    for group_id, group_interactions in conflicts_by_group.items():
        group = group_store.get_group(group_id)
        if group is None or group.status not in {"pending_review", "applying"}:
            continue
        members = group_store.get_members(group_id)
        new_records = [
            _direct_write_conflict_record(
                graph=graph,
                interaction=interaction,
                receipt_id=receipt_id,
                detected_at=detected_at,
                source=source,
                source_ref=source_ref,
                actor_context=actor_context,
            )
            for interaction in group_interactions
        ]
        analysis_state, summary = _merge_direct_write_conflict_state(
            group=group,
            members=members,
            new_records=new_records,
        )
        group_store.update_group_analysis_state(group_id, analysis_state)
        annotations.append(
            {
                "group_id": group_id,
                "coverage": summary["coverage"],
                "conflicted_member_count": summary["conflicted_member_count"],
                "member_count": summary["member_count"],
            }
        )

    if annotations and builder is not None:
        builder.record_validation(
            passed=True,
            detail={"direct_write_group_annotations": annotations},
        )
    return annotations


def _stamp_group_approval_drift(
    graph: EntityGraph,
    relationship: RelationshipInstance,
    interactions: _DirectWriteGroupInteractions,
    *,
    receipt_id: str | None,
    actor_context: GovernedActorContext | None,
) -> None:
    """Mark a group-approved edge whose content this write just changed.

    Called AFTER ``apply_relationship`` so the marker lands on the edge as
    written. The marker rides on the assertion axis (not review, not lifecycle):
    the edge stays live and stays approved — the world legitimately changed —
    but every ordinary read of it now carries the divergence from what the group
    signed off on.
    """
    drift = interactions.content_drift.get(relationship.identity_tuple())
    if drift is None:
        return
    persisted = graph.get_relationship(
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.relationship_type,
    )
    if persisted is None:
        return

    detected_at = utc_now()
    previous = persisted.metadata.assertion.group_approval_drift
    changed_properties = set(drift.changed_properties)
    approved_values = dict(drift.approved_values)
    first_detected_at = detected_at
    if previous is not None and previous.group_id == drift.group_id:
        # Successive drifts accumulate against the SAME approval: the earlier
        # approved_values are the group's, this write's "before" values are only
        # the previous drift's, so the earlier ones win on overlap.
        changed_properties |= set(previous.changed_properties)
        approved_values = {**approved_values, **previous.approved_values}
        first_detected_at = previous.first_detected_at or previous.detected_at or detected_at

    metadata = persisted.metadata.model_copy(
        update={
            "assertion": persisted.metadata.assertion.model_copy(
                update={
                    "group_approval_drift": GroupApprovalDrift(
                        group_id=drift.group_id,
                        changed_properties=sorted(changed_properties),
                        approved_values=approved_values,
                        first_detected_at=first_detected_at,
                        detected_at=detected_at,
                        receipt_id=receipt_id,
                        actor_context=actor_context,
                    )
                }
            )
        }
    )
    graph.replace_relationship_state(
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.relationship_type,
        properties=persisted.properties,
        metadata=metadata,
        edge_key=persisted.edge_key,
    )


def _entity_from_input(value: EntityWriteInput) -> EntityInstance:
    return EntityInstance(
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        properties=value.properties,
        metadata=EntityMetadata.from_metadata(value.metadata),
    )


def _relationship_from_input(
    instance: InstanceProtocol,
    value: RelationshipWriteInput,
) -> tuple[RelationshipInstance, bool]:
    evidence_refs = resolve_evidence_refs(
        instance,
        evidence_refs=value.evidence_refs,
        source_evidence=value.source_evidence,
    )
    metadata = RelationshipMetadata()
    if evidence_refs or value.evidence_rationale is not None:
        metadata = RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=evidence_refs,
                rationale=value.evidence_rationale,
            )
        )
    return (
        RelationshipInstance(
            from_type=value.from_type,
            from_id=value.from_id,
            relationship_type=value.relationship_type,
            to_type=value.to_type,
            to_id=value.to_id,
            properties=value.properties,
            metadata=metadata,
        ),
        value.pending,
    )


def _shared_evidence_input(value: SharedEvidenceInput | Mapping[str, Any]) -> SharedEvidenceInput:
    if isinstance(value, SharedEvidenceInput):
        return value
    return SharedEvidenceInput(
        evidence_refs=value.get("evidence_refs", ()),
        source_evidence=value.get("source_evidence", ()),
    )


def _relationship_from_batch_input(
    instance: InstanceProtocol,
    value: BatchRelationshipWriteInput,
    shared_evidence: Mapping[str, SharedEvidenceInput | Mapping[str, Any]],
) -> tuple[RelationshipInstance, list[EvidenceRef]]:
    evidence_refs: list[EvidenceRef | Mapping[str, Any]] = []
    source_evidence: list[Any] = []
    for key in value.shared_evidence_keys:
        shared = shared_evidence.get(key)
        if shared is None:
            raise DataValidationError(f"shared_evidence key '{key}' not found")
        shared_input = _shared_evidence_input(shared)
        evidence_refs.extend(shared_input.evidence_refs)
        source_evidence.extend(shared_input.source_evidence)
    evidence_refs.extend(value.evidence_refs)
    source_evidence.extend(value.source_evidence)
    resolved_refs = resolve_evidence_refs(
        instance,
        evidence_refs=evidence_refs,
        source_evidence=source_evidence,
    )
    metadata = RelationshipMetadata()
    if resolved_refs or value.evidence_rationale is not None:
        metadata = RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=resolved_refs,
                rationale=value.evidence_rationale,
            )
        )
    return (
        RelationshipInstance(
            from_type=value.from_type,
            from_id=value.from_id,
            relationship_type=value.relationship_type,
            to_type=value.to_type,
            to_id=value.to_id,
            properties=value.properties,
            metadata=metadata,
        ),
        resolved_refs,
    )


def _record_evidence_sources(
    evidence_sources: list[str],
    evidence_seen: set[str],
    refs: Sequence[EvidenceRef],
) -> None:
    for ref in refs:
        if ref.source not in evidence_seen:
            evidence_seen.add(ref.source)
            evidence_sources.append(ref.source)


def _prepare_batch_direct_write(
    instance: InstanceProtocol,
    payload: BatchDirectWriteInput,
    *,
    source: str,
    source_ref: str,
    actor_context: GovernedActorContext | None = None,
    builder: Any | None = None,
    group_store: GroupStoreProtocol | None = None,
    resolution_contract_store: ResolutionContractStoreProtocol | None = None,
) -> _PreparedBatchDirectWrite:
    config = instance.load_config()
    current_graph = instance.load_graph()
    graph = EntityGraph.from_dict(deepcopy(current_graph.to_dict()))
    errors: list[str] = []
    warnings: list[str] = []
    evidence_sources: list[str] = []
    evidence_seen: set[str] = set()
    entity_seen: set[tuple[str, str]] = set()
    relationship_seen: set[tuple[str, str, str, str, str]] = set()
    validated_entities: list[ValidatedEntity] = []
    entity_write_details: dict[tuple[str, str], dict[str, Any]] = {}
    validated_relationships: list[_PreparedBatchRelationship] = []

    for index, entity in enumerate(payload.entities, start=1):
        entity_key = (entity.entity_type, entity.entity_id)
        if entity_key in entity_seen:
            message = f"Entity {index}: duplicate in batch {entity.entity_type}:{entity.entity_id}"
            errors.append(message)
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"entity": index, "error": "duplicate in batch"},
                )
            continue
        try:
            validated_entity = validate_entity(
                config,
                graph,
                entity.entity_type,
                entity.entity_id,
                entity.properties,
                metadata=entity.metadata,
            )
        except DataValidationError as exc:
            errors.append(f"Entity {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"entity": index, "error": str(exc)},
                )
            continue
        entity_seen.add(entity_key)
        validated_entities.append(validated_entity)
        entity_write_details[entity_key] = _entity_property_change_detail(
            current_graph,
            validated_entity,
            actor_context=actor_context,
        )
        apply_entity(graph, validated_entity, config=config, source=source)
        if builder:
            builder.record_validation(
                passed=True,
                detail={"entity_type": entity.entity_type, "entity_id": entity.entity_id},
            )

    for index, relationship in enumerate(payload.relationships, start=1):
        try:
            edge, refs = _relationship_from_batch_input(
                instance,
                relationship,
                payload.shared_evidence,
            )
        except DataValidationError as exc:
            errors.append(f"Relationship {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": str(exc)},
                )
            continue
        relationship_key = edge.identity_tuple()
        if relationship_key in relationship_seen:
            errors.append(
                f"Relationship {index}: duplicate in batch "
                f"{edge.from_type}:{edge.from_id} "
                f"-[{edge.relationship_type}]-> "
                f"{edge.to_type}:{edge.to_id}"
            )
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": "duplicate in batch"},
                )
            continue
        try:
            validated_relationship = validate_relationship(
                config,
                graph,
                edge.from_type,
                edge.from_id,
                edge.relationship_type,
                edge.to_type,
                edge.to_id,
                edge.properties,
            )
        except DataValidationError as exc:
            errors.append(f"Relationship {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": str(exc)},
                )
            continue
        validated_relationship.relationship.metadata = edge.metadata
        if relationship.pending and validated_relationship.is_update:
            errors.append(
                f"Relationship {index}: pending relationship writes can only create new edges"
            )
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": "pending update not allowed"},
                )
            continue
        relationship_seen.add(relationship_key)
        validated_relationships.append(
            _PreparedBatchRelationship(
                validated=validated_relationship,
                relationship=edge,
                evidence_refs=refs,
                pending=relationship.pending,
                lifecycle=relationship.lifecycle,
            )
        )
        _record_evidence_sources(evidence_sources, evidence_seen, refs)
        if builder:
            builder.record_validation(
                passed=True,
                detail={
                    "from": f"{edge.from_type}:{edge.from_id}",
                    "to": f"{edge.to_type}:{edge.to_id}",
                    "relationship": edge.relationship_type,
                },
            )

    proposed_guard_graph = graph
    if validated_relationships:
        # Apply the validated edges to a throwaway graph through the shared
        # chokepoint. This serves two purposes: (1) it surfaces a
        # DirectWriteRefusedError for proposal_only types here, in prepare, so
        # both the dry-run preview and the live write refuse identically (entity
        # refusal already happens in prepare via apply_entity above); and (2) it
        # yields the post-write proposed graph the mutation guards evaluate
        # against. The guard error collection below still runs only when guards
        # are configured.
        proposed_guard_graph = EntityGraph.from_dict(deepcopy(graph.to_dict()))
        for relationship_item in validated_relationships:
            apply_relationship(
                proposed_guard_graph,
                relationship_item.validated,
                source=source,
                source_ref=source_ref,
                config=config,
                pending=relationship_item.pending,
                lifecycle=relationship_item.lifecycle,
            )

    try:
        guard_evaluation = evaluate_mutation_guards(
            config,
            current_graph=current_graph,
            proposed_graph=proposed_guard_graph,
            entities=validated_entities,
            actor_context=actor_context,
            write_delta=build_guard_write_delta(
                validated_entities,
                [item.validated for item in validated_relationships],
            ),
            creation_actor_resolver=receipt_creation_actor_resolver(instance),
            resolution_contract_store=resolution_contract_store,
        )
    except DataValidationError as exc:
        guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
    if builder:
        record_guard_evaluation(builder, guard_evaluation)
    errors.extend(guard_evaluation.messages)

    try:
        relationship_guard_evaluation = evaluate_relationship_mutation_guards(
            instance,
            config,
            current_graph=current_graph,
            relationships=[item.validated for item in validated_relationships],
        )
    except DataValidationError as exc:
        relationship_guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
    if builder:
        record_guard_evaluation(builder, relationship_guard_evaluation)
    errors.extend(relationship_guard_evaluation.messages)

    interactions = _detect_direct_write_group_interactions(
        instance,
        current_graph,
        # The VALIDATED relationship, not the raw payload: content drift must be
        # measured against what actually lands (see ``_content_change``).
        [item.validated.relationship for item in validated_relationships],
        config=config,
        source=source,
        pending_flags=[item.pending for item in validated_relationships],
        group_store=group_store,
    )
    _record_group_interaction_validation(builder, interactions)

    if errors:
        raise DataValidationError(
            f"Batch direct write validation failed with {len(errors)} error(s)",
            errors=errors,
        )

    return _PreparedBatchDirectWrite(
        graph=graph,
        entities=validated_entities,
        entity_write_details=entity_write_details,
        relationships=validated_relationships,
        validation_errors=errors,
        validation_warnings=warnings,
        evidence_sources_used=evidence_sources,
        interactions=interactions,
        contract_activations=guard_evaluation.contract_activations,
    )


def _batch_direct_write_result(
    prepared: _PreparedBatchDirectWrite,
    *,
    dry_run: bool,
    receipt_id: str | None = None,
) -> BatchDirectWriteResult:
    return BatchDirectWriteResult(
        dry_run=dry_run,
        valid=not prepared.validation_errors,
        entities_added=sum(1 for item in prepared.entities if not item.is_update),
        entities_updated=sum(1 for item in prepared.entities if item.is_update),
        relationships_added=sum(
            1 for item in prepared.relationships if not item.validated.is_update
        ),
        relationships_updated=sum(1 for item in prepared.relationships if item.validated.is_update),
        validation_errors=list(prepared.validation_errors),
        validation_warnings=list(prepared.validation_warnings),
        evidence_sources_used=list(prepared.evidence_sources_used),
        pending_conflicts=list(prepared.interactions.pending_conflicts),
        updated_group_backed_edges=list(prepared.interactions.updated_group_backed_edges),
        receipt_id=receipt_id,
    )


def service_batch_direct_write(
    instance: InstanceProtocol,
    payload: BatchDirectWriteInput,
    *,
    dry_run: bool = False,
    source: str = "batch_direct_write",
    source_ref: str = SOURCE_REF_BATCH_DIRECT_WRITE,
    actor_context: GovernedActorContext | None = None,
) -> BatchDirectWriteResult:
    """Validate or apply one direct entity/relationship write payload.

    ``dry_run`` previews: refusals raised here (pending-edge, write-policy,
    validation) carry identical semantics to the applied path but are NOT
    receipted — a preview persists nothing, and receipts record what happened,
    not what was previewed.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity.entity_type for entity in payload.entities],
        relationship_types=[
            relationship.relationship_type for relationship in payload.relationships
        ],
    )

    if dry_run:
        # A preview opens no unit of work, so it reads eligibility on its own
        # connection. That is safe precisely because it writes nothing: the
        # activation intents it produces are discarded, so no contract is
        # consumed by a write that never happened.
        preview_contract_store = instance.get_resolution_contract_store()
        try:
            prepared = _prepare_batch_direct_write(
                instance,
                payload,
                source=source,
                source_ref=source_ref,
                actor_context=actor_context,
                resolution_contract_store=preview_contract_store,
            )
        finally:
            preview_contract_store.close()
        return _batch_direct_write_result(prepared, dry_run=True)

    with mutation_receipt(
        instance,
        "batch_direct_write",
        {
            "entity_count": len(payload.entities),
            "relationship_count": len(payload.relationships),
            "shared_evidence_count": len(payload.shared_evidence),
            "source": source,
        },
        actor_context=actor_context,
    ) as ctx:
        builder = ctx.builder
        if builder:
            proposal, subjects = batch_direct_write_proposal(payload, source=source)
            builder.record_proposal(proposal, subjects=subjects)
        prepared = _prepare_batch_direct_write(
            instance,
            payload,
            source=source,
            source_ref=source_ref,
            actor_context=actor_context,
            builder=builder,
            group_store=ctx.uow.groups if ctx.uow is not None else None,
            resolution_contract_store=(
                ctx.uow.resolution_contracts if ctx.uow is not None else None
            ),
        )
        if ctx.uow is not None and prepared.contract_activations:
            record_contract_activations(
                ctx.uow.resolution_contracts,
                prepared.contract_activations,
                acceptance_receipt_id=builder.receipt_id if builder else None,
            )
        touched_entities = []
        for entity_item in prepared.entities:
            persisted_entity = prepared.graph.get_entity(
                entity_item.entity.entity_type,
                entity_item.entity.entity_id,
            )
            if persisted_entity is not None:
                touched_entities.append(persisted_entity)
            if builder:
                detail = prepared.entity_write_details.get(
                    (entity_item.entity.entity_type, entity_item.entity.entity_id),
                )
                builder.record_entity_write(
                    entity_item.entity.entity_type,
                    entity_item.entity.entity_id,
                    is_update=entity_item.is_update,
                    detail=detail,
                )

        config = instance.load_config()
        touched_relationships = []
        for relationship_item in prepared.relationships:
            edge = relationship_item.relationship
            apply_relationship(
                prepared.graph,
                relationship_item.validated,
                source,
                source_ref,
                config=config,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
                pending=relationship_item.pending,
                lifecycle=relationship_item.lifecycle,
            )
            _stamp_group_approval_drift(
                prepared.graph,
                edge,
                prepared.interactions,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
            )
            persisted_relationship = prepared.graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if persisted_relationship is not None:
                touched_relationships.append(persisted_relationship)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload() for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = edge.metadata.evidence.rationale
                evidence_detail.update(
                    _relationship_group_interaction_detail(edge, prepared.interactions)
                )
                if relationship_item.pending:
                    evidence_detail["review_status"] = "pending"
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=relationship_item.validated.is_update,
                    detail=evidence_detail,
                )

        if ctx.uow is not None:
            _annotate_direct_write_conflict_groups(
                graph=prepared.graph,
                group_store=ctx.uow.groups,
                interactions=prepared.interactions,
                receipt_id=builder.receipt_id if builder else None,
                source=source,
                source_ref=source_ref,
                actor_context=actor_context,
                builder=builder,
            )

        save_graph_for_mutation(
            instance,
            prepared.graph,
            entities=touched_entities,
            relationships=touched_relationships,
            uow=ctx.uow,
        )
        ctx.set_result(_batch_direct_write_result(prepared, dry_run=False))

    result = ctx.result
    assert isinstance(result, BatchDirectWriteResult)
    return result


def service_add_entity_inputs(
    instance: InstanceProtocol,
    entities: Sequence[EntityWriteInput],
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Normalize entity write inputs, then add or update graph entities."""
    return service_add_entities(
        instance,
        [_entity_from_input(entity) for entity in entities],
        dry_run=dry_run,
        actor_context=actor_context,
        _create_receipt=_create_receipt,
    )


def service_add_entities(
    instance: InstanceProtocol,
    entities: Sequence[EntityInstance],
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Add or update entities in the graph (batch upsert).

    Validates all entities first, then applies atomically.
    Raises DataValidationError on duplicates within the batch or schema violations.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity.entity_type for entity in entities],
    )
    config = instance.load_config()
    current_graph = instance.load_graph()

    with mutation_receipt(
        instance,
        "add_entity",
        {"count": len(entities)},
        enabled=_create_receipt and not dry_run,
        actor_context=actor_context,
    ) as ctx:
        builder = ctx.builder
        if builder:
            proposal, subjects = build_proposal(
                operation="add_entity",
                entities=[entity_instance_member(entity) for entity in entities],
            )
            builder.record_proposal(proposal, subjects=subjects)
        errors: list[str] = []
        batch_seen: set[tuple[str, str]] = set()
        pending = []

        for i, ent in enumerate(entities, start=1):
            key = (ent.entity_type, ent.entity_id)
            if key in batch_seen:
                errors.append(f"Entity {i}: duplicate in batch {ent.entity_type}:{ent.entity_id}")
                if builder:
                    builder.record_validation(
                        passed=False,
                        detail={"entity": i, "error": "duplicate in batch"},
                    )
                continue

            try:
                validated = validate_entity(
                    config,
                    current_graph,
                    ent.entity_type,
                    ent.entity_id,
                    ent.properties,
                    metadata=ent.metadata,
                )
            except DataValidationError as exc:
                errors.append(f"Entity {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"entity": i, "error": str(exc)})
                continue

            batch_seen.add(key)
            pending.append(validated)
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={"entity_type": ent.entity_type, "entity_id": ent.entity_id},
                )

        if errors:
            raise DataValidationError(
                f"Entity validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        graph = EntityGraph.from_dict(deepcopy(current_graph.to_dict()))
        for validated in pending:
            apply_entity(graph, validated, config=config, source="add_entity")
        try:
            guard_evaluation = evaluate_mutation_guards(
                config,
                current_graph=current_graph,
                proposed_graph=graph,
                entities=pending,
                actor_context=actor_context,
                write_delta=build_guard_write_delta(pending),
                creation_actor_resolver=receipt_creation_actor_resolver(instance),
                resolution_contract_store=(
                    ctx.uow.resolution_contracts if ctx.uow is not None else None
                ),
            )
        except DataValidationError as exc:
            guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
        if builder:
            record_guard_evaluation(builder, guard_evaluation)
        guard_errors = guard_evaluation.messages
        if guard_errors:
            raise DataValidationError(
                f"Mutation guard validation failed with {len(guard_errors)} error(s)",
                errors=guard_errors,
            )

        if dry_run:
            return AddEntityResult(
                added=sum(1 for validated in pending if not validated.is_update),
                updated=sum(1 for validated in pending if validated.is_update),
            )

        if ctx.uow is not None and guard_evaluation.contract_activations:
            record_contract_activations(
                ctx.uow.resolution_contracts,
                guard_evaluation.contract_activations,
                acceptance_receipt_id=builder.receipt_id if builder else None,
            )

        added = 0
        updated = 0
        touched_entities = []
        for validated in pending:
            persisted = graph.get_entity(
                validated.entity.entity_type,
                validated.entity.entity_id,
            )
            if persisted is not None:
                touched_entities.append(persisted)
            if builder:
                detail = _entity_property_change_detail(
                    current_graph,
                    validated,
                    actor_context=actor_context,
                )
                builder.record_entity_write(
                    validated.entity.entity_type,
                    validated.entity.entity_id,
                    is_update=validated.is_update,
                    detail=detail,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        save_graph_for_mutation(
            instance,
            graph,
            entities=touched_entities,
            relationships=[],
            uow=ctx.uow,
        )
        ctx.set_result(AddEntityResult(added=added, updated=updated))

    result = ctx.result
    assert isinstance(result, AddEntityResult)
    return result


def service_add_relationship_inputs(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipWriteInput],
    source: str,
    source_ref: str,
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
) -> AddRelationshipResult:
    """Normalize relationship write inputs, then add or update graph relationships."""
    inputs = list(relationships)
    normalized = [_relationship_from_input(instance, relationship) for relationship in inputs]
    return service_add_relationships(
        instance,
        [relationship for relationship, _pending in normalized],
        source=source,
        source_ref=source_ref,
        dry_run=dry_run,
        actor_context=actor_context,
        _create_receipt=_create_receipt,
        pending=[pending for _relationship, pending in normalized],
        lifecycle=[relationship.lifecycle for relationship in inputs],
    )


def service_add_relationships(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipInstance],
    source: str,
    source_ref: str,
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
    pending: bool | Sequence[bool] = False,
    lifecycle: RelationshipLifecycleState
    | None
    | Sequence[RelationshipLifecycleState | None] = None,
) -> AddRelationshipResult:
    """Add or update relationships in the graph (batch upsert).

    Validates all relationships first, then applies atomically.
    New edges get provenance stamped. Updated edges merge domain properties and
    preserve existing relationship metadata.
    Raises DataValidationError on duplicates within the batch or schema violations.

    ``lifecycle`` is the typed, review-SAFE lifecycle write channel (mirrors the
    batch direct-write path). When supplied per edge it is threaded to
    ``apply_relationship`` which sets ONLY ``assertion.lifecycle``; the review axis
    and group override are never touched from here.

    ``dry_run`` previews: refusals raised here (pending-edge, write-policy,
    validation) carry identical semantics to the applied path but are NOT
    receipted — a preview persists nothing, and receipts record what happened,
    not what was previewed.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[relationship.relationship_type for relationship in relationships],
    )
    config = instance.load_config()
    graph = instance.load_graph()

    with mutation_receipt(
        instance,
        "add_relationship",
        {"count": len(relationships), "source": source},
        enabled=_create_receipt and not dry_run,
        actor_context=actor_context,
    ) as ctx:
        builder = ctx.builder
        if builder:
            proposal, subjects = build_proposal(
                operation="add_relationship",
                relationships=[
                    relationship_instance_member(relationship) for relationship in relationships
                ],
                extra={
                    "source": source,
                    "source_ref": source_ref,
                    "pending": pending,
                    "lifecycle": lifecycle,
                },
            )
            builder.record_proposal(proposal, subjects=subjects)
        errors: list[str] = []
        batch_seen: set[tuple[str, str, str, str, str]] = set()
        prepared_relationships = []
        pending_flags = (
            list(pending)
            if isinstance(pending, Sequence) and not isinstance(pending, (str, bytes))
            else [bool(pending)] * len(relationships)
        )
        if len(pending_flags) != len(relationships):
            raise DataValidationError("pending flag count must match relationship count")
        lifecycle_states: list[RelationshipLifecycleState | None] = (
            list(lifecycle) if isinstance(lifecycle, Sequence) else [lifecycle] * len(relationships)
        )
        if len(lifecycle_states) != len(relationships):
            raise DataValidationError("lifecycle count must match relationship count")

        for i, edge in enumerate(relationships, start=1):
            pending_flag = pending_flags[i - 1]
            lifecycle_state = lifecycle_states[i - 1]
            key = edge.identity_tuple()
            if key in batch_seen:
                errors.append(
                    f"Edge {i}: duplicate in batch "
                    f"{edge.from_type}:{edge.from_id} "
                    f"-[{edge.relationship_type}]-> "
                    f"{edge.to_type}:{edge.to_id}"
                )
                if builder:
                    builder.record_validation(
                        passed=False, detail={"edge": i, "error": "duplicate in batch"}
                    )
                continue

            try:
                validated = validate_relationship(
                    config,
                    graph,
                    edge.from_type,
                    edge.from_id,
                    edge.relationship_type,
                    edge.to_type,
                    edge.to_id,
                    edge.properties,
                )
            except DataValidationError as exc:
                errors.append(f"Edge {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"edge": i, "error": str(exc)})
                continue

            validated.relationship.metadata = edge.metadata
            if pending_flag and validated.is_update:
                errors.append(f"Edge {i}: pending relationship writes can only create new edges")
                if builder:
                    builder.record_validation(
                        passed=False,
                        detail={"edge": i, "error": "pending update not allowed"},
                    )
                continue
            batch_seen.add(key)
            prepared_relationships.append((validated, edge, pending_flag, lifecycle_state))
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={
                        "from": f"{edge.from_type}:{edge.from_id}",
                        "to": f"{edge.to_type}:{edge.to_id}",
                        "relationship": edge.relationship_type,
                    },
                )

        if errors:
            raise DataValidationError(
                f"Relationship validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        guard_evaluation = evaluate_relationship_mutation_guards(
            instance,
            config,
            current_graph=graph,
            relationships=[
                validated for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
            ],
        )
        if builder:
            record_guard_evaluation(builder, guard_evaluation)
        guard_errors = guard_evaluation.messages
        if guard_errors:
            raise DataValidationError(
                f"Mutation guard validation failed with {len(guard_errors)} error(s)",
                errors=guard_errors,
            )

        interactions = _detect_direct_write_group_interactions(
            instance,
            graph,
            # The VALIDATED relationship, not the raw edge: content drift must be
            # measured against what actually lands (see ``_content_change``).
            [
                validated.relationship
                for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
            ],
            config=config,
            source=source,
            pending_flags=[
                pending_flag
                for _validated, _edge, pending_flag, _lifecycle in prepared_relationships
            ],
            group_store=ctx.uow.groups if ctx.uow is not None else None,
        )
        _record_group_interaction_validation(builder, interactions)

        # Run the refuse_direct_writes chokepoint here, in the prepare phase that
        # executes for BOTH dry-run and live, so a dry-run preview refuses a
        # proposal_only direct write identically to the live write (mirrors how
        # _prepare_batch_direct_write applies the edges through the chokepoint and
        # how the entity path refuses via apply_entity before its own dry-run
        # early return). Without this, the dry-run branch below would return
        # added=1 for a proposal_only direct add while the live write raises
        # DirectWriteRefusedError. A throwaway graph copy keeps this side-effect
        # free; the live block re-applies to the real graph below. pending=True
        # and governed sources are PERMITTED by the chokepoint, so this never
        # over-refuses.
        refusal_check_graph = EntityGraph.from_dict(deepcopy(graph.to_dict()))
        for validated, _edge, pending_flag, lifecycle_state in prepared_relationships:
            apply_relationship(
                refusal_check_graph,
                validated,
                source,
                source_ref,
                config=config,
                pending=pending_flag,
                lifecycle=lifecycle_state,
            )

        if dry_run:
            return AddRelationshipResult(
                added=sum(
                    1
                    for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
                    if not validated.is_update
                ),
                updated=sum(
                    1
                    for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
                    if validated.is_update
                ),
                pending_conflicts=list(interactions.pending_conflicts),
                updated_group_backed_edges=list(interactions.updated_group_backed_edges),
            )

        added = 0
        updated = 0
        touched_relationships = []
        for validated, edge, pending_flag, lifecycle_state in prepared_relationships:
            apply_relationship(
                graph,
                validated,
                source,
                source_ref,
                config=config,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
                pending=pending_flag,
                lifecycle=lifecycle_state,
            )
            _stamp_group_approval_drift(
                graph,
                edge,
                interactions,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
            )
            persisted = graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if persisted is not None:
                touched_relationships.append(persisted)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload() for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = edge.metadata.evidence.rationale
                evidence_detail.update(_relationship_group_interaction_detail(edge, interactions))
                if pending_flag:
                    evidence_detail["review_status"] = "pending"
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=validated.is_update,
                    detail=evidence_detail,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        if ctx.uow is not None:
            _annotate_direct_write_conflict_groups(
                graph=graph,
                group_store=ctx.uow.groups,
                interactions=interactions,
                receipt_id=builder.receipt_id if builder else None,
                source=source,
                source_ref=source_ref,
                actor_context=actor_context,
                builder=builder,
            )

        save_graph_for_mutation(
            instance,
            graph,
            entities=[],
            relationships=touched_relationships,
            uow=ctx.uow,
        )
        ctx.set_result(
            AddRelationshipResult(
                added=added,
                updated=updated,
                pending_conflicts=list(interactions.pending_conflicts),
                updated_group_backed_edges=list(interactions.updated_group_backed_edges),
            )
        )

    result = ctx.result
    assert isinstance(result, AddRelationshipResult)
    return result
