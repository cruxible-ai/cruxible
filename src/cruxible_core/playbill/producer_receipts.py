"""Resolve Capture producer receipts from the daemon-owned exhaust journal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import ValidationError

from cruxible_client.contracts.captures import (
    CaptureFormatError,
    ProcedureProducerReceiptProtocol,
    ProducerReceiptResolverProtocol,
    ProviderProducerReceiptResolution,
    provider_capture_receipt_matches_occurrence,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.provider_execution import (
    ProcedureDerivedSourceRequestV1,
    ProviderInvocationCompletedV1,
)
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_DIGEST,
    SourceReadReceiptV1,
    WorkspaceFileSourceRequestV1,
    source_read_receipt_digest,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import LocalJournalBackend
from cruxible_core.playbill.exhaust.records import parse_journal_payload
from cruxible_core.playbill.procedures.egress import (
    ProcedureProducerReceiptV1,
    TerminalEgressReceiptV2,
    procedure_producer_receipt_digest,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureAdmissionBoundPayloadV5,
    procedure_line_journal_stream,
)


def local_producer_receipt_resolver(
    *,
    exhaust_root: Path,
    instance_id: str,
    bodies: ContentAddressedBodyStore,
) -> ProducerReceiptResolverProtocol:
    """Return a resolver backed only by validated, admission-bound journal records."""

    resolver: JournalProducerReceiptResolver | None = None

    def resolve(
        digest: str,
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        nonlocal resolver
        journal_root = exhaust_root / "procedure-runs"
        if not journal_root.exists():
            return None
        if resolver is None:
            resolver = journal_producer_receipt_resolver(
                journal=LocalJournalBackend(journal_root),
                instance_id=instance_id,
                bodies=bodies,
            )
        return resolver(digest)

    return resolve


@dataclass(frozen=True)
class ProducerReceiptJournalNote:
    """Typed account of one skipped or digest-addressable malformed record."""

    code: Literal[
        "producer_receipt_unrelated_record_skipped",
        "producer_receipt_malformed_candidate",
    ]
    record_digest: str
    event_kind: str
    producer_receipt_digest: str | None
    message: str


ProducerReceiptResolution: TypeAlias = (
    ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol
)


class JournalProducerReceiptResolver:
    """One-pass digest index over a verified Procedure exhaust journal."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        instance_id: str,
        bodies: ContentAddressedBodyStore,
        note_sink: Callable[[ProducerReceiptJournalNote], None] | None = None,
    ) -> None:
        self._journal = journal
        self._instance_id = instance_id
        self._bodies = bodies
        self._note_sink = note_sink
        self._indexed = False
        self._resolutions: dict[str, ProducerReceiptResolution] = {}
        self._errors: dict[str, CaptureFormatError] = {}
        self._notes: list[ProducerReceiptJournalNote] = []
        self._access = BodyAccessContext(
            principal_id="capture-verifier",
            can_read_body=True,
        )

    @property
    def notes(self) -> tuple[ProducerReceiptJournalNote, ...]:
        return tuple(self._notes)

    def _note(
        self,
        *,
        record_digest: str,
        event_kind: str,
        candidate_digest: str | None,
        message: str,
    ) -> None:
        note = ProducerReceiptJournalNote(
            code=(
                "producer_receipt_unrelated_record_skipped"
                if candidate_digest is None
                else "producer_receipt_malformed_candidate"
            ),
            record_digest=record_digest,
            event_kind=event_kind,
            producer_receipt_digest=candidate_digest,
            message=message,
        )
        self._notes.append(note)
        if self._note_sink is not None:
            self._note_sink(note)

    def _malformed(
        self,
        *,
        record_digest: str,
        event_kind: str,
        candidate_digest: str | None,
        exc: Exception,
    ) -> None:
        message = f"malformed {event_kind} record: {type(exc).__name__}: {exc}"
        self._note(
            record_digest=record_digest,
            event_kind=event_kind,
            candidate_digest=candidate_digest,
            message=message,
        )
        if candidate_digest is not None:
            self._errors.setdefault(
                candidate_digest,
                CaptureFormatError(
                    "Capture producer receipt journal is invalid for " + candidate_digest
                ),
            )

    @staticmethod
    def _candidate_digest(event_kind: str, payload: object) -> str | None:
        if not isinstance(payload, dict):
            return None
        if event_kind == "provider_invocation_completed":
            candidate = payload.get("receipt_digest")
            return candidate if isinstance(candidate, str) else None
        if event_kind == "terminal_egress":
            receipt = payload.get("receipt")
            if isinstance(receipt, dict):
                candidate = receipt.get("producer_receipt_digest")
                return candidate if isinstance(candidate, str) else None
        return None

    def _store(self, digest: str, resolution: ProducerReceiptResolution) -> None:
        existing = self._resolutions.get(digest)
        if existing is not None and existing != resolution:
            self._resolutions.pop(digest, None)
            self._errors[digest] = CaptureFormatError(
                f"Capture producer receipt journal is ambiguous for {digest}"
            )
            return
        if digest not in self._errors:
            self._resolutions[digest] = resolution

    def _index(self) -> None:
        if self._indexed:
            return
        stream = procedure_line_journal_stream(self._instance_id)
        try:
            partition_ids = self._journal.partition_ids(stream)
        except (OSError, PlaybillError, ValueError) as exc:
            raise CaptureFormatError("Capture producer receipt journal is unavailable") from exc
        for partition_id in partition_ids:
            admissions: dict[tuple[str, str], ProcedureAdmissionBoundPayloadV5] = {}
            derived_requests: dict[tuple[str, str], ProcedureDerivedSourceRequestV1 | None] = {}
            source_reads: dict[tuple[str, str], SourceReadReceiptV1 | None] = {}
            try:
                records = self._journal.all_records(stream, partition_id)
            except (OSError, PlaybillError, ValueError) as exc:
                raise CaptureFormatError(
                    "Capture producer receipt journal partition is unavailable"
                ) from exc
            for stored in records:
                record = stored.record
                try:
                    payload = parse_journal_payload(
                        self._bodies.read(record.payload_digest, access=self._access)
                    )
                except (KeyError, OSError, TypeError, PlaybillError, ValueError) as exc:
                    self._malformed(
                        record_digest=stored.record_digest,
                        event_kind=record.event_kind,
                        candidate_digest=None,
                        exc=exc,
                    )
                    continue
                candidate_digest = self._candidate_digest(record.event_kind, payload)
                if record.event_kind == "admission_bound" and isinstance(payload, dict):
                    if payload.get("tag") != "playbill-procedure-admission-bound-payload-v5":
                        continue
                    try:
                        bound_payload = ProcedureAdmissionBoundPayloadV5.model_validate(payload)
                        admission = bound_payload.admission
                        if (
                            record.run_id != admission.run_id
                            or record.admission_binding_digest != admission.admission_binding_digest
                            or record.accepted_coordinate != admission.accepted_coordinate
                            or record.procedure_artifact_digest
                            != admission.procedure_artifact_digest
                        ):
                            raise ValueError(
                                "Capture producer admission journal coordinates do not correspond"
                            )
                    except (KeyError, TypeError, ValidationError, ValueError) as exc:
                        self._malformed(
                            record_digest=stored.record_digest,
                            event_kind=record.event_kind,
                            candidate_digest=None,
                            exc=exc,
                        )
                        continue
                    admissions[(admission.run_id, admission.admission_binding_digest)] = (
                        bound_payload
                    )
                    continue
                if record.event_kind == "source_request_derived":
                    try:
                        derived = ProcedureDerivedSourceRequestV1.model_validate(payload)
                        if (
                            record.run_id != derived.run_id
                            or record.admission_binding_digest != derived.admission_binding_digest
                        ):
                            raise ValueError(
                                "Derived Source request journal coordinates do not correspond"
                            )
                        key = (derived.run_id, derived.occurrence_path)
                        derived_requests[key] = None if key in derived_requests else derived
                    except (KeyError, TypeError, ValidationError, ValueError) as exc:
                        self._malformed(
                            record_digest=stored.record_digest,
                            event_kind=record.event_kind,
                            candidate_digest=None,
                            exc=exc,
                        )
                    continue
                if record.event_kind == "source_read":
                    try:
                        if not isinstance(payload, dict):
                            raise ValueError("Source-read payload is not an object")
                        receipt = SourceReadReceiptV1.model_validate(payload.get("receipt"))
                        digest = payload.get("receipt_digest")
                        if digest != source_read_receipt_digest(receipt):
                            raise ValueError("Source-read receipt digest does not reproduce")
                        if (
                            record.run_id != receipt.run_id
                            or record.admission_binding_digest != receipt.admission_binding_digest
                        ):
                            raise ValueError("Source-read journal coordinates do not correspond")
                        key = (receipt.run_id, receipt.occurrence_path)
                        resolved_derived = derived_requests.get(key)
                        if resolved_derived is None:
                            raise ValueError(
                                "Source-read receipt has no exact derived Source request"
                            )
                        request = WorkspaceFileSourceRequestV1.model_validate(
                            resolved_derived.request
                        )
                        if (
                            receipt.derived_request_digest != resolved_derived.request_digest
                            or receipt.logical_source != request.logical_source
                            or receipt.workspace_binding_digest != request.workspace_binding_digest
                            or receipt.relative_path != request.relative_path
                        ):
                            raise ValueError(
                                "Source-read receipt differs from its derived Source request"
                            )
                        source_reads[key] = None if key in source_reads else receipt
                    except (KeyError, TypeError, ValidationError, ValueError) as exc:
                        self._malformed(
                            record_digest=stored.record_digest,
                            event_kind=record.event_kind,
                            candidate_digest=None,
                            exc=exc,
                        )
                    continue
                if record.event_kind == "provider_invocation_completed":
                    try:
                        if candidate_digest is None:
                            raise ValueError("Provider completion omits receipt_digest")
                        completed = ProviderInvocationCompletedV1.model_validate(payload)
                        provider_receipt = completed.receipt
                        binding_digest = record.admission_binding_digest
                        admitted = (
                            None
                            if binding_digest is None or record.run_id is None
                            else admissions.get((record.run_id, binding_digest))
                        )
                        if admitted is None or binding_digest is None:
                            raise ValueError("Provider completion has no exact admission")
                        admission = admitted.admission
                        occurrences = tuple(
                            occurrence
                            for occurrence in admitted.acquisition_plan.external_occurrences
                            if occurrence.occurrence_path == provider_receipt.occurrence_path
                        )
                        if (
                            completed.receipt_digest != candidate_digest
                            or len(occurrences) != 1
                            or provider_receipt.run_id != admission.run_id
                            or provider_receipt.admission_binding_digest != binding_digest
                            or not provider_capture_receipt_matches_occurrence(
                                provider_receipt, occurrences[0]
                            )
                        ):
                            raise ValueError(
                                "Provider completion differs from its exact admitted occurrence"
                            )
                        source_read = source_reads.get(
                            (provider_receipt.run_id, provider_receipt.occurrence_path)
                        )
                        if (
                            provider_receipt.interface_digest == WORKSPACE_FILE_INTERFACE_DIGEST
                        ) != (source_read is not None):
                            raise ValueError(
                                "Provider completion source-read receipt does not correspond"
                            )
                        if source_read is not None and (
                            source_read.run_id != provider_receipt.run_id
                            or source_read.provider_input_digest != provider_receipt.input_digest
                            or source_read.policy_coordinate != admission.accepted_coordinate
                        ):
                            raise ValueError(
                                "Provider completion source-read receipt differs from invocation"
                            )
                    except (KeyError, TypeError, ValidationError, ValueError) as exc:
                        self._malformed(
                            record_digest=stored.record_digest,
                            event_kind=record.event_kind,
                            candidate_digest=candidate_digest,
                            exc=exc,
                        )
                        continue
                    self._store(
                        candidate_digest,
                        ProviderProducerReceiptResolution(
                            receipt=provider_receipt,
                            occurrence=occurrences[0],
                            source_read_receipt=source_read,
                        ),
                    )
                    continue
                if (
                    record.event_kind == "terminal_egress"
                    and isinstance(payload, dict)
                    and payload.get("verdict") == "delivered"
                    and payload.get("kind") == "emit_capture"
                ):
                    try:
                        if candidate_digest is None:
                            raise ValueError("Capture terminal omits producer receipt digest")
                        binding_digest = record.admission_binding_digest
                        admitted = (
                            None
                            if binding_digest is None or record.run_id is None
                            else admissions.get((record.run_id, binding_digest))
                        )
                        if admitted is None or binding_digest is None:
                            raise ValueError("Capture terminal has no exact admission")
                        admission = admitted.admission
                        terminal = TerminalEgressReceiptV2.model_validate(payload.get("receipt"))
                        children = payload.get("children")
                        if not isinstance(children, list):
                            raise ValueError("Capture producer terminal children are malformed")
                        manifests: list[str] = []
                        for child in children:
                            manifest_digest = (
                                child.get("manifest_digest") if isinstance(child, dict) else None
                            )
                            if not isinstance(manifest_digest, str):
                                raise ValueError("Capture producer terminal child is malformed")
                            manifests.append(manifest_digest)
                        if terminal.bound_artifact_digest is None:
                            raise ValueError("Capture producer terminal omits its contract")
                        procedure_receipt = ProcedureProducerReceiptV1(
                            admission_binding_digest=binding_digest,
                            run_id=admission.run_id,
                            accepted_coordinate=admission.accepted_coordinate,
                            procedure_identity=admission.procedure_identity,
                            procedure_artifact_digest=admission.procedure_artifact_digest,
                            terminal_node_id=str(payload.get("node_id")),
                            item_manifest_digests=tuple(manifests),
                            capture_contract_digest=terminal.bound_artifact_digest,
                        )
                        if (
                            terminal.producer_receipt_digest != candidate_digest
                            or procedure_producer_receipt_digest(procedure_receipt)
                            != candidate_digest
                        ):
                            raise ValueError("Capture terminal producer receipt does not reproduce")
                    except (KeyError, TypeError, ValidationError, ValueError) as exc:
                        self._malformed(
                            record_digest=stored.record_digest,
                            event_kind=record.event_kind,
                            candidate_digest=candidate_digest,
                            exc=exc,
                        )
                        continue
                    self._store(candidate_digest, procedure_receipt)
        self._indexed = True

    def __call__(
        self,
        digest: str,
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        self._index()
        error = self._errors.get(digest)
        if error is not None:
            raise error
        return self._resolutions.get(digest)


def journal_producer_receipt_resolver(
    *,
    journal: LocalJournalBackend,
    instance_id: str,
    bodies: ContentAddressedBodyStore,
    note_sink: Callable[[ProducerReceiptJournalNote], None] | None = None,
) -> JournalProducerReceiptResolver:
    """Bind receipt resolution to an already-open verified journal backend."""
    return JournalProducerReceiptResolver(
        journal=journal,
        instance_id=instance_id,
        bodies=bodies,
        note_sink=note_sink,
    )


__all__ = [
    "JournalProducerReceiptResolver",
    "ProducerReceiptJournalNote",
    "journal_producer_receipt_resolver",
    "local_producer_receipt_resolver",
]
