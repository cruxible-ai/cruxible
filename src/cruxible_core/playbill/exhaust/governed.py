"""Open governed-journal client contracts and remote refusal types.

A governed home sits above the journal storage seam: callers describe an event,
while the home assigns chain coordinates and authenticated actor attribution.
The contracts here therefore do not implement ``JournalBackendProtocol`` and do
not accept a fully bound journal-record draft.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeVar, cast, runtime_checkable
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from cruxible_core.playbill.canonical import ArtifactDigest, normalize_canonical, typed_digest
from cruxible_core.playbill.errors import CanonicalEncodingError, PlaybillJournalError
from cruxible_core.playbill.exhaust.interchange import (
    JournalExportBundleV1,
    parse_journal_export,
)
from cruxible_core.playbill.exhaust.records import (
    JournalHeadManifestV1,
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    ProcedureJournalRecordDraftV1,
    ProcedureJournalRecordV1,
    StoredProcedureJournalRecordV1,
    journal_genesis_digest,
    journal_head_key,
    verify_journal_head_manifest,
    verify_journal_range,
)

_FENCING_TOKEN_HEADER = "X-Cruxible-Journal-Fencing-Token"
_HOME_ASSIGNED_APPEND_FIELDS = frozenset(
    {
        "actor_context",
        "partition_id",
        "previous_record_digest",
        "sequence",
        "stream",
        "tag",
    }
)
_CONFLICT_REFUSAL_IDS = frozenset(
    {
        "journal_idempotency_conflict",
        "journal_law_refused",
        "journal_writer_lease_conflict",
        "journal_writer_lease_invalid",
    }
)
_IDEMPOTENCY_DOMAIN = "playbill-governed-journal-append-idempotency-v1"

ModelT = TypeVar("ModelT", bound=BaseModel)


class RemoteJournalError(PlaybillJournalError):
    """Base refusal for communication with a governed journal home."""


class RemoteJournalTransportError(RemoteJournalError):
    """The journal home could not be reached through the configured transport."""


class RemoteJournalRefusal(RemoteJournalError):
    """The home refused an operation under an opaque refusal identifier."""

    def __init__(
        self,
        *,
        remote_status: int,
        refusal_id: str | None,
    ) -> None:
        self.remote_status = remote_status
        self.refusal_id = refusal_id
        identifier = refusal_id if refusal_id is not None else "unspecified"
        super().__init__(
            f"remote journal refused the operation with status {remote_status} "
            f"and identifier {identifier!r}"
        )


class RemoteJournalConflict(RemoteJournalRefusal):
    """A writer fence, expected head, or idempotent append conflicted."""


class RemoteJournalVerificationError(RemoteJournalError):
    """A successful response failed coordinate, digest, chain, or proof checks."""


class JournalCoverageState(str, Enum):
    """What a journal home can honestly prove about an addressed prefix."""

    EXACT = "exact"
    TRUNCATED = "truncated"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class JournalCoverage:
    """Coverage of requested partitions, with missing history named explicitly."""

    state: JournalCoverageState
    partitions: tuple[JournalPartitionHeadV1, ...] = ()
    reason: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.state is JournalCoverageState.EXACT


@dataclass(frozen=True)
class JournalWriterGrant:
    """One active writer generation and its opaque fencing token."""

    partition_id: str
    generation: int
    fencing_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("journal writer generation must be positive")
        if not self.fencing_token:
            raise ValueError("journal writer grant requires a fencing token")


@dataclass(frozen=True)
class JournalAppendOutcome:
    """The exact stored record an append committed or idempotently replayed."""

    record: StoredProcedureJournalRecordV1
    head: JournalPartitionHeadV1
    replayed: bool
    operation_id: str


@dataclass(frozen=True)
class JournalTransfer:
    """One normalized export and the signed retained heads that authorize it."""

    payload: bytes = field(repr=False)
    head_manifest: JournalHeadManifestV1
    expected_head_public_key: str
    segment_count: int
    record_count: int

    def __post_init__(self) -> None:
        if not self.payload:
            raise ValueError("journal transfer payload cannot be empty")
        if (
            isinstance(self.segment_count, bool)
            or isinstance(self.record_count, bool)
            or self.segment_count < 1
            or self.record_count < 1
        ):
            raise ValueError("journal transfer counts must be positive")

    @property
    def head_vector(self) -> JournalHeadVectorV1:
        return self.head_manifest.statement.head_vector


@dataclass(frozen=True)
class JournalHeadProof:
    """A home's signed proof that it retains the exact named prefixes."""

    manifest: JournalHeadManifestV1
    expected_public_key: str

    def verify(self) -> JournalHeadVectorV1:
        verify_journal_head_manifest(
            self.manifest,
            expected_public_key=self.expected_public_key,
        )
        return self.manifest.statement.head_vector


