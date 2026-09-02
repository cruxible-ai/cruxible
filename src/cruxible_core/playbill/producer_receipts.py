"""Resolve Capture producer receipts from the daemon-owned exhaust journal."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from cruxible_client.contracts.captures import (
    CaptureFormatError,
    ProcedureProducerReceiptProtocol,
    ProducerReceiptResolverProtocol,
    provider_capture_receipt_matches_occurrence,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.provider_execution import (
    ProviderInvocationCompletedV1,
    ProviderInvocationReceiptV1,
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

    def resolve(
        digest: str,
    ) -> ProviderInvocationReceiptV1 | ProcedureProducerReceiptProtocol | None:
        journal_root = exhaust_root / "procedure-runs"
        if not journal_root.exists():
            return None
        resolver = journal_producer_receipt_resolver(
            journal=LocalJournalBackend(journal_root),
            instance_id=instance_id,
            bodies=bodies,
        )
        return resolver(digest)

    return resolve


def journal_producer_receipt_resolver(
    *,
    journal: LocalJournalBackend,
    instance_id: str,
    bodies: ContentAddressedBodyStore,
) -> ProducerReceiptResolverProtocol:
    """Bind receipt resolution to an already-open verified journal backend."""

    access = BodyAccessContext(principal_id="capture-verifier", can_read_body=True)

    def resolve(
        digest: str,
    ) -> ProviderInvocationReceiptV1 | ProcedureProducerReceiptProtocol | None:
        try:
            stream = procedure_line_journal_stream(instance_id)
            for partition_id in journal.partition_ids(stream):
                admissions: dict[str, ProcedureAdmissionBoundPayloadV5] = {}
                for stored in journal.all_records(stream, partition_id):
                    record = stored.record
                    payload = parse_journal_payload(
                        bodies.read(record.payload_digest, access=access)
                    )
                    if record.event_kind == "admission_bound" and isinstance(payload, dict):
                        if payload.get("tag") != "playbill-procedure-admission-bound-payload-v5":
                            continue
                        bound_payload = ProcedureAdmissionBoundPayloadV5.model_validate(payload)
                        admission = bound_payload.admission
                        if (
                            record.run_id != admission.run_id
                            or record.admission_binding_digest != admission.admission_binding_digest
                            or record.accepted_coordinate != admission.accepted_coordinate
                            or record.procedure_artifact_digest
                            != admission.procedure_artifact_digest
                        ):
                            raise CaptureFormatError(
                                "Capture producer admission journal coordinates do not correspond"
                            )
                        admissions[admission.admission_binding_digest] = bound_payload
                        continue
                    binding_digest = record.admission_binding_digest
                    admitted = None if binding_digest is None else admissions.get(binding_digest)
                    if admitted is None or binding_digest is None:
                        continue
                    admission = admitted.admission
                    if record.event_kind == "provider_invocation_completed":
                        completed = ProviderInvocationCompletedV1.model_validate(payload)
                        provider_receipt = completed.receipt
                        occurrences = tuple(
                            occurrence
                            for occurrence in admitted.acquisition_plan.external_occurrences
                            if occurrence.occurrence_path == provider_receipt.occurrence_path
                        )
                        if (
                            completed.receipt_digest == digest
                            and len(occurrences) == 1
                            and provider_receipt.run_id == admission.run_id
                            and provider_receipt.admission_binding_digest == binding_digest
                            and provider_capture_receipt_matches_occurrence(
                                provider_receipt, occurrences[0]
                            )
                        ):
                            return provider_receipt
                    if (
                        record.event_kind == "terminal_egress"
                        and isinstance(payload, dict)
                        and payload.get("verdict") == "delivered"
                        and payload.get("kind") == "emit_capture"
                    ):
                        terminal = TerminalEgressReceiptV2.model_validate(payload.get("receipt"))
                        children = payload.get("children")
                        if not isinstance(children, list):
                            raise CaptureFormatError(
                                "Capture producer terminal journal children are malformed"
                            )
                        manifests: list[str] = []
                        for child in children:
                            manifest_digest = (
                                child.get("manifest_digest") if isinstance(child, dict) else None
                            )
                            if not isinstance(manifest_digest, str):
                                raise CaptureFormatError(
                                    "Capture producer terminal journal child is malformed"
                                )
                            manifests.append(manifest_digest)
                        if terminal.bound_artifact_digest is None:
                            raise CaptureFormatError(
                                "Capture producer terminal journal omits its contract"
                            )
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
                            terminal.producer_receipt_digest == digest
                            and procedure_producer_receipt_digest(procedure_receipt) == digest
                        ):
                            return procedure_receipt
        except (KeyError, TypeError, ValidationError, PlaybillError, ValueError) as exc:
            raise CaptureFormatError(
                f"Capture producer receipt journal is invalid for {digest}"
            ) from exc
        return None

    return resolve


__all__ = ["journal_producer_receipt_resolver", "local_producer_receipt_resolver"]
