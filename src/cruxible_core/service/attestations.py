"""Receipted attestation routing, dispositions, queues, and read summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn, cast

from cruxible_core.attestation.types import (
    AttestationDisposition,
    AttestationDispositionResult,
    AttestationListItem,
    AttestationQueueEntry,
    AttestationRecord,
    AttestationRecordResult,
    AttestationStance,
    AttestationVerdict,
    ClaimKey,
    ClaimStateAtRecord,
    CorroborationSummary,
    compute_claim_content_digest,
)
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.graph.evidence import (
    EvidenceRef,
    RelationshipEvidence,
    normalize_evidence_ref,
)
from cruxible_core.graph.operations import apply_relationship, validate_relationship
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import ListResult, list_truncated
from cruxible_core.temporal import ensure_utc, utc_now


def service_attest(
    instance: InstanceProtocol,
    *,
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    stance: AttestationStance,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]],
    observed_at: datetime,
    actor_context: GovernedActorContext | None,
    edge_key: int | None = None,
    properties: dict[str, Any] | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> AttestationRecordResult:
    """Record one observation, attaching or creating a pending claim per D2."""
    claim_key = _claim_key(relationship_type, from_type, from_id, to_type, to_id)
    normalized_evidence = [normalize_evidence_ref(ref) for ref in evidence_refs]
    observed = ensure_utc(observed_at)
    recorded = utc_now()
    with mutation_receipt(
        instance,
        "attestation",
        {
            "relationship_type": relationship_type,
            "from_type": from_type,
            "from_id": from_id,
            "to_type": to_type,
            "to_id": to_id,
            "edge_key": edge_key,
            "stance": stance,
            "observed_at": observed.isoformat(),
            "idempotency_key": idempotency_key,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        actor = _require_actor(actor_context, role="observer", builder=ctx.builder)
        if stance in {"support", "contradict"} and not normalized_evidence:
            _refuse(
                ctx.builder,
                f"attestation stance '{stance}' requires at least one evidence ref",
            )
        if observed > recorded:
            _refuse(ctx.builder, "attestation observed_at must be <= recorded_at")
        _refuse_oversized_procedure_evidence(
            ctx.uow.procedures,
            normalized_evidence,
            builder=ctx.builder,
        )

        if idempotency_key is not None:
            original = ctx.uow.attestations.find_idempotent_attestation(
                idempotency_key=idempotency_key,
                claim_key=claim_key,
                actor_org_id=actor.org_id,
                actor_id=actor.actor_id,
            )
            if original is not None:
                result = AttestationRecordResult(
                    attestation=original,
                    idempotent_replay=True,
                    receipt_id=original.receipt_id,
                )
                # Do not set a mutation result: replay returns the original record
                # and receipt without minting a second mutation receipt.
                return result

        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()
        relationship = _resolve_claim(graph, claim_key)
        created_claim = False
        warnings: list[str] = []

        if relationship is None:
            if stance != "support":
                _refuse(
                    ctx.builder,
                    f"cannot record stance '{stance}' for an absent claim; "
                    "only support may create a pending claim",
                )
            try:
                relationship, created_claim = _create_pending_claim(
                    graph,
                    config=config,
                    claim_key=claim_key,
                    properties=properties or {},
                    evidence_refs=normalized_evidence,
                    actor_context=actor,
                    receipt_id=ctx.builder.receipt_id,
                )
            except DataValidationError as exc:
                # The create path may discover that another writer won the tuple.
                # Retry tuple-first against the transaction's current graph image;
                # if the tuple is still absent, preserve the original refusal.
                retry_graph = ctx.uow.graph.load_graph()
                raced_relationship = _resolve_claim(retry_graph, claim_key)
                if raced_relationship is None:
                    _refuse(
                        ctx.builder,
                        f"cannot create pending claim for attestation: {exc}",
                    )
                graph = retry_graph
                relationship = raced_relationship
                warnings.append("pending claim appeared during create; attached to existing claim")
            if created_claim:
                assert relationship is not None
                ctx.uow.graph.upsert_relationships([relationship])
                instance.invalidate_graph_cache()
                ctx.builder.record_relationship_write(
                    relationship.from_type,
                    relationship.from_id,
                    relationship.to_type,
                    relationship.to_id,
                    relationship.relationship_type,
                    is_update=False,
                    detail={
                        "review_status": "pending",
                        "source": "attestation",
                    },
                )
        elif properties is not None:
            warnings.append("properties ignored because the claim tuple already exists")

        assert relationship is not None
        record = AttestationRecord(
            relationship_type=relationship.relationship_type,
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            edge_key=edge_key if edge_key is not None else relationship.edge_key,
            claim_content_digest=_relationship_digest(relationship),
            claim_state_at_record=_claim_state(relationship),
            stance=stance,
            evidence_refs=normalized_evidence,
            observed_at=observed,
            recorded_at=recorded,
            actor_context=actor,
            note=note,
            idempotency_key=idempotency_key,
            receipt_id=ctx.builder.receipt_id,
        )
        ctx.uow.attestations.save_attestation(record)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "attestation_id": record.attestation_id,
                "claim_state_at_record": record.claim_state_at_record,
                "claim_content_digest": record.claim_content_digest,
                "created_claim": created_claim,
                "warnings": warnings,
            },
        )
        result = AttestationRecordResult(
            attestation=record,
            created_claim=created_claim,
            warnings=warnings,
        )
        ctx.set_result(result)
    return result


def service_list_attestations(
    instance: InstanceProtocol,
    *,
    claim_key: ClaimKey | None = None,
    stance: AttestationStance | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List immutable records with tuple-first resolution markers."""
    _validate_page(limit=limit, offset=offset)
    graph = instance.load_graph()
    store = instance.get_attestation_store()
    try:
        records = store.list_attestations(
            claim_key=claim_key,
            stance=stance,
            limit=limit,
            offset=offset,
        )
        total = store.count_attestations(claim_key=claim_key, stance=stance)
        dispositions = store.get_latest_dispositions([record.attestation_id for record in records])
    finally:
        store.close()
    items = []
    for record in records:
        relationship = _resolve_claim(graph, record.claim_key())
        if relationship is None:
            items.append(
                AttestationListItem(
                    attestation=record,
                    latest_disposition=dispositions.get(record.attestation_id),
                    unresolved_target=True,
                )
            )
            continue
        items.append(
            AttestationListItem(
                attestation=record,
                latest_disposition=dispositions.get(record.attestation_id),
                edge_key_mismatch=(
                    record.edge_key is not None and record.edge_key != relationship.edge_key
                ),
                stale_content=(record.claim_content_digest != _relationship_digest(relationship)),
                current_claim_state=_claim_state(relationship),
            )
        )
    return ListResult(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(items)),
        read_revision=instance.get_read_revision(),
    )


