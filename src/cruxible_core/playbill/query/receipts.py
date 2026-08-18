"""The query-receipt journal: executions leave a receipt, reads leave nothing.

Only an actual evaluation of an accepted QueryDefinition is journalled here.
``discover``, ``expand``, and ``open_source`` are semantically side-effect-free
reads: they return their own receipt digest for replay, and they append nothing,
because an exploratory read that mutated observable state is exactly the
TauBench failure this seam exists to avoid.

The receipt records replay coordinates only -- definition, parameters,
coordinate, evaluation time, budgets, truncation, verdict, and the result digest
-- never the result rows. It changes no accepted state, no candidate, no
permission, no verdict input, and no evaluation episode's reward.
"""

from __future__ import annotations

from datetime import datetime

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust.records import (
    QUERY_RECEIPT_EVENT_KIND,
    QUERY_RECEIPT_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
)
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import AcceptedQueryDefinitionV1
from cruxible_core.playbill.query.engine import QueryExecutionReceiptV1

QUERY_RECEIPT_PARTITION_DIGEST_DOMAIN = "playbill-query-receipt-partition-v1"


def query_receipt_partition_id(definition: AcceptedQueryDefinitionV1) -> str:
    """Return the stable partition one QueryDefinition's receipts append to.

    Partitioning by definition keeps each canonical query's replay history in its
    own independent chain, so one query's receipts can be exported, verified, or
    pruned without touching another's.
    """

    digest = typed_digest(
        Sha256Value,
        QUERY_RECEIPT_PARTITION_DIGEST_DOMAIN,
        {"definition_path": definition.path},
    )
    return f"query.{digest.value}"


def query_receipt_payload(receipt: QueryExecutionReceiptV1) -> dict[str, object]:
    """Return the exact CAS-stored receipt body; result rows are never included."""

    return receipt.model_dump(mode="json")


def append_query_execution_receipt(
    writer: ProcedureExhaustWriter,
    *,
    receipt: QueryExecutionReceiptV1,
    definition: AcceptedQueryDefinitionV1,
    stream: JournalStreamIdentityV1,
    accepted_coordinate: AcceptedCoordinate,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> StoredProcedureJournalRecordV1:
    """Append one query execution receipt under the active writer fence."""

    if stream.journal_family != QUERY_RECEIPT_JOURNAL_FAMILY:
        raise PlaybillJournalError("query receipts append only to the query-receipt journal family")
    if receipt.definition_path != definition.path or (
        receipt.definition_digest != definition.artifact_digest
    ):
        raise PlaybillJournalError("query receipt names a different accepted QueryDefinition")
    if accepted_coordinate != AcceptedCoordinate.from_internal(receipt.coordinate):
        raise PlaybillJournalError(
            "query receipt coordinate differs from the journalled coordinate"
        )
    return writer.append(
        stream=stream,
        partition_id=query_receipt_partition_id(definition),
        event_kind=QUERY_RECEIPT_EVENT_KIND,
        accepted_coordinate=accepted_coordinate,
        definition_digest=receipt.definition_digest,
        actor_context=actor_context,
        recorded_at=recorded_at,
        payload=query_receipt_payload(receipt),
    )


__all__ = [
    "QUERY_RECEIPT_PARTITION_DIGEST_DOMAIN",
    "append_query_execution_receipt",
    "query_receipt_partition_id",
    "query_receipt_payload",
]
