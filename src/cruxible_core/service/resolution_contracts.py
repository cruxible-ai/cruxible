"""Receipted resolution-contract opening, resolution, dispositions, and queues.

Outcome forcing is the second half of the observation loop: attestation records
observations against claims, resolution contracts DEMAND them for decisions.
Every write here is append-only and never mutates its subject; consequence
remains a separate reviewer act through existing verbs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn, cast

from cruxible_core.attestation.types import compute_claim_content_digest
from cruxible_core.config.schema import CoreConfig, NamedQuerySchema
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import EvidenceRef, normalize_evidence_ref
from cruxible_core.graph.types import EntityInstance
from cruxible_core.instance_protocol import (
    InstanceProtocol,
    ResolutionContractStoreProtocol,
)
from cruxible_core.query.entity_state import entity_matches_query_state
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt
from cruxible_core.resolution_contracts.types import (
    AttestationMeasurement,
    ContractDeclaration,
    ContractDispositionResult,
    ContractListItem,
    ContractOpenResult,
    ContractQueue,
    ContractQueueEntry,
    ContractResolution,
    ContractResolveResult,
    ContractStatus,
    MeasurementExpectation,
    QueryMeasurement,
    ResolutionContract,
    ResolutionDisposition,
    ResolutionDispositionVerdict,
    ResolutionVerdict,
    compute_entity_content_digest,
    compute_query_definition_digest,
)
from cruxible_core.service.gates import entity_matches_property_equality_condition
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import ListResult, list_truncated
from cruxible_core.temporal import ensure_utc, format_datetime, utc_now

# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------


def service_open_resolution_contract(
    instance: InstanceProtocol,
    *,
    entity_type: str,
    entity_id: str,
    description: str,
    check_at: datetime,
    expires_at: datetime,
    measurement: Mapping[str, Any],
    actor_context: GovernedActorContext | None,
    idempotency_key: str | None = None,
) -> ContractOpenResult:
    """Open one resolution contract against an existing governed subject."""
    with mutation_receipt(
        instance,
        "resolution_contract_open",
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "check_at": format_datetime(check_at),
            "expires_at": format_datetime(expires_at),
            "measurement_kind": measurement.get("kind"),
            "idempotency_key": idempotency_key,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        actor = _require_actor(actor_context, role="opening", builder=ctx.builder)
        config = instance.load_config()
        graph = ctx.uow.graph.load_graph()

        subject = graph.get_entity(entity_type, entity_id)
        if subject is None:
            # Endpoint-existence precedent: a commitment against a subject that
            # does not exist can never be measured, and for guard-adopting types
            # this is exactly what makes create-with-accepted-value impossible.
            _refuse(
                ctx.builder,
                f"resolution contract subject '{entity_type}:{entity_id}' does not exist; "
                "propose the record first, then open a contract against it",
            )

        declaration = _build_declaration(
            config,
            description=description,
            check_at=check_at,
            expires_at=expires_at,
            measurement=measurement,
            builder=ctx.builder,
        )

        if idempotency_key is not None:
            original = ctx.uow.resolution_contracts.find_idempotent_contract(
                idempotency_key=idempotency_key,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_org_id=actor.org_id,
                actor_id=actor.actor_id,
            )
            if original is not None:
                divergences = _declaration_divergences(original.declaration, declaration)
                if divergences:
                    _refuse(
                        ctx.builder,
                        "idempotency key replay diverges from the original contract "
                        f"on {', '.join(divergences)}; a reused key must carry an "
                        "identical request",
                    )
                replay = ContractOpenResult(
                    contract=original,
                    idempotent_replay=True,
                    receipt_id=original.receipt_id,
                )
                # Replay returns the original contract and receipt without
                # minting a second mutation receipt.
                return replay

        assert subject is not None
        contract = ResolutionContract(
            entity_type=entity_type,
            entity_id=entity_id,
            subject_content_digest=_entity_digest(subject),
            declaration=declaration,
            actor_context=actor,
            idempotency_key=idempotency_key,
            receipt_id=ctx.builder.receipt_id,
        )
        ctx.uow.resolution_contracts.save_contract(contract)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "contract_id": contract.contract_id,
                "subject_content_digest": contract.subject_content_digest,
                "measurement_kind": declaration.measurement.kind,
            },
            entity_type=entity_type,
            entity_id=entity_id,
        )
        result = ContractOpenResult(contract=contract)
        ctx.set_result(result)
    return result


def _build_declaration(
    config: CoreConfig,
    *,
    description: str,
    check_at: datetime,
    expires_at: datetime,
    measurement: Mapping[str, Any],
    builder: ReceiptBuilder,
) -> ContractDeclaration:
    """Validate the declaration at open, pinning the query definition digest."""
    try:
        declaration = ContractDeclaration.model_validate(
            {
                "description": description,
                "check_at": check_at,
                "expires_at": expires_at,
                "measurement": dict(measurement),
            }
        )
    except ValueError as exc:
        _refuse(builder, f"invalid resolution contract declaration: {exc}")

    if isinstance(declaration.measurement, QueryMeasurement):
        schema = _validated_query_schema(config, declaration.measurement, builder=builder)
        declaration = declaration.model_copy(
            update={
                "measurement": declaration.measurement.model_copy(
                    update={"query_definition_digest": compute_query_definition_digest(schema)}
                )
            }
        )
    return declaration


def _validated_query_schema(
    config: CoreConfig,
    measurement: QueryMeasurement,
    *,
    builder: ReceiptBuilder,
) -> NamedQuerySchema:
    """Resolve and shape-check a query measurement against the live config.

    With auto-resolution deferred, nothing downstream would ever catch a rotten
    query reference, so it is caught here, at open.
    """
    schema = config.named_queries.get(measurement.query_name)
    if schema is None:
        _refuse(
            builder,
            f"query measurement query_name '{measurement.query_name}' is not "
            "defined in named_queries",
        )
    if schema.entry_point is not None:
        entity_schema = config.get_entity_type(schema.entry_point)
        primary_key = entity_schema.get_primary_key() if entity_schema is not None else None
        if primary_key is None:
            _refuse(
                builder,
                f"query measurement query '{measurement.query_name}' has entry point "
                f"'{schema.entry_point}', which declares no primary key",
            )
        if primary_key not in measurement.params:
            _refuse(
                builder,
                f"query measurement params must supply '{primary_key}' for entry "
                f"point '{schema.entry_point}'; got {sorted(measurement.params)}",
            )
    return schema


def _declaration_divergences(
    original: ContractDeclaration,
    replay: ContractDeclaration,
) -> list[str]:
    divergences: list[str] = []
    if original.description != replay.description:
        divergences.append("description")
    if original.check_at != replay.check_at or original.expires_at != replay.expires_at:
        divergences.append("clock")
    original_measurement = original.measurement.model_dump(mode="json", exclude_none=True)
    replay_measurement = replay.measurement.model_dump(mode="json", exclude_none=True)
    if original_measurement != replay_measurement:
        divergences.append("measurement")
    return divergences


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def service_resolve_outcome(
    instance: InstanceProtocol,
    contract_id: str,
    *,
    verdict: ResolutionVerdict,
    observed_at: datetime,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = (),
    actor_context: GovernedActorContext | None,
    note: str | None = None,
    resolving_query_receipt_id: str | None = None,
    resolving_attestation_ids: Sequence[str] = (),
) -> ContractResolveResult:
    """Record the one standing answer to one activated contract."""
    normalized_evidence = [normalize_evidence_ref(ref) for ref in evidence_refs]
    attestation_ids = list(resolving_attestation_ids)
    observed = ensure_utc(observed_at)
    recorded = utc_now()
    with mutation_receipt(
        instance,
        "resolution_contract_resolve",
        {
            "contract_id": contract_id,
            "verdict": verdict,
            "observed_at": observed.isoformat(),
            "resolving_query_receipt_id": resolving_query_receipt_id,
            "resolving_attestation_ids": attestation_ids,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        actor = _require_actor(actor_context, role="resolving", builder=ctx.builder)
        store = ctx.uow.resolution_contracts

        contract = store.get_contract(contract_id)
        if contract is None:
            _refuse(ctx.builder, f"resolution contract '{contract_id}' not found")
        if not store.get_activations([contract_id]):
            _refuse(
                ctx.builder,
                f"resolution contract '{contract_id}' was never activated by an "
                "acceptance; a prepared contract has nothing to answer for and "
                "simply expires",
            )

        sequence = _next_resolution_sequence(store, contract_id, builder=ctx.builder)

        if verdict in {"satisfied", "contradicted"} and not normalized_evidence:
            _refuse(ctx.builder, f"verdict '{verdict}' requires at least one evidence ref")
        if verdict == "contradicted" and not (note or "").strip():
            _refuse(ctx.builder, "verdict 'contradicted' requires a note")
        if observed > recorded:
            _refuse(ctx.builder, "observed_at must be <= recorded_at")
        if verdict == "satisfied" and observed < contract.declaration.check_at:
            # The clock rule: a success observed before the contract said to
            # look has not measured what was promised. Failure and uncertainty
            # are honest at any time.
            _refuse(
                ctx.builder,
                "verdict 'satisfied' requires observed_at at or after the "
                f"declared check_at ({format_datetime(contract.declaration.check_at)}); "
                "an observation taken earlier has not measured the declared outcome",
            )

        _validate_measurement_evidence(
            instance,
            ctx.uow,
            contract=contract,
            verdict=verdict,
            resolving_query_receipt_id=resolving_query_receipt_id,
            resolving_attestation_ids=attestation_ids,
            builder=ctx.builder,
        )

        resolution = ContractResolution(
            contract_id=contract_id,
            sequence=sequence,
            verdict=verdict,
            evidence_refs=normalized_evidence,
            observed_at=observed,
            recorded_at=recorded,
            actor_context=actor,
            note=note,
            resolving_query_receipt_id=resolving_query_receipt_id,
            resolving_attestation_ids=attestation_ids,
            receipt_id=ctx.builder.receipt_id,
        )
        store.save_resolution(resolution)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "contract_id": contract_id,
                "resolution_id": resolution.resolution_id,
                "sequence": sequence,
                "verdict": verdict,
            },
            entity_type=contract.entity_type,
            entity_id=contract.entity_id,
        )
        result = ContractResolveResult(resolution=resolution)
        ctx.set_result(result)
    return result


def _next_resolution_sequence(
    store: ResolutionContractStoreProtocol,
    contract_id: str,
    *,
    builder: ReceiptBuilder,
) -> int:
    """Return the next sequence, refusing unless the contract is open.

    One-shot by construction: the first resolution closes the contract. Only a
    reviewer ``overturned`` disposition re-opens it for exactly one more.
    """
    history = store.list_resolutions(contract_id)
    if not history:
        return 1
    latest = history[-1]
    disposition = store.get_dispositions([latest.resolution_id]).get(latest.resolution_id)
    if disposition is None or disposition.verdict != "overturned":
        builder.record_validation(
            passed=False,
            detail={
                "reason": "contract already resolved",
                "contract_id": contract_id,
                "standing_resolution_id": latest.resolution_id,
                "standing_verdict": latest.verdict,
            },
        )
        raise ConfigError(
            f"resolution contract '{contract_id}' already carries a standing "
            f"'{latest.verdict}' resolution ({latest.resolution_id}); a reviewer "
            "must overturn it before another resolution can be recorded"
        )
    return latest.sequence + 1


def _validate_measurement_evidence(
    instance: InstanceProtocol,
    uow: Any,
    *,
    contract: ResolutionContract,
    verdict: ResolutionVerdict,
    resolving_query_receipt_id: str | None,
    resolving_attestation_ids: Sequence[str],
    builder: ReceiptBuilder,
) -> None:
    """Refuse resolutions whose measurement evidence does not back the verdict."""
    measurement = contract.declaration.measurement
    if isinstance(measurement, QueryMeasurement):
        _validate_query_measurement(
            instance,
            uow,
            measurement=measurement,
            verdict=verdict,
            resolving_query_receipt_id=resolving_query_receipt_id,
            builder=builder,
        )
        if resolving_attestation_ids:
            _refuse(
                builder,
                "resolving_attestation_ids do not apply to a query measurement",
            )
        return

    if resolving_query_receipt_id is not None:
        _refuse(
            builder,
            "resolving_query_receipt_id does not apply to an attestation measurement",
        )
    _validate_attestation_measurement(
        uow,
        measurement=measurement,
        verdict=verdict,
        resolving_attestation_ids=resolving_attestation_ids,
        builder=builder,
    )


def _validate_query_measurement(
    instance: InstanceProtocol,
    uow: Any,
    *,
    measurement: QueryMeasurement,
    verdict: ResolutionVerdict,
    resolving_query_receipt_id: str | None,
    builder: ReceiptBuilder,
) -> None:
    config = instance.load_config()
    schema = config.named_queries.get(measurement.query_name)
    current_digest = None if schema is None else compute_query_definition_digest(schema)
    drifted = (
        measurement.query_definition_digest is not None
        and current_digest != measurement.query_definition_digest
    )
    if drifted:
        # The measurement no longer means what was declared, so no verdict about
        # the declared outcome can be justified by running it.
        if verdict != "indeterminate":
            _refuse(
                builder,
                f"named query '{measurement.query_name}' has changed since this "
                "contract was opened (definition digest drift); only an "
                "'indeterminate' resolution is available",
            )
        return

    if verdict == "indeterminate":
        return
    if resolving_query_receipt_id is None:
        _refuse(
            builder,
            f"verdict '{verdict}' on a query measurement requires "
            "resolving_query_receipt_id: the receipt of the query run that "
            "observed the outcome",
        )
    receipt = uow.receipts.get_receipt(resolving_query_receipt_id)
    if receipt is None:
        _refuse(
            builder,
            f"resolving_query_receipt_id '{resolving_query_receipt_id}' does not "
            "resolve to a receipt in this instance",
        )
    if receipt.operation_type != "query":
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' is a "
            f"'{receipt.operation_type}' receipt, not a query receipt",
        )
    if receipt.query_name != measurement.query_name:
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' ran query "
            f"'{receipt.query_name}', not the declared '{measurement.query_name}'",
        )
    if receipt.parameters != measurement.params:
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' ran the declared query with "
            "different parameters than the contract declared",
        )
    result_detail = _result_node_detail(receipt)
    if result_detail.get("truncated"):
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' is truncated "
            f"({result_detail.get('truncation_reasons')}); a partial result cannot "
            "settle a count expectation",
        )
    satisfied = _expectation_holds(
        config,
        measurement.expect,
        rows=list(receipt.results),
        total=_receipt_total_results(receipt, result_detail),
    )
    if verdict == "satisfied" and not satisfied:
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' does not satisfy the declared "
            "expectation; the stored result contradicts a 'satisfied' verdict",
        )
    if verdict == "contradicted" and satisfied:
        _refuse(
            builder,
            f"receipt '{resolving_query_receipt_id}' satisfies the declared "
            "expectation; it cannot evidence a 'contradicted' verdict",
        )


def _result_node_detail(receipt: Receipt) -> dict[str, Any]:
    for node in receipt.nodes:
        if node.node_type == "result":
            return dict(node.detail)
    return {}


def _receipt_total_results(receipt: Receipt, result_detail: Mapping[str, Any]) -> int:
    total = result_detail.get("total_results")
    if isinstance(total, int):
        return total
    return len(receipt.results)


def _expectation_holds(
    config: CoreConfig,
    expect: MeasurementExpectation,
    *,
    rows: list[dict[str, Any]],
    total: int,
) -> bool:
    """Evaluate the count grammar plus the optional property condition."""
    if expect.min_count is not None and total < expect.min_count:
        return False
    if expect.max_count is not None and total > expect.max_count:
        return False
    if expect.condition is None:
        return True
    matches = [_row_matches_condition(config, row, expect.condition) for row in rows]
    if expect.condition_scope == "any":
        return any(matches)
    # ALL over an empty result set is vacuously true only when the count grammar
    # allowed emptiness; the count check above already ran, so honor it.
    return all(matches)


def _row_matches_condition(
    config: CoreConfig,
    row: Mapping[str, Any],
    condition: Mapping[str, str | int | float | bool],
) -> bool:
    """Apply the shared gate property-equality helper to one entity-shaped row.

    A projected row keeps its unprojected ``source``, so a query with a
    ``select`` still resolves to the entity the condition is about. A row that
    is neither entity-shaped nor entity-sourced (a path or relationship row)
    does not match: the condition surface is entity properties, and silently
    passing an unevaluable row would let an expectation hold vacuously.
    """
    if "values" in row and isinstance(row.get("source"), Mapping):
        row = cast(Mapping[str, Any], row["source"])
    entity_type = row.get("entity_type")
    entity_id = row.get("entity_id")
    properties = row.get("properties")
    if not isinstance(entity_type, str) or not isinstance(entity_id, str):
        return False
    entity = EntityInstance(
        entity_type=entity_type,
        entity_id=entity_id,
        properties=dict(properties) if isinstance(properties, Mapping) else {},
    )
    return entity_matches_property_equality_condition(config, entity, dict(condition))


def _validate_attestation_measurement(
    uow: Any,
    *,
    measurement: AttestationMeasurement,
    verdict: ResolutionVerdict,
    resolving_attestation_ids: Sequence[str],
    builder: ReceiptBuilder,
) -> None:
    if verdict == "indeterminate":
        return
    if not resolving_attestation_ids:
        _refuse(
            builder,
            f"verdict '{verdict}' on an attestation measurement requires at least "
            "one resolving attestation id",
        )
    expected_stance = "support" if verdict == "satisfied" else "contradict"
    graph = uow.graph.load_graph()
    claim = graph.get_relationship(
        measurement.from_type,
        measurement.from_id,
        measurement.to_type,
        measurement.to_id,
        measurement.relationship_type,
    )
    current_digest = (
        None
        if claim is None
        else compute_claim_content_digest(
            claim.relationship_type,
            claim.from_type,
            claim.from_id,
            claim.to_type,
            claim.to_id,
            dict(claim.properties),
        )
    )
    for attestation_id in resolving_attestation_ids:
        record = uow.attestations.get_attestation(attestation_id)
        if record is None:
            _refuse(builder, f"attestation '{attestation_id}' not found")
        if record.claim_key() != measurement.claim_key():
            _refuse(
                builder,
                f"attestation '{attestation_id}' targets a different claim than "
                "the contract declared",
            )
        if record.stance != expected_stance:
            _refuse(
                builder,
                f"attestation '{attestation_id}' has stance '{record.stance}'; "
                f"verdict '{verdict}' needs '{expected_stance}'",
            )
        if current_digest is None or record.claim_content_digest != current_digest:
            _refuse(
                builder,
                f"attestation '{attestation_id}' was recorded against different "
                "claim content than the claim now carries; it cannot settle this "
                "contract",
            )


# ---------------------------------------------------------------------------
# Reviewer dispositions
# ---------------------------------------------------------------------------


def service_dispose_resolution(
    instance: InstanceProtocol,
    resolution_id: str,
    *,
    verdict: ResolutionDispositionVerdict,
    actor_context: GovernedActorContext | None,
    note: str | None = None,
) -> ContractDispositionResult:
    """Uphold or overturn one resolution; an overturn re-opens its contract."""
    with mutation_receipt(
        instance,
        "resolution_contract_disposition",
        {"resolution_id": resolution_id, "verdict": verdict},
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        reviewer = _require_actor(actor_context, role="reviewer", builder=ctx.builder)
        store = ctx.uow.resolution_contracts
        resolution = store.get_resolution(resolution_id)
        if resolution is None:
            _refuse(ctx.builder, f"resolution '{resolution_id}' not found")
        if store.get_dispositions([resolution_id]):
            # One disposition per resolution: after an overturn the next answer
            # is a NEW resolution, so a second disposition on the same record
            # would have nothing left to decide.
            _refuse(
                ctx.builder,
                f"resolution '{resolution_id}' already carries a reviewer "
                "disposition; record the next answer as a new resolution",
            )
        disposition = ResolutionDisposition(
            resolution_id=resolution_id,
            verdict=verdict,
            reviewer_actor_context=reviewer,
            note=note,
            receipt_id=ctx.builder.receipt_id,
        )
        store.save_disposition(disposition)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "resolution_id": resolution_id,
                "contract_id": resolution.contract_id,
                "disposition_id": disposition.disposition_id,
                "verdict": verdict,
                "reopened": verdict == "overturned",
            },
        )
        result = ContractDispositionResult(disposition=disposition)
        ctx.set_result(result)
    return result


# ---------------------------------------------------------------------------
# Derived reads
# ---------------------------------------------------------------------------


def service_list_resolution_contracts(
    instance: InstanceProtocol,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: ContractStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List contracts with derived lifecycle markers.

    ``status`` filters the returned page only; ``total`` stays the unfiltered
    store count for the subject, so paging remains honest rather than pretending
    the store holds only what this page kept.
    """
    _validate_page(limit=limit, offset=offset)
    graph = instance.load_graph()
    store = instance.get_resolution_contract_store()
    try:
        contracts = store.list_contracts(
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
        )
        total = store.count_contracts(entity_type=entity_type, entity_id=entity_id)
        items = _build_list_items(store, graph, contracts)
    finally:
        store.close()
    if status is not None:
        items = [item for item in items if item.status == status]
    return ListResult(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(contracts)),
        read_revision=instance.get_read_revision(),
    )