def service_attestation_queue(
    instance: InstanceProtocol,
    *,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """Return per-claim open, current-content contradictions on live claims."""
    _validate_page(limit=limit, offset=offset)
    graph = instance.load_graph()
    store = instance.get_attestation_store()
    try:
        open_records = store.list_open_contradictions()
    finally:
        store.close()
    grouped: dict[ClaimKey, list[AttestationRecord]] = defaultdict(list)
    relationships: dict[ClaimKey, RelationshipInstance] = {}
    for record in open_records:
        relationship = _resolve_claim(graph, record.claim_key())
        if relationship is None or not relationship_is_live(relationship.metadata):
            continue
        if record.claim_content_digest != _relationship_digest(relationship):
            continue
        grouped[record.claim_key()].append(record)
        relationships[record.claim_key()] = relationship

    entries = []
    for key, records in grouped.items():
        relationship = relationships[key]
        entries.append(
            AttestationQueueEntry(
                relationship_type=relationship.relationship_type,
                from_type=relationship.from_type,
                from_id=relationship.from_id,
                to_type=relationship.to_type,
                to_id=relationship.to_id,
                edge_key=relationship.edge_key,
                properties=dict(relationship.properties),
                open_contradict_count=len(records),
                distinct_contradicting_actor_count=len(
                    {
                        (record.actor_context.org_id, record.actor_context.actor_id)
                        for record in records
                    }
                ),
                latest_observed_at=max(record.observed_at for record in records),
            )
        )
    entries.sort(
        key=lambda item: (
            -item.latest_observed_at.timestamp(),
            item.relationship_type,
            item.from_type,
            item.from_id,
            item.to_type,
            item.to_id,
        )
    )
    total = len(entries)
    page = entries[offset : offset + limit]
    return ListResult(
        items=page,
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(page)),
        read_revision=instance.get_read_revision(),
    )