@runtime_checkable
class GovernedJournalClientProtocol(Protocol):
    """Governed-home operations above the journal storage backend seam."""

    @property
    def identity(self) -> JournalStreamIdentityV1: ...

    def read_head(self, partition_id: str) -> JournalPartitionHeadV1: ...

    def read_head_vector(self, partition_ids: Sequence[str]) -> JournalHeadVectorV1: ...

    def read_exact_range(
        self,
        journal_range: JournalRangeV1,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]: ...

    def coverage(self, partition_ids: Sequence[str]) -> JournalCoverage: ...

    def acquire_writer(
        self,
        partition_id: str,
        *,
        expected_head: JournalPartitionHeadV1,
    ) -> JournalWriterGrant: ...

    def append(
        self,
        partition_id: str,
        *,
        content: Mapping[str, object],
        expected_head: JournalPartitionHeadV1,
        fencing_token: str,
    ) -> JournalAppendOutcome: ...

    def fence_writer(
        self,
        partition_id: str,
        *,
        fencing_token: str,
        expected_generation: int | None = None,
    ) -> None: ...

    def export(self, partition_ids: Sequence[str]) -> JournalTransfer: ...

    def import_transfer(
        self,
        transfer: JournalTransfer,
    ) -> tuple[JournalPartitionHeadV1, ...]: ...

    def head_proof(self, partition_ids: Sequence[str]) -> JournalHeadProof: ...

    def complete_handoff(
        self,
        *,
        target_proof: JournalHeadProof,
        source_fencing_tokens: Mapping[str, str],
        partition_ids: Sequence[str],
    ) -> None: ...


