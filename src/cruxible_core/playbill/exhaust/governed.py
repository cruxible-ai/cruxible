"""Open governed-journal client contracts and remote refusal types.

A governed home sits above the journal storage seam: callers describe an event,
while the home assigns chain coordinates and authenticated actor attribution.
The contracts here therefore do not implement ``JournalBackendProtocol`` and do
not accept a fully bound journal-record draft.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust.records import (
    JournalHeadManifestV1,
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
    verify_journal_head_manifest,
)


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
        if self.generation < 1:
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
        if self.segment_count < 1 or self.record_count < 1:
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


__all__ = [
    "GovernedJournalClientProtocol",
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
