"""Shared durable appender for fenced operational journal records.

The record shape is per-kind: Procedure exhaust events name their exact
Procedure artifact, while a query execution receipt names only its accepted
QueryDefinition. Both take the same fence, the same hash chain, and the same
CAS-addressed payload, so there is exactly one durable append path.
"""

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
from cruxible_core.playbill.material_reservations import (
    ProcedureMaterialReservationStore,
    make_run_reservation,
    run_material_invocation_id,
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
        self.material_reservations = ProcedureMaterialReservationStore(bodies.reservation_root)

    def append(
        self,
        *,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        event_kind: JournalEventKindV1,
        accepted_coordinate: AcceptedCoordinate,
        definition_digest: str,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        payload: object,
        procedure_artifact_digest: str | None = None,
        run_id: str | None = None,
        admission_binding_digest: str | None = None,
        line_spec_digest: str | None = None,
        occurrence_id: str | None = None,
        attempt: int | None = None,
    ) -> StoredProcedureJournalRecordV1:
        payload_bytes = journal_payload_bytes(payload)
        body_digest = self.bodies.digest_bytes(payload_bytes).tagged
        reservation = make_run_reservation(
            instance_id=stream.instance_id,
            partition_id=partition_id,
            event_kind=event_kind,
            run_id=run_id,
            admission_binding_digest=admission_binding_digest,
            body_digest=body_digest,
        )
        with self.material_reservations.locked():
            self.material_reservations.reserve_locked(reservation)
            metadata = self.bodies.store(payload_bytes)
            head = self.journal.read_head(stream, partition_id)
            stored = self.journal.append(
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
            if (
                stored.record.payload_digest != reservation.body_digest
                or stored.record.event_kind != reservation.intended_event_kind
                or stored.record.run_id != reservation.run_id
                or stored.record.admission_binding_digest != reservation.admission_binding_digest
                or reservation.invocation_id
                != run_material_invocation_id(
                    instance_id=stored.record.stream.instance_id,
                    partition_id=stored.record.partition_id,
                    event_kind=stored.record.event_kind,
                    run_id=stored.record.run_id,
                    admission_binding_digest=stored.record.admission_binding_digest,
                    body_digest=stored.record.payload_digest,
                )
            ):
                raise RuntimeError("journal append did not reproduce its material reservation")
            self.material_reservations.release_locked(reservation.reservation_id)
            return stored


__all__ = ["ProcedureExhaustWriter"]
