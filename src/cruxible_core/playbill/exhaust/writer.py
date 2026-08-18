"""Shared durable appender for non-run Procedure exhaust records."""

from __future__ import annotations

from datetime import datetime

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust.backends import LocalJournalBackend
from cruxible_core.playbill.exhaust.records import (
    JournalEventKindV1,
    JournalStreamIdentityV1,
    ProcedureJournalRecordDraftV1,
    StoredProcedureJournalRecordV1,
    journal_payload_bytes,
)
from cruxible_core.playbill.projection import AcceptedCoordinate


class ProcedureExhaustWriter:
    """Append one CAS-backed record under the active local writer fence."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        fencing_token: str,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.fencing_token = fencing_token

    def append(
        self,
        *,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        event_kind: JournalEventKindV1,
        accepted_coordinate: AcceptedCoordinate,
        procedure_artifact_digest: str,
        definition_digest: str,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        payload: object,
        run_id: str | None = None,
        admission_binding_digest: str | None = None,
        line_spec_digest: str | None = None,
        occurrence_id: str | None = None,
        attempt: int | None = None,
    ) -> StoredProcedureJournalRecordV1:
        metadata = self.bodies.store(journal_payload_bytes(payload))
        head = self.journal.read_head(stream, partition_id)
        return self.journal.append(
            ProcedureJournalRecordDraftV1(
                stream=stream,
                partition_id=partition_id,
                event_kind=event_kind,
                accepted_coordinate=accepted_coordinate,
                procedure_artifact_digest=procedure_artifact_digest,
                definition_digest=definition_digest,
                run_id=run_id,
                line_spec_digest=line_spec_digest,
                occurrence_id=occurrence_id,
                attempt=attempt,
                admission_binding_digest=admission_binding_digest,
                payload_digest=metadata.digest,
                actor_context=actor_context,
                recorded_at=recorded_at,
            ),
            expected_head=head,
            fencing_token=self.fencing_token,
        )


__all__ = ["ProcedureExhaustWriter"]