def service_resolve_attestation(
    instance: InstanceProtocol,
    attestation_id: str,
    *,
    verdict: AttestationVerdict,
    actor_context: GovernedActorContext | None,
    note: str | None = None,
    follow_up_receipt_id: str | None = None,
) -> AttestationDispositionResult:
    """Append a reviewer disposition; latest disposition wins at read time."""
    with mutation_receipt(
        instance,
        "attestation_disposition",
        {
            "attestation_id": attestation_id,
            "verdict": verdict,
            "follow_up_receipt_id": follow_up_receipt_id,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        reviewer = _require_actor(actor_context, role="reviewer", builder=ctx.builder)
        if ctx.uow.attestations.get_attestation(attestation_id) is None:
            _refuse(ctx.builder, f"attestation '{attestation_id}' not found")
        disposition = AttestationDisposition(
            attestation_id=attestation_id,
            verdict=verdict,
            reviewer_actor_context=reviewer,
            note=note,
            follow_up_receipt_id=follow_up_receipt_id,
            receipt_id=ctx.builder.receipt_id,
        )
        ctx.uow.attestations.save_disposition(disposition)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "attestation_id": attestation_id,
                "disposition_id": disposition.disposition_id,
                "verdict": verdict,
            },
        )
        result = AttestationDispositionResult(disposition=disposition)
        ctx.set_result(result)
    return result


def service_corroboration_summaries(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipInstance],
) -> dict[ClaimKey, CorroborationSummary]:
    """Return derived summaries for many claims via one store query."""
    claim_digests = {
        _relationship_key(relationship): _relationship_digest(relationship)
        for relationship in relationships
    }
    if not claim_digests:
        return {}
    store = instance.get_attestation_store()
    try:
        return store.summaries_for_claims(claim_digests)
    finally:
        store.close()


def attach_corroboration_summaries(
    instance: InstanceProtocol,
    payloads: Sequence[dict[str, Any]],
) -> None:
    """Mutate serialized claim payloads with universally zero-elided summaries."""
    claims: dict[ClaimKey, list[dict[str, Any]]] = defaultdict(list)
    digests: dict[ClaimKey, str] = {}
    for payload in payloads:
        for claim in _walk_claim_payloads(payload):
            key = _payload_claim_key(claim)
            claims[key].append(claim)
            digests[key] = compute_claim_content_digest(
                key[0],
                key[1],
                key[2],
                key[3],
                key[4],
                dict(claim.get("properties") or {}),
            )
    if not claims:
        return
    store = instance.get_attestation_store()
    try:
        summaries = store.summaries_for_claims(digests)
    finally:
        store.close()
    for key, summary in summaries.items():
        summary_payload = summary.model_dump(mode="json", exclude_none=True)
        for claim in claims[key]:
            claim["corroboration"] = summary_payload