class HttpGovernedJournalClient:
    """Untrusted HTTP peer implementing ``GovernedJournalClientProtocol``.

    The endpoint root, peer format, authorization value, and any extra write
    headers are injected. This client owns none of their lifecycle and retains
    no default endpoint or format profile.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        endpoint_root: str,
        home_stream_id: str,
        identity: JournalStreamIdentityV1,
        authorization_header: str,
        format_version: str,
        write_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._endpoint_root = _nonblank(endpoint_root, "endpoint_root").rstrip("/")
        if not self._endpoint_root:
            raise ValueError("endpoint_root cannot resolve to an empty URL")
        self._home_stream_id = _nonblank(home_stream_id, "home_stream_id")
        self._identity = identity
        self._authorization_header = _nonblank(
            authorization_header,
            "authorization_header",
        )
        self._format_version = _nonblank(format_version, "format_version")
        self._write_header_extensions = _write_header_extensions(write_headers)

    @property
    def identity(self) -> JournalStreamIdentityV1:
        return self._identity

    @property
    def _stream_url(self) -> str:
        stream_segment = quote(self._home_stream_id, safe="")
        return f"{self._endpoint_root}/journal/streams/{stream_segment}"

    def _partition_url(self, partition_id: str) -> str:
        self._validate_partition_id(partition_id)
        return f"{self._stream_url}/partitions/{quote(partition_id, safe='')}"

    @property
    def _read_headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization_header}

    def _write_headers(self, fencing_token: str | None = None) -> dict[str, str]:
        headers = {**self._read_headers, **self._write_header_extensions}
        if fencing_token is not None:
            headers[_FENCING_TOKEN_HEADER] = _nonblank(fencing_token, "fencing_token")
        return headers

    def read_head(self, partition_id: str) -> JournalPartitionHeadV1:
        document = self._request(
            "GET",
            f"{self._partition_url(partition_id)}/head",
            headers=self._read_headers,
        )
        head = _model(JournalPartitionHeadV1, document.get("head"), "head")
        self._require_head_scope(head, partition_id)
        return head

    def read_head_vector(self, partition_ids: Sequence[str]) -> JournalHeadVectorV1:
        requested = self._partition_ids(partition_ids, allow_empty=True)
        heads = tuple(self.read_head(partition_id) for partition_id in requested)
        return JournalHeadVectorV1(partitions=tuple(sorted(heads, key=journal_head_key)))

    def read_exact_range(
        self,
        journal_range: JournalRangeV1,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        if journal_range.stream != self.identity:
            raise RemoteJournalVerificationError(
                "requested range names another logical journal stream"
            )
        document = self._request(
            "GET",
            f"{self._partition_url(journal_range.partition_id)}/records",
            headers=self._read_headers,
            params={
                "first_sequence": journal_range.first_sequence,
                "last_sequence": journal_range.last_sequence,
            },
        )
        returned_range = _model(JournalRangeV1, document.get("range"), "range")
        if returned_range != journal_range:
            raise RemoteJournalVerificationError(
                "range response substituted its requested coordinate"
            )
        raw_records = document.get("records")
        if not isinstance(raw_records, list):
            raise RemoteJournalVerificationError("range response has no records list")
        records = tuple(
            _model(StoredProcedureJournalRecordV1, item, f"records[{index}]")
            for index, item in enumerate(raw_records)
        )
        try:
            verify_journal_range(journal_range, records)
        except (PlaybillJournalError, ValueError) as exc:
            raise RemoteJournalVerificationError(
                "range response failed journal-chain verification"
            ) from exc
        return records

    def coverage(self, partition_ids: Sequence[str]) -> JournalCoverage:
        requested = self._partition_ids(partition_ids, allow_empty=True)
        if not requested:
            return JournalCoverage(
                state=JournalCoverageState.UNAVAILABLE,
                reason="no journal partitions were addressed",
            )
        response = self._send(
            "GET",
            f"{self._stream_url}/coverage",
            headers=self._read_headers,
        )
        if response.status_code in {401, 403}:
            return JournalCoverage(
                state=JournalCoverageState.UNAUTHORIZED,
                reason="the configured caller cannot read journal coverage",
            )
        document = self._response_document(response, conflict_capable=False)
        report = document.get("coverage")
        if not isinstance(report, dict):
            raise RemoteJournalVerificationError("coverage response has no coverage object")
        raw_partitions = report.get("partitions")
        if not isinstance(raw_partitions, list):
            raise RemoteJournalVerificationError("coverage response has no partition coverage list")
        by_id: dict[str, Mapping[str, object]] = {}
        for index, item in enumerate(raw_partitions):
            if not isinstance(item, dict) or not isinstance(item.get("partition_id"), str):
                raise RemoteJournalVerificationError(
                    f"coverage partition {index} has no coordinate"
                )
            partition_id = cast(str, item["partition_id"])
            if partition_id in by_id:
                raise RemoteJournalVerificationError(
                    "coverage response repeats a partition coordinate"
                )
            by_id[partition_id] = cast(Mapping[str, object], item)
        missing = sorted(set(requested) - set(by_id))
        if missing:
            return JournalCoverage(
                state=JournalCoverageState.UNAVAILABLE,
                reason=f"coverage omitted addressed partitions {missing}",
            )

        states: list[JournalCoverageState] = []
        heads: list[JournalPartitionHeadV1] = []
        reasons: list[str] = []
        report_reason = report.get("reason")
        if isinstance(report_reason, str) and report_reason:
            reasons.append(report_reason)
        for partition_id in requested:
            item = by_id[partition_id]
            try:
                state = JournalCoverageState(str(item.get("coverage")))
            except ValueError as exc:
                raise RemoteJournalVerificationError(
                    f"coverage returned an unknown state for partition {partition_id!r}"
                ) from exc
            states.append(state)
            reason = item.get("reason")
            if isinstance(reason, str) and reason:
                reasons.append(reason)
            if state is JournalCoverageState.UNAVAILABLE:
                return JournalCoverage(
                    state=JournalCoverageState.UNAVAILABLE,
                    reason="; ".join(reasons)
                    or f"coverage is unavailable for partition {partition_id!r}",
                )
            try:
                head = self.read_head(partition_id)
            except RemoteJournalRefusal:
                return JournalCoverage(
                    state=JournalCoverageState.UNAVAILABLE,
                    reason="; ".join(reasons)
                    or f"authoritative head is unavailable for partition {partition_id!r}",
                )
            if (
                item.get("head_sequence") != head.sequence
                or item.get("head_record_digest") != head.record_digest
            ):
                return JournalCoverage(
                    state=JournalCoverageState.UNAVAILABLE,
                    reason=f"coverage and authoritative head disagree for {partition_id!r}",
                )
            heads.append(head)
        return JournalCoverage(
            state=_narrowest_coverage(tuple(states)),
            partitions=tuple(heads),
            reason="; ".join(reasons) or None,
        )

    def acquire_writer(
        self,
        partition_id: str,
        *,
        expected_head: JournalPartitionHeadV1,
    ) -> JournalWriterGrant:
        self._require_head_scope(expected_head, partition_id)
        document = self._request(
            "POST",
            f"{self._partition_url(partition_id)}/lease",
            headers=self._write_headers(),
            json={
                "format_version": self._format_version,
                "expected_head_sequence": expected_head.sequence,
                "expected_head_record_digest": expected_head.record_digest,
            },
            conflict_capable=True,
        )
        lease = document.get("writer_lease")
        token = document.get("fencing_token")
        if not isinstance(lease, dict) or not isinstance(token, str) or not token:
            raise RemoteJournalVerificationError(
                "writer response omitted its metadata or fencing token"
            )
        if (
            lease.get("journal_stream_id") != self._home_stream_id
            or lease.get("partition_id") != partition_id
            or lease.get("status") != "active"
            or lease.get("expected_head_sequence") != expected_head.sequence
            or lease.get("expected_head_record_digest") != expected_head.record_digest
        ):
            raise RemoteJournalVerificationError(
                "writer response substituted its stream, partition, state, or expected head"
            )
        generation = lease.get("lease_generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise RemoteJournalVerificationError("writer response has no usable generation")
        return JournalWriterGrant(
            partition_id=partition_id,
            generation=generation,
            fencing_token=token,
        )

    def append(
        self,
        partition_id: str,
        *,
        content: Mapping[str, object],
        expected_head: JournalPartitionHeadV1,
        fencing_token: str,
    ) -> JournalAppendOutcome:
        self._require_head_scope(expected_head, partition_id)
        normalized_content = _append_content(content)
        idempotency_key = _append_idempotency_key(
            identity=self.identity,
            partition_id=partition_id,
            expected_head=expected_head,
            content=normalized_content,
        )
        document = self._request(
            "POST",
            f"{self._partition_url(partition_id)}/records",
            headers=self._write_headers(fencing_token),
            json={
                "format_version": self._format_version,
                "content": normalized_content,
                "idempotency_key": idempotency_key,
                "expected_head_sequence": expected_head.sequence,
                "expected_head_record_digest": expected_head.record_digest,
            },
            conflict_capable=True,
        )
        stored = _model(StoredProcedureJournalRecordV1, document.get("record"), "record")
        head = _model(JournalPartitionHeadV1, document.get("head"), "head")
        self._require_head_scope(head, partition_id)
        if stored.record.stream != self.identity or stored.record.partition_id != partition_id:
            raise RemoteJournalVerificationError(
                "append response record substituted stream or partition identity"
            )
        if head.record_digest != stored.record_digest or head.sequence != stored.record.sequence:
            raise RemoteJournalVerificationError(
                "append response head does not commit its returned record"
            )
        replayed = document.get("replayed")
        operation_id = document.get("operation_id")
        if not isinstance(replayed, bool) or not isinstance(operation_id, str) or not operation_id:
            raise RemoteJournalVerificationError(
                "append response omitted replay or operation metadata"
            )
        if (
            stored.record.previous_record_digest != expected_head.record_digest
            or stored.record.sequence != expected_head.sequence + 1
        ):
            raise RemoteJournalVerificationError(
                "append response did not extend the exact expected head"
            )
        _verify_returned_append_content(stored, normalized_content)
        return JournalAppendOutcome(
            record=stored,
            head=head,
            replayed=replayed,
            operation_id=operation_id,
        )

    def fence_writer(
        self,
        partition_id: str,
        *,
        fencing_token: str,
        expected_generation: int | None = None,
    ) -> None:
        if expected_generation is not None and expected_generation < 1:
            raise RemoteJournalVerificationError(
                "expected writer generation must be positive when supplied"
            )
        document = self._request(
            "POST",
            f"{self._partition_url(partition_id)}/lease/fence",
            headers=self._write_headers(fencing_token),
            json={
                "format_version": self._format_version,
                "expected_generation": expected_generation,
            },
            conflict_capable=True,
        )
        lease = document.get("writer_lease")
        if not isinstance(lease, dict):
            raise RemoteJournalVerificationError("fence response has no writer metadata")
        if (
            lease.get("journal_stream_id") != self._home_stream_id
            or lease.get("partition_id") != partition_id
            or lease.get("status") != "fenced"
            or (
                expected_generation is not None
                and lease.get("lease_generation") != expected_generation
            )
        ):
            raise RemoteJournalVerificationError(
                "fence response substituted its stream, partition, generation, or state"
            )

    def export(self, partition_ids: Sequence[str]) -> JournalTransfer:
        requested = self._partition_ids(partition_ids)
        document = self._request(
            "POST",
            f"{self._stream_url}/export",
            headers=self._read_headers,
            json={
                "format_version": self._format_version,
                "partition_ids": list(requested),
            },
        )
        return self._transfer(document, requested_partitions=requested)

    def import_transfer(
        self,
        transfer: JournalTransfer,
    ) -> tuple[JournalPartitionHeadV1, ...]:
        bundle = self._verify_transfer(transfer)
        document = self._request(
            "POST",
            f"{self._stream_url}/import",
            headers=self._read_headers,
            json={
                "format_version": self._format_version,
                "payload_base64": base64.b64encode(transfer.payload).decode("ascii"),
                "expected_head_public_key": transfer.expected_head_public_key,
            },
            conflict_capable=True,
        )
        raw_heads = document.get("imported_heads")
        if not isinstance(raw_heads, list):
            raise RemoteJournalVerificationError("import response has no imported-head list")
        heads = tuple(
            _model(JournalPartitionHeadV1, item, f"imported_heads[{index}]")
            for index, item in enumerate(raw_heads)
        )
        expected = bundle.manifest.head_manifest.statement.head_vector.partitions
        if heads != expected:
            raise RemoteJournalVerificationError(
                "import response does not reproduce the transferred heads"
            )
        return heads

    def head_proof(self, partition_ids: Sequence[str]) -> JournalHeadProof:
        requested = self._partition_ids(partition_ids)
        document = self._request(
            "POST",
            f"{self._stream_url}/handoff/begin",
            headers=self._read_headers,
            json={
                "format_version": self._format_version,
                "partition_ids": list(requested),
            },
        )
        transfer = self._transfer(document, requested_partitions=requested)
        moving = document.get("moving_partitions")
        expected_moving = [
            head.partition_id for head in transfer.head_vector.partitions if head.sequence > 0
        ]
        if not _same_partition_set(moving, expected_moving):
            raise RemoteJournalVerificationError("handoff proof changed its moving partition set")
        return JournalHeadProof(
            manifest=transfer.head_manifest,
            expected_public_key=transfer.expected_head_public_key,
        )

    def complete_handoff(
        self,
        *,
        target_proof: JournalHeadProof,
        source_fencing_tokens: Mapping[str, str],
        partition_ids: Sequence[str],
    ) -> None:
        requested = self._partition_ids(partition_ids)
        try:
            proved = target_proof.verify()
        except (PlaybillJournalError, ValueError) as exc:
            raise RemoteJournalVerificationError(
                "handoff target head proof does not verify"
            ) from exc
        if any(head.stream != self.identity for head in proved.partitions) or {
            head.partition_id for head in proved.partitions
        } != set(requested):
            raise RemoteJournalVerificationError(
                "handoff target proof names another stream or partition set"
            )
        if set(source_fencing_tokens) != set(requested) or any(
            not value for value in source_fencing_tokens.values()
        ):
            raise RemoteJournalVerificationError(
                "handoff must supply one nonblank fence for every requested partition"
            )
        document = self._request(
            "POST",
            f"{self._stream_url}/handoff/complete",
            headers=self._read_headers,
            json={
                "format_version": self._format_version,
                "target_head_manifest": target_proof.manifest.model_dump(mode="json"),
                "target_head_public_key": target_proof.expected_public_key,
                "source_fencing_tokens": dict(source_fencing_tokens),
                "partition_ids": list(requested),
            },
            conflict_capable=True,
        )
        released = document.get("released_partitions")
        fenced = document.get("fenced_leases")
        if (
            not _same_partition_set(released, requested)
            or document.get("export_remains_available") is not True
        ):
            raise RemoteJournalVerificationError(
                "handoff completion did not release every requested partition"
            )
        if not isinstance(fenced, list) or len(fenced) != len(requested):
            raise RemoteJournalVerificationError(
                "handoff completion omitted fenced-writer evidence"
            )
        fenced_partitions: list[str] = []
        for item in fenced:
            if (
                not isinstance(item, dict)
                or item.get("journal_stream_id") != self._home_stream_id
                or not isinstance(item.get("partition_id"), str)
                or item.get("status") != "fenced"
            ):
                raise RemoteJournalVerificationError(
                    "handoff completion returned substituted fenced-writer evidence"
                )
            fenced_partitions.append(cast(str, item["partition_id"]))
        if not _same_partition_set(fenced_partitions, requested):
            raise RemoteJournalVerificationError(
                "handoff completion fenced a different partition set"
            )

    def _transfer(
        self,
        document: Mapping[str, object],
        *,
        requested_partitions: tuple[str, ...],
    ) -> JournalTransfer:
        export = document.get("export")
        if not isinstance(export, dict):
            raise RemoteJournalVerificationError("export response has no export object")
        encoded = export.get("payload_base64")
        if not isinstance(encoded, str):
            raise RemoteJournalVerificationError("export response has no base64 payload")
        try:
            payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise RemoteJournalVerificationError(
                "export response payload is not canonical base64"
            ) from exc
        if export.get("byte_length") != len(payload):
            raise RemoteJournalVerificationError("export response byte length does not reproduce")
        manifest = _model(
            JournalHeadManifestV1,
            document.get("head_manifest"),
            "head_manifest",
        )
        expected_key = document.get("expected_head_public_key")
        segment_count = export.get("segment_count")
        record_count = export.get("record_count")
        if not isinstance(expected_key, str):
            raise RemoteJournalVerificationError("export response omitted its head public key")
        if (
            isinstance(segment_count, bool)
            or not isinstance(segment_count, int)
            or isinstance(record_count, bool)
            or not isinstance(record_count, int)
        ):
            raise RemoteJournalVerificationError("export response omitted its counts")
        try:
            transfer = JournalTransfer(
                payload=payload,
                head_manifest=manifest,
                expected_head_public_key=expected_key,
                segment_count=segment_count,
                record_count=record_count,
            )
        except ValueError as exc:
            raise RemoteJournalVerificationError("export response counts are invalid") from exc
        bundle = self._verify_transfer(transfer)
        if {head.partition_id for head in transfer.head_vector.partitions} != set(
            requested_partitions
        ):
            raise RemoteJournalVerificationError(
                "export response substituted its requested partition set"
            )
        if segment_count != len(bundle.manifest.segments) or record_count != sum(
            item.record_count for item in bundle.manifest.segments
        ):
            raise RemoteJournalVerificationError(
                "export response counts do not reproduce its normalized bundle"
            )
        return transfer

    def _verify_transfer(self, transfer: JournalTransfer) -> JournalExportBundleV1:
        try:
            bundle = parse_journal_export(transfer.payload)
            verify_journal_head_manifest(
                transfer.head_manifest,
                expected_public_key=transfer.expected_head_public_key,
            )
        except (PlaybillJournalError, ValueError) as exc:
            raise RemoteJournalVerificationError(
                "normalized journal transfer failed bundle or head-proof verification"
            ) from exc
        if bundle.manifest.head_manifest != transfer.head_manifest:
            raise RemoteJournalVerificationError(
                "transfer bytes and metadata name different head proofs"
            )
        if any(
            head.stream != self.identity
            for head in bundle.manifest.head_manifest.statement.head_vector.partitions
        ):
            raise RemoteJournalVerificationError(
                "journal transfer substituted its logical stream identity"
            )
        if transfer.segment_count != len(bundle.manifest.segments) or transfer.record_count != sum(
            item.record_count for item in bundle.manifest.segments
        ):
            raise RemoteJournalVerificationError(
                "journal transfer metadata does not reproduce its normalized bundle"
            )
        return bundle

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, int] | None = None,
        json: Mapping[str, object] | None = None,
        conflict_capable: bool = False,
    ) -> dict[str, object]:
        response = self._send(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
        )
        return self._response_document(response, conflict_capable=conflict_capable)

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, int] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise RemoteJournalTransportError("remote journal transport failed") from exc

    def _response_document(
        self,
        response: httpx.Response,
        *,
        conflict_capable: bool,
    ) -> dict[str, object]:
        try:
            raw: object = response.json()
        except ValueError as exc:
            if not 200 <= response.status_code < 300:
                raise RemoteJournalRefusal(
                    remote_status=response.status_code,
                    refusal_id=None,
                ) from exc
            raise RemoteJournalVerificationError("successful journal response is not JSON") from exc
        if not isinstance(raw, dict):
            if not 200 <= response.status_code < 300:
                raise RemoteJournalRefusal(
                    remote_status=response.status_code,
                    refusal_id=None,
                )
            raise RemoteJournalVerificationError("successful journal response is not an object")
        document = cast(dict[str, object], raw)
        if not 200 <= response.status_code < 300:
            raw_refusal_id = document.get("error_code")
            refusal_id = raw_refusal_id if isinstance(raw_refusal_id, str) else None
            refusal_type = (
                RemoteJournalConflict
                if conflict_capable and refusal_id in _CONFLICT_REFUSAL_IDS
                else RemoteJournalRefusal
            )
            raise refusal_type(
                remote_status=response.status_code,
                refusal_id=refusal_id,
            )
        if document.get("format_version") != self._format_version:
            raise RemoteJournalVerificationError(
                "journal response changed its configured format version"
            )
        return document

    def _require_head_scope(
        self,
        head: JournalPartitionHeadV1,
        partition_id: str,
    ) -> None:
        self._validate_partition_id(partition_id)
        if head.stream != self.identity or head.partition_id != partition_id:
            raise RemoteJournalVerificationError(
                "journal head substituted stream or partition identity"
            )

    def _validate_partition_id(self, partition_id: str) -> None:
        try:
            journal_genesis_digest(self.identity, partition_id)
        except ValueError as exc:
            raise RemoteJournalVerificationError(
                "journal partition is not a canonical identifier"
            ) from exc

    def _partition_ids(
        self,
        partition_ids: Sequence[str],
        *,
        allow_empty: bool = False,
    ) -> tuple[str, ...]:
        requested = tuple(partition_ids)
        if not requested and not allow_empty:
            raise RemoteJournalVerificationError(
                "journal operation requires at least one partition"
            )
        for partition_id in requested:
            self._validate_partition_id(partition_id)
        if len(set(requested)) != len(requested):
            raise RemoteJournalVerificationError("journal operation repeats a partition coordinate")
        return requested


def _append_content(content: Mapping[str, object]) -> dict[str, object]:
    try:
        normalized = normalize_canonical(dict(content), location="$.content")
    except (CanonicalEncodingError, ValueError) as exc:
        raise RemoteJournalVerificationError(
            "append content is not in the canonical journal value set"
        ) from exc
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping input guarantees an object
        raise RemoteJournalVerificationError("append content must be a journal object")
    refused = sorted(set(normalized) & _HOME_ASSIGNED_APPEND_FIELDS)
    if refused:
        raise RemoteJournalVerificationError(
            f"append content supplies home-assigned fields: {refused}"
        )
    return cast(dict[str, object], normalized)


def _append_idempotency_key(
    *,
    identity: JournalStreamIdentityV1,
    partition_id: str,
    expected_head: JournalPartitionHeadV1,
    content: Mapping[str, object],
) -> str:
    return typed_digest(
        ArtifactDigest,
        _IDEMPOTENCY_DOMAIN,
        {
            "stream": identity.model_dump(mode="json"),
            "partition_id": partition_id,
            "expected_head": expected_head.model_dump(mode="json"),
            "content": content,
        },
    ).tagged


def _verify_returned_append_content(
    stored: StoredProcedureJournalRecordV1,
    content: Mapping[str, object],
) -> None:
    document = {
        **dict(content),
        "stream": stored.record.stream.model_dump(mode="json"),
        "partition_id": stored.record.partition_id,
        "actor_context": stored.record.actor_context.model_dump(mode="json", exclude_none=True),
    }
    try:
        draft = ProcedureJournalRecordDraftV1.model_validate(document)
        expected = ProcedureJournalRecordV1.bind(
            draft,
            sequence=stored.record.sequence,
            previous_record_digest=stored.record.previous_record_digest,
        )
    except ValueError as exc:
        raise RemoteJournalVerificationError(
            "append response cannot reproduce the requested content"
        ) from exc
    if expected != stored.record:
        raise RemoteJournalVerificationError(
            "append response changed caller-supplied record content"
        )


def _model(model: type[ModelT], raw: object, field_name: str) -> ModelT:
    try:
        return model.model_validate(raw)
    except ValueError as exc:
        raise RemoteJournalVerificationError(
            f"journal response field {field_name!r} is malformed"
        ) from exc


def _nonblank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} cannot be blank")
    return value


def _same_partition_set(raw: object, expected: Sequence[str]) -> bool:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return False
    return len(raw) == len(expected) and len(set(raw)) == len(raw) and set(raw) == set(expected)


def _write_header_extensions(headers: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(headers or {})
    reserved = {"authorization", _FENCING_TOKEN_HEADER.lower()}
    for name, value in result.items():
        if not isinstance(name, str) or not name or name.lower() in reserved:
            raise ValueError("write header extensions cannot replace protocol headers")
        if not isinstance(value, str) or not value:
            raise ValueError("write header extension values cannot be blank")
    return result


def _narrowest_coverage(states: tuple[JournalCoverageState, ...]) -> JournalCoverageState:
    order = {
        JournalCoverageState.EXACT: 0,
        JournalCoverageState.TRUNCATED: 1,
        JournalCoverageState.EXPIRED: 2,
        JournalCoverageState.UNAVAILABLE: 3,
        JournalCoverageState.UNAUTHORIZED: 4,
    }
    return max(states, key=order.__getitem__) if states else JournalCoverageState.UNAVAILABLE


__all__ = [
    "GovernedJournalClientProtocol",
    "HttpGovernedJournalClient",
    "JournalAppendOutcome",
    "JournalCoverage",
    "JournalCoverageState",
    "JournalHeadProof",
    "JournalTransfer",
    "JournalWriterGrant",
    "RemoteJournalConflict",
    "RemoteJournalError",
    "RemoteJournalRefusal",
    "RemoteJournalTransportError",
    "RemoteJournalVerificationError",
]
