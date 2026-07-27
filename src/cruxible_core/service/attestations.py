"""Receipted attestation routing, dispositions, queues, and read summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, NoReturn, cast

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
    SupersessionPointer,
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.graph.claim_target import (
    ClaimTargetConflictError,
    resolve_claim_target,
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
from cruxible_core.temporal import ensure_utc, format_datetime, utc_now


def _replay_divergences(
    original: AttestationRecord,
    *,
    stance: AttestationStance,
    evidence_refs: Sequence[EvidenceRef],
    observed_at: datetime,
    note: str | None,
    edge_key: int | None,
    relationship: RelationshipInstance | None,
) -> list[str]:
    """Diff a replayed request against the record its idempotency key already minted.

    An idempotency key promises "this is the SAME observation". Anything the
    record persists is therefore in scope: a reused key carrying a different
    payload is a second, distinct observation wearing the first one's key, and
    returning the original as an idempotent replay would silently drop it.

    Shape back-ported from ``resolution_contracts._declaration_divergences``:
    identity fields, clock fields, and content fields are all compared, not just
    the two that used to be. ``note``, ``observed_at`` and ``edge_key`` used to
    diverge silently.

    NOT compared: ``properties``. It is a create-branch-only payload that is
    never persisted on ``AttestationRecord``, so there is no stored value to
    diff against — and a replay by definition finds the claim already present,
    where ``properties`` is ignored with a warning anyway. Adding it here would
    require persisting it purely to enable the comparison.
    """
    divergences: list[str] = []
    if original.stance != stance:
        divergences.append(f"stance (original '{original.stance}', request '{stance}')")

    original_evidence = [ref.model_dump(mode="json") for ref in original.evidence_refs]
    request_evidence = [ref.model_dump(mode="json") for ref in evidence_refs]
    if original_evidence != request_evidence:
        divergences.append("evidence_refs")

    if original.observed_at != ensure_utc(observed_at):
        divergences.append(
            f"observed_at (original '{format_datetime(original.observed_at)}', "
            f"request '{format_datetime(ensure_utc(observed_at))}')"
        )

    if original.note != note:
        divergences.append("note")

    resolved_claim_id = relationship.claim_id if relationship is not None else None
    if (
        original.claim_id is not None
        and resolved_claim_id is not None
        and original.claim_id != resolved_claim_id
    ):
        divergences.append(
            f"claim target (original '{original.claim_id}', request '{resolved_claim_id}')"
        )

    # edge_key is compared ONLY when the comparison can mean something.
    #
    # ``edge_key`` is a PER-LOAD counter (see ``graph/entity_graph``), not a
    # stable identity: re-materializing the graph -- which a pull does -- can
    # hand the same edge a different key. Manufacturing a missing replay key
    # from the CURRENT relationship and diffing it against the historical stamp
    # therefore reports a divergence for a pre-identity record (``claim_id`` is
    # NULL) whose key was simply repointed, refusing an honest, unchanged,
    # tuple-first replay.
    #
    # So it is compared only when the CALLER explicitly supplied one: that is a
    # deliberate reference, and naming a different edge than the original did is
    # a real divergence regardless of how keys were assigned.
    #
    # A replay that omits the disambiguator is left to the claim-target check
    # above. That is not a gap: for a record WITH a claim_id, "resolved to a
    # different edge" is exactly what the claim-target comparison already
    # catches, on the stable identity rather than on a per-load key; and for a
    # record WITHOUT one, the tuple is the identity and there is nothing else to
    # compare against.
    if edge_key is not None and original.edge_key != edge_key:
        divergences.append(f"edge_key (original '{original.edge_key}', request '{edge_key}')")

    return divergences


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
    claim_id: str | None = None,
    properties: dict[str, Any] | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> AttestationRecordResult:
    """Record one observation, attaching or creating a pending claim per D2.

    ``claim_id`` is the optional target disambiguator and it WINS over
    ``edge_key``; supplying both with disagreeing values is refused rather than
    silently resolved (see ``graph.claim_target``).
    """
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
            "claim_id": claim_id,
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

        # Target resolution runs BEFORE the idempotency check, because the
        # resolved target is part of what a replay has to match. A key that was
        # first used against one claim and is replayed against a DIFFERENT claim
        # on the same tuple is not a replay at all -- it is a second, distinct
        # observation wearing the first one's key -- and returning the original
        # record as "idempotent" would silently drop it.
        graph = ctx.uow.graph.load_graph()
        try:
            relationship = _resolve_target(
                graph,
                claim_key,
                claim_id=claim_id,
                edge_key=edge_key,
            )
        except ClaimTargetConflictError as exc:
            _refuse(ctx.builder, str(exc))

        if idempotency_key is not None:
            original = ctx.uow.attestations.find_idempotent_attestation(
                idempotency_key=idempotency_key,
                claim_key=claim_key,
                actor_org_id=actor.org_id,
                actor_id=actor.actor_id,
            )
            if original is not None:
                divergences = _replay_divergences(
                    original,
                    stance=stance,
                    evidence_refs=normalized_evidence,
                    observed_at=observed,
                    note=note,
                    edge_key=edge_key,
                    relationship=relationship,
                )
                if divergences:
                    _refuse(
                        ctx.builder,
                        "idempotency key replay diverges from the original attestation "
                        f"on {', '.join(divergences)}; a reused key must carry an "
                        "identical request",
                    )
                result = AttestationRecordResult(
                    attestation=original,
                    idempotent_replay=True,
                    receipt_id=original.receipt_id,
                )
                # Do not set a mutation result: replay returns the original record
                # and receipt without minting a second mutation receipt.
                return result

        config = instance.load_config()
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
                # Under the current SQLite backend, BEGIN IMMEDIATE serializes
                # writers, so a concurrent create-loser race cannot actually
                # reach this handler; the tuple-exists case returns via
                # is_update rather than raising. This catch therefore wraps
                # genuine validation failures (missing endpoints, required
                # properties) into a receipted refusal, and the tuple-first
                # re-read below is a best-effort attach for any backend whose
                # conflict surfaces as an in-transaction validation error.
                # Real conflict-at-commit handling (MVCC backends surface the
                # loser AFTER this scope) is deferred to backend-abstraction
                # work.
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
                    claim_id=relationship.claim_id,
                )
        # Placed OUTSIDE the create branch on purpose. As an ``elif`` it was
        # skipped whenever the create branch ended up ATTACHING to an existing
        # claim rather than creating one, which is precisely the case where the
        # caller's properties were silently discarded with no warning at all.
        if properties is not None and not created_claim:
            warnings.append("properties ignored because the claim tuple already exists")

        assert relationship is not None
        record = AttestationRecord(
            relationship_type=relationship.relationship_type,
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            edge_key=edge_key if edge_key is not None else relationship.edge_key,
            # Stamp from the RESOLVED claim, never from the request: the
            # request's disambiguator is a reference, the resolved edge is the
            # claim actually observed.
            claim_id=relationship.claim_id,
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
        mismatch, mismatch_kind = _target_identity_mismatch(record, relationship)
        items.append(
            AttestationListItem(
                attestation=record,
                latest_disposition=dispositions.get(record.attestation_id),
                target_identity_mismatch=mismatch,
                target_identity_mismatch_kind=mismatch_kind,
                stale_content=(record.claim_content_digest != _relationship_digest(relationship)),
                current_claim_state=_claim_state(relationship),
                successor_ref=_claim_successor_ref(relationship),
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
                claim_id=relationship.claim_id,
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
        if (
            follow_up_receipt_id is not None
            and ctx.uow.receipts.get_receipt(follow_up_receipt_id) is None
        ):
            _refuse(
                ctx.builder,
                f"follow_up_receipt_id '{follow_up_receipt_id}' does not resolve "
                "to a receipt in this instance",
            )
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
    """Mutate serialized claim payloads with universally zero-elided summaries.

    Payload detection is structural: any dict carrying the six claim-identity
    fields is treated as a claim row. A future payload shape that
    coincidentally carries those keys would get a ``corroboration`` key
    injected — callers introducing new envelope shapes should keep that in
    mind. When one tuple appears multiple times in a batch with divergent
    properties, the latest occurrence's digest wins for staleness bucketing.
    """
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
    created = apply_relationship(
        graph,
        validated,
        "attestation",
        "support_observation",
        config=config,
        receipt_id=receipt_id,
        actor_context=actor_context,
        pending=True,
    )
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


def _claim_successor_ref(relationship: RelationshipInstance) -> SupersessionPointer | None:
    assertion = relationship_assertion_from_metadata(relationship.metadata)
    if assertion.lifecycle.status != "superseded":
        return None
    return assertion.lifecycle.superseded_by


def _target_identity_mismatch(
    record: AttestationRecord,
    relationship: RelationshipInstance,
) -> tuple[bool, Literal["claim_id", "edge_key"] | None]:
    """Compare a record's stamped target identity with the live claim.

    EVER-OR-NEVER per record: a record that stamped a ``claim_id`` is compared
    by id and never falls back to ``edge_key`` (which is per-load and would
    report a spurious mismatch after any reload); a legacy record with only an
    ``edge_key`` is compared by key, exactly as before.
    """
    if record.claim_id is not None:
        return record.claim_id != relationship.claim_id, "claim_id"
    if record.edge_key is not None:
        return record.edge_key != relationship.edge_key, "edge_key"
    return False, None


def _resolve_target(
    graph: Any,
    claim_key: ClaimKey,
    *,
    claim_id: str | None,
    edge_key: int | None,
) -> RelationshipInstance | None:
    """Resolve the observed claim honouring the disambiguator precedence."""
    relationship_type, from_type, from_id, to_type, to_id = claim_key
    return resolve_claim_target(
        graph,
        relationship_type=relationship_type,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        claim_id=claim_id,
        edge_key=edge_key,
    ).relationship


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
    # Deliberate asymmetry, on the record: an oversized procedure-evidence
    # artifact refuses (attesting to a payload nobody can dereference is
    # misleading), while a hand-constructed procedure_run ref whose artifact
    # does not exist passes silently — consistent with the general
    # no-existence-validation precedent for evidence refs.
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