def _create_pending_claim(
    graph: Any,
    *,
    config: Any,
    claim_key: ClaimKey,
    properties: dict[str, Any],
    evidence_refs: list[EvidenceRef],
    actor_context: GovernedActorContext,
    receipt_id: str,
) -> tuple[RelationshipInstance, bool]:
    relationship_type, from_type, from_id, to_type, to_id = claim_key
    validated = validate_relationship(
        config,
        graph,
        from_type,
        from_id,
        relationship_type,
        to_type,
        to_id,
        properties,
    )
    if validated.is_update:
        existing = _resolve_claim(graph, claim_key)
        if existing is None:
            raise DataValidationError("claim tuple appeared but could not be resolved")
        return existing, False
    validated.relationship.metadata = RelationshipMetadata(
        evidence=RelationshipEvidence(evidence_refs=evidence_refs)
    )
    apply_relationship(
        graph,
        validated,
        "attestation",
        "support_observation",
        config=config,
        receipt_id=receipt_id,
        actor_context=actor_context,
        pending=True,
    )
    created = _resolve_claim(graph, claim_key)
    if created is None:
        raise DataValidationError("pending claim creation did not produce a resolvable tuple")
    return created, True


def _claim_state(relationship: RelationshipInstance) -> ClaimStateAtRecord:
    assertion = relationship_assertion_from_metadata(relationship.metadata)
    if relationship_is_live(assertion):
        return "live"
    if assertion.lifecycle.status != "active":
        return assertion.lifecycle.status
    if assertion.review.status == "pending":
        return "pending"
    if assertion.review.status == "rejected":
        return "rejected"
    if not relationship_lifecycle_is_active(assertion):
        return "inactive"
    return "live"


def _resolve_claim(graph: Any, claim_key: ClaimKey) -> RelationshipInstance | None:
    relationship_type, from_type, from_id, to_type, to_id = claim_key
    return cast(
        RelationshipInstance | None,
        graph.get_relationship(
            from_type,
            from_id,
            to_type,
            to_id,
            relationship_type,
        ),
    )


def _relationship_key(relationship: RelationshipInstance) -> ClaimKey:
    return _claim_key(
        relationship.relationship_type,
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
    )


def _claim_key(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
) -> ClaimKey:
    return relationship_type, from_type, from_id, to_type, to_id


def _relationship_digest(relationship: RelationshipInstance) -> str:
    return compute_claim_content_digest(
        relationship.relationship_type,
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        dict(relationship.properties),
    )


_CLAIM_FIELDS = frozenset(
    {"relationship_type", "from_type", "from_id", "to_type", "to_id", "properties"}
)


def _walk_claim_payloads(value: Any) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _CLAIM_FIELDS <= value.keys():
            claims.append(value)
        for nested in value.values():
            claims.extend(_walk_claim_payloads(nested))
    elif isinstance(value, list):
        for nested in value:
            claims.extend(_walk_claim_payloads(nested))
    return claims


def _payload_claim_key(payload: Mapping[str, Any]) -> ClaimKey:
    return _claim_key(
        str(payload["relationship_type"]),
        str(payload["from_type"]),
        str(payload["from_id"]),
        str(payload["to_type"]),
        str(payload["to_id"]),
    )


def _require_actor(
    actor_context: GovernedActorContext | None,
    *,
    role: str,
    builder: ReceiptBuilder,
) -> GovernedActorContext:
    if actor_context is None:
        _refuse(builder, f"attestation {role} actor context is required")
    return actor_context


def _refuse_oversized_procedure_evidence(
    procedure_store: Any,
    refs: Sequence[EvidenceRef],
    *,
    builder: ReceiptBuilder,
) -> None:
    for ref in refs:
        if ref.source != "procedure_run" or ref.artifact_id is None:
            continue
        artifact = procedure_store.get_evidence_artifact(ref.artifact_id)
        if artifact is not None and artifact.oversized:
            _refuse(
                builder,
                f"procedure evidence artifact '{ref.artifact_id}' exceeds the size cap "
                "and cannot be used for attestation",
            )


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ConfigError("Attestation list limit must be at least 1")
    if offset < 0:
        raise ConfigError("Attestation list offset must be at least 0")


def _refuse(builder: ReceiptBuilder, reason: str) -> NoReturn:
    builder.record_validation(passed=False, detail={"reason": reason})
    raise ConfigError(reason)


__all__ = [
    "attach_corroboration_summaries",
    "service_attest",
    "service_attestation_queue",
    "service_corroboration_summaries",
    "service_list_attestations",
    "service_resolve_attestation",
]