def service_outcome_queue(
    instance: InstanceProtocol,
    *,
    queue: ContractQueue = "due",
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """Return one derived attention queue over activated contracts.

    - ``due`` — activated, unanswered, past ``check_at``; the triage work list.
      Past-expiry contracts stay in it (the check is still owed) and carry
      ``overdue``.
    - ``overdue`` — the escalation subset past ``expires_at``.
    - ``contradicted`` — standing contradicted resolutions with no reviewer
      disposition.

    All three filter to LIVE subjects: a dead or superseded subject must not
    nag forever. Prepared-but-never-activated contracts appear in none of them;
    they simply expire, having never demanded attention.
    """
    _validate_page(limit=limit, offset=offset)
    graph = instance.load_graph()
    now = format_datetime(utc_now()) or ""
    store = instance.get_resolution_contract_store()
    try:
        if queue == "contradicted":
            entries = _contradicted_entries(store, graph)
        else:
            entries = _clock_entries(store, graph, now=now, queue=queue)
    finally:
        store.close()
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


def _clock_entries(
    store: ResolutionContractStoreProtocol,
    graph: EntityGraph,
    *,
    now: str,
    queue: ContractQueue,
) -> list[ContractQueueEntry]:
    contracts = store.list_activated_unresolved(before=now, use_expiry=queue == "overdue")
    entries: list[ContractQueueEntry] = []
    for contract in contracts:
        if not _subject_is_live(graph, contract):
            continue
        entries.append(
            ContractQueueEntry(
                contract_id=contract.contract_id,
                entity_type=contract.entity_type,
                entity_id=contract.entity_id,
                description=contract.declaration.description,
                check_at=contract.declaration.check_at,
                expires_at=contract.declaration.expires_at,
                overdue=(format_datetime(contract.declaration.expires_at) or "") <= now,
                measurement_kind=contract.declaration.measurement.kind,
            )
        )
    entries.sort(key=lambda entry: (entry.check_at, entry.contract_id))
    return entries


def _contradicted_entries(
    store: ResolutionContractStoreProtocol,
    graph: EntityGraph,
) -> list[ContractQueueEntry]:
    entries: list[ContractQueueEntry] = []
    for contract, resolution in store.list_undisposed_contradictions():
        if not _subject_is_live(graph, contract):
            continue
        entries.append(
            ContractQueueEntry(
                contract_id=contract.contract_id,
                entity_type=contract.entity_type,
                entity_id=contract.entity_id,
                description=contract.declaration.description,
                check_at=contract.declaration.check_at,
                expires_at=contract.declaration.expires_at,
                measurement_kind=contract.declaration.measurement.kind,
                latest_resolution=resolution,
            )
        )
    entries.sort(
        key=lambda entry: (
            -(entry.latest_resolution.recorded_at.timestamp() if entry.latest_resolution else 0.0),
            entry.contract_id,
        )
    )
    return entries


def _build_list_items(
    store: ResolutionContractStoreProtocol,
    graph: EntityGraph,
    contracts: Sequence[ResolutionContract],
) -> list[ContractListItem]:
    contract_ids = [contract.contract_id for contract in contracts]
    activations = store.get_activations(contract_ids)
    resolutions = store.get_latest_resolutions(contract_ids)
    dispositions = store.get_dispositions(
        [resolution.resolution_id for resolution in resolutions.values()]
    )
    now = utc_now()
    items: list[ContractListItem] = []
    for contract in contracts:
        activation = activations.get(contract.contract_id)
        resolution = resolutions.get(contract.contract_id)
        disposition = dispositions.get(resolution.resolution_id) if resolution is not None else None
        standing = resolution is not None and (
            disposition is None or disposition.verdict != "overturned"
        )
        status: ContractStatus
        if activation is None:
            status = "prepared"
        elif standing:
            status = "resolved"
        else:
            status = "open"
        subject = graph.get_entity(contract.entity_type, contract.entity_id)
        items.append(
            ContractListItem(
                contract=contract,
                status=status,
                activation=activation,
                latest_resolution=resolution,
                latest_disposition=disposition,
                expired=contract.declaration.expires_at <= now,
                subject_present=subject is not None,
                subject_content_drifted=(
                    subject is not None
                    and _entity_digest(subject) != contract.subject_content_digest
                ),
            )
        )
    return items


def _subject_is_live(graph: EntityGraph, contract: ResolutionContract) -> bool:
    subject = graph.get_entity(contract.entity_type, contract.entity_id)
    if subject is None:
        return False
    return bool(entity_matches_query_state(subject.metadata, "live"))


def _entity_digest(entity: EntityInstance) -> str:
    return compute_entity_content_digest(
        entity.entity_type,
        entity.entity_id,
        dict(entity.properties),
    )


def _require_actor(
    actor_context: GovernedActorContext | None,
    *,
    role: str,
    builder: ReceiptBuilder,
) -> GovernedActorContext:
    if actor_context is None:
        _refuse(builder, f"resolution contract {role} actor context is required")
    return actor_context


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ConfigError("Resolution contract list limit must be at least 1")
    if offset < 0:
        raise ConfigError("Resolution contract list offset must be at least 0")


def _refuse(builder: ReceiptBuilder, reason: str) -> NoReturn:
    builder.record_validation(passed=False, detail={"reason": reason})
    raise ConfigError(reason)


def parse_measurement(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a caller-supplied measurement payload for the service."""
    return cast(dict[str, Any], dict(payload))


__all__ = [
    "parse_measurement",
    "service_dispose_resolution",
    "service_list_resolution_contracts",
    "service_open_resolution_contract",
    "service_outcome_queue",
    "service_resolve_outcome",
]
