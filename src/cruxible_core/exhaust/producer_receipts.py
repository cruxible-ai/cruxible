"""Resolve Capture producer receipts and production records from the daemon journal.

Only the daemon appends to the Procedure journal, under its fencing token, and
it checked every link below when it appended. A write that cites a Capture
therefore reads only the cited run's records (located by run id through the
journal index) and re-runs the link checks over that one run. A miss falls back
to one pass over the whole journal -- the index may lag an append made by
another process -- which is also what recovery uses (``full_journal``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import ValidationError

from cruxible_client.contracts.captures import (
    CaptureFormatError,
    CaptureProductionRecord,
    ProcedureProducerReceiptProtocol,
    ProviderProducerReceiptResolution,
    VerifiedCaptureMemo,
    provider_capture_receipt_matches_occurrence,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.provider_execution import (
    ProcedureDerivedSourceRequest,
    ProviderInvocationCompleted,
)
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_DIGESTS,
    SourceReadReceipt,
    WorkspaceFileSourceRequest,
    source_read_receipt_digest,
)
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.exhaust.records import StoredProcedureJournalRecordV1, parse_journal_payload
from cruxible_core.procedures.egress import (
    ProcedureProducerReceiptV1,
    TerminalEgressReceiptV2,
    procedure_producer_receipt_digest,
)
from cruxible_core.procedures.execution import (
    ProcedureAdmissionBoundPayloadV5,
    parse_admission_payload,
    procedure_line_journal_stream,
)
from cruxible_core.storage.cas import ContentAddressedBodyStore

#: The records a receipt or production proof is built from; no other payload is read.
_PROOF_EVENT_KINDS = frozenset(
    {
        "admission_bound",
        "source_request_derived",
        "source_read",
        "provider_invocation_completed",
        "terminal_egress",
        "produced_capture",
    }
)


class _VerifiedCaptures:
    """The Capture verifications one context finished, keyed by Capture digest."""

    def __init__(self) -> None:
        self._memos: dict[str, VerifiedCaptureMemo] = {}

    def recall(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        return self._memos.get(capture_digest)

    def remember(self, memo: VerifiedCaptureMemo) -> None:
        held = self._memos.get(memo.capture_digest)
        # A strict verification is the stronger fact: a lenient one never replaces it.
        if held is None or memo.production_proven or not held.production_proven:
            self._memos[memo.capture_digest] = memo


class LocalProducerReceiptResolver:
    """One verification context over the daemon's own journal, opened on first use."""

    def __init__(
        self,
        *,
        exhaust_root: Path,
        instance_id: str,
        bodies: ContentAddressedBodyStore,
        full_journal: bool,
    ) -> None:
        self._journal_root = exhaust_root / "procedure-runs"
        self._instance_id = instance_id
        self._bodies = bodies
        self._full_journal = full_journal
        self._resolver: JournalProducerReceiptResolver | None = None
        self._verified = _VerifiedCaptures()

    def _opened(self) -> JournalProducerReceiptResolver | None:
        if self._resolver is None:
            if not self._journal_root.exists():
                return None
            self._resolver = JournalProducerReceiptResolver(
                journal=LocalJournalBackend(self._journal_root),
                instance_id=self._instance_id,
                bodies=self._bodies,
                full_journal=self._full_journal,
            )
        return self._resolver

    def producer_receipt(
        self, digest: str, *, run_id: str
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        resolver = self._opened()
        return None if resolver is None else resolver.producer_receipt(digest, run_id=run_id)

    def produced_capture(
        self, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        resolver = self._opened()
        return () if resolver is None else resolver.produced_capture(capture_digest, run_id=run_id)

    def recall_verified(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        return self._verified.recall(capture_digest)

    def remember_verified(self, memo: VerifiedCaptureMemo) -> None:
        self._verified.remember(memo)


#: The verification context the current request owns, with the instance it serves.
_REQUEST_CONTEXT: ContextVar[tuple[tuple[str, str], LocalProducerReceiptResolver] | None] = (
    ContextVar("cruxible_capture_verification_request", default=None)
)


def requested_verification_context(
    instance_key: tuple[str, str],
) -> LocalProducerReceiptResolver | None:
    """The context a request already owns for this instance, if one is open."""

    active = _REQUEST_CONTEXT.get()
    return active[1] if active is not None and active[0] == instance_key else None


@contextmanager
def verification_request(
    instance_key: tuple[str, str],
    open_context: Callable[[], LocalProducerReceiptResolver],
) -> Iterator[LocalProducerReceiptResolver]:
    """Own one verification context for a whole request; a nested request shares it."""

    active = requested_verification_context(instance_key)
    if active is not None:
        yield active
        return
    context = open_context()
    token = _REQUEST_CONTEXT.set((instance_key, context))
    try:
        yield context
    finally:
        _REQUEST_CONTEXT.reset(token)


def local_producer_receipt_resolver(
    *,
    exhaust_root: Path,
    instance_id: str,
    bodies: ContentAddressedBodyStore,
    full_journal: bool = False,
) -> LocalProducerReceiptResolver:
    """One verification context over the daemon's own journal.

    Build one per request and pass it to every verification the request makes:
    each run's records are read once and each answer is kept for the context's
    life. ``full_journal`` indexes the whole journal on first use (recovery)
    instead of reading run by run.
    """

    return LocalProducerReceiptResolver(
        exhaust_root=exhaust_root,
        instance_id=instance_id,
        bodies=bodies,
        full_journal=full_journal,
    )


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
    """Receipts and production records from a verified Procedure exhaust journal.

    One instance is one verification context: what it read and resolved is kept
    for its life, keyed by receipt digest and by Capture digest, so N citations
    of one Capture read its run once. A lookup reads only the run it names;
    ``index_all`` makes one pass over every partition.
    """

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        instance_id: str,
        bodies: ContentAddressedBodyStore,
        note_sink: Callable[[ProducerReceiptJournalNote], None] | None = None,
        full_journal: bool = False,
    ) -> None:
        self._journal = journal
        self._stream = procedure_line_journal_stream(instance_id)
        self._bodies = bodies
        self._note_sink = note_sink
        self._full_journal = full_journal
        self._full = False
        #: How many runs this context located through the journal index.
        self.lookups = 0
        self._resolutions: dict[str, ProducerReceiptResolution] = {}
        self._errors: dict[str, CaptureFormatError] = {}
        self._productions: dict[str, set[CaptureProductionRecord]] = {}
        #: The journal payload bodies each run's answers were derived from: an
        #: answer stands only while every one of them is still held and intact.
        self._run_bodies: dict[str, set[str]] = {}
        self._error_runs: dict[str, str] = {}
        self._current_run = ""
        self._verified = _VerifiedCaptures()
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
            self._error_runs.setdefault(candidate_digest, self._current_run)

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

    def _produced(self, stored: StoredProcedureJournalRecordV1, payload: object) -> None:
        """Keep one ``produced_capture`` record's exact digests, or note it malformed."""

        record = stored.record
        try:
            if (
                not isinstance(payload, dict)
                or payload.get("tag") != "playbill-procedure-produced-capture-v1"
                or record.run_id is None
                or record.admission_binding_digest is None
            ):
                raise ValueError("produced-Capture record is not typed to its run")
            capture = payload.get("capture_digest")
            contract = payload.get("capture_contract_digest")
            receipt = payload.get(
                "producer_receipt_digest", payload.get("invocation_receipt_digest")
            )
            if (
                not isinstance(capture, str)
                or not isinstance(contract, str)
                or not (receipt is None or isinstance(receipt, str))
            ):
                raise ValueError("produced-Capture record digests are malformed")
        except ValueError as exc:
            self._malformed(
                record_digest=stored.record_digest,
                event_kind=record.event_kind,
                candidate_digest=None,
                exc=exc,
            )
            return
        self._productions.setdefault(capture, set()).add(
            CaptureProductionRecord(
                capture_digest=capture,
                capture_contract_digest=contract,
                run_id=record.run_id,
                receipt_digest=receipt,
            )
        )

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

    def index_all(self) -> None:
        """Index every partition once: recovery, and the fallback after a per-run miss."""

        if self._full:
            return
        try:
            partition_ids = self._journal.partition_ids(self._stream)
        except (OSError, CruxibleError, ValueError) as exc:
            raise CaptureFormatError("Capture producer receipt journal is unavailable") from exc
        for partition_id in partition_ids:
            try:
                records = self._journal.all_records(self._stream, partition_id)
            except (OSError, CruxibleError, ValueError) as exc:
                raise CaptureFormatError(
                    "Capture producer receipt journal partition is unavailable"
                ) from exc
            self._index_records(records)
        self._full = True

    def _index_run(self, run_id: str) -> None:
        """Index only one run's records, located through the journal index."""

        self.lookups += 1
        try:
            records = self._journal.select_records(self._stream, run_id=run_id)
        except (OSError, CruxibleError, ValueError) as exc:
            raise CaptureFormatError("Capture producer receipt journal is unavailable") from exc
        partitions: dict[str, list[StoredProcedureJournalRecordV1]] = {}
        for stored in records:
            partitions.setdefault(stored.record.partition_id, []).append(stored)
        for partition in partitions.values():
            self._index_records(partition)

    def _index_records(self, records: Iterable[StoredProcedureJournalRecordV1]) -> None:
        """Re-check the receipt links within one partition's records, in order."""

        admissions: dict[tuple[str, str], ProcedureAdmissionBoundPayloadV5] = {}
        derived_requests: dict[tuple[str, str], ProcedureDerivedSourceRequest | None] = {}
        source_reads: dict[tuple[str, str], SourceReadReceipt | None] = {}
        for stored in records:
            record = stored.record
            if record.event_kind not in _PROOF_EVENT_KINDS:
                continue
            self._current_run = record.run_id or ""
            try:
                payload = parse_journal_payload(
                    self._bodies.read(record.payload_digest, access=self._access)
                )
            except (KeyError, OSError, TypeError, CruxibleError, ValueError) as exc:
                self._malformed(
                    record_digest=stored.record_digest,
                    event_kind=record.event_kind,
                    candidate_digest=None,
                    exc=exc,
                )
                continue
            # What this run's answers rest on: re-checked before any is reused.
            self._run_bodies.setdefault(self._current_run, set()).add(record.payload_digest)
            candidate_digest = self._candidate_digest(record.event_kind, payload)
            if record.event_kind == "produced_capture":
                self._produced(stored, payload)
                continue
            if record.event_kind == "admission_bound" and isinstance(payload, dict):
                if payload.get("tag") not in {
                    "playbill-procedure-admission-bound-payload-v5",
                    "playbill-procedure-admission-bound-payload-v7",
                    "playbill-procedure-admission-bound-payload-v8",
                }:
                    continue
                try:
                    bound_payload = parse_admission_payload(payload)
                    if not isinstance(bound_payload, ProcedureAdmissionBoundPayloadV5):
                        raise ValueError("capture source requires an acquisition admission")
                    admission = bound_payload.admission
                    if (
                        record.run_id != admission.run_id
                        or record.admission_binding_digest != admission.admission_binding_digest
                        or record.accepted_coordinate != admission.accepted_coordinate
                        or record.procedure_artifact_digest != admission.procedure_artifact_digest
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
                admissions[(admission.run_id, admission.admission_binding_digest)] = bound_payload
                continue
            if record.event_kind == "source_request_derived":
                try:
                    derived = ProcedureDerivedSourceRequest.model_validate(payload)
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
                    receipt = SourceReadReceipt.model_validate(payload.get("receipt"))
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
                        raise ValueError("Source-read receipt has no exact derived Source request")
                    request = WorkspaceFileSourceRequest.model_validate(resolved_derived.request)
                    if (
                        receipt.derived_request_digest != resolved_derived.request_digest
                        or receipt.logical_source != request.logical_source
                        or receipt.workspace_binding_digest != request.workspace_binding_digest
                        # The receipt's relative_path carries the REAL on-disk
                        # names; requested_path is what the derived request
                        # asked for. Receipts written before the real-name law
                        # carry only the requested spelling in relative_path.
                        or (receipt.requested_path or receipt.relative_path)
                        != request.relative_path
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
                    completed = ProviderInvocationCompleted.model_validate(payload)
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
                    if (provider_receipt.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS) != (
                        source_read is not None
                    ):
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
                        or procedure_producer_receipt_digest(procedure_receipt) != candidate_digest
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
                assert record.run_id is not None  # checked with its admission above
                for produced in terminal.children:
                    self._productions.setdefault(produced.egress_digest, set()).add(
                        CaptureProductionRecord(
                            capture_digest=produced.egress_digest,
                            capture_contract_digest=procedure_receipt.capture_contract_digest,
                            run_id=record.run_id,
                            receipt_digest=candidate_digest,
                        )
                    )

    def _lookup(
        self,
        found: Callable[[], object],
        *,
        run_id: str,
    ) -> None:
        """Index the cited run, then the whole journal, until ``found`` answers.

        Only answers are kept, each only while the journal payloads it was
        derived from are held (``_still_proven``). A miss reads the run again,
        since a context may outlive an append to a run it already read; the
        whole journal is read at most once per context.
        """

        if found() is not None:
            return
        self._index_run(run_id)
        if found() is None and not self._full:
            self.index_all()

    def _still_proven(self, run_ids: Iterable[str]) -> bool:
        """Whether every journal payload these runs' answers were derived from is held."""

        for run_id in dict.fromkeys(run_ids):
            for body in sorted(self._run_bodies.get(run_id, ())):
                try:
                    if not self._bodies.verify(body):
                        return False
                except (OSError, CruxibleError, ValueError):
                    # Unreadable as cold resolution would find it: re-derive.
                    return False
        return True

    def _forget(self) -> None:
        """Drop every derived answer: the context is cold again and re-derives."""

        self._resolutions.clear()
        self._errors.clear()
        self._error_runs.clear()
        self._productions.clear()
        self._run_bodies.clear()
        self._full = False

    def _receipt_runs(self, digest: str) -> tuple[str, ...]:
        if digest in self._errors:
            return (self._error_runs.get(digest, ""),)
        resolution = self._resolutions.get(digest)
        if resolution is None:
            return ()
        if isinstance(resolution, ProviderProducerReceiptResolution):
            return (resolution.receipt.run_id,)
        return (resolution.run_id,)

    def _resolution(self, digest: str) -> ProducerReceiptResolution | None:
        error = self._errors.get(digest)
        if error is not None:
            raise error
        return self._resolutions.get(digest)

    def producer_receipt(
        self,
        digest: str,
        *,
        run_id: str,
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        if not self._still_proven(self._receipt_runs(digest)):
            self._forget()
        if self._full_journal:
            self.index_all()
        self._lookup(lambda: self._resolution(digest), run_id=run_id)
        return self._resolution(digest)

    def produced_capture(
        self,
        capture_digest: str,
        *,
        run_id: str,
    ) -> tuple[CaptureProductionRecord, ...]:
        if not self._still_proven(
            record.run_id for record in self._productions.get(capture_digest, ())
        ):
            self._forget()
        if self._full_journal:
            self.index_all()
        self._lookup(lambda: self._productions.get(capture_digest), run_id=run_id)
        return tuple(self._productions.get(capture_digest, ()))

    def recall_verified(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        return self._verified.recall(capture_digest)

    def remember_verified(self, memo: VerifiedCaptureMemo) -> None:
        self._verified.remember(memo)


def journal_producer_receipt_resolver(
    *,
    journal: LocalJournalBackend,
    instance_id: str,
    bodies: ContentAddressedBodyStore,
    note_sink: Callable[[ProducerReceiptJournalNote], None] | None = None,
    full_journal: bool = False,
) -> JournalProducerReceiptResolver:
    """Bind receipt resolution to an already-open verified journal backend."""
    return JournalProducerReceiptResolver(
        journal=journal,
        instance_id=instance_id,
        bodies=bodies,
        note_sink=note_sink,
        full_journal=full_journal,
    )


__all__ = [
    "JournalProducerReceiptResolver",
    "requested_verification_context",
    "verification_request",
    "LocalProducerReceiptResolver",
    "ProducerReceiptJournalNote",
    "journal_producer_receipt_resolver",
    "local_producer_receipt_resolver",
]
