"""Service-owned read-touch receipts for accepted Cruxible artifacts.

Raw receipts are kept for the retention window (``RetentionSettings.days``).
Older ones fold into counters, the durable record: each fold event keeps, per
artifact, its touches by operation and the audit's distinct reader keys, and
every receipt id it folded, so a read served again after its receipt folded is
never counted twice (an ``--at`` read of a folded coordinate included). Once a
fold records them, the raw receipts are removed. Nothing signed reads a
receipt; the dead-vocabulary detector and audit ranking read the counters and
the raw receipts not yet folded.
"""

from __future__ import annotations

import fcntl
import heapq
import os
import secrets
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, TypeAlias, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.claims.closure import parse_dependency_artifact
from cruxible_core.curation.review_operational import (
    REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT,
    PlaybillReviewOperationalEventV1,
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStore,
    ReviewOperationalStoreError,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance

# Read-touch receipts feed curation only. A local daemon records none, so a
# read never writes; a managed deployment sets ``on``.
CONSUMPTION_RECEIPTS_ENV = "CRUXIBLE_CONSUMPTION_RECEIPTS"


def consumption_receipts_enabled() -> bool:
    value = os.environ.get(CONSUMPTION_RECEIPTS_ENV, "off").strip().lower()
    if value not in {"off", "on"}:
        raise FormatError(f"{CONSUMPTION_RECEIPTS_ENV} must be 'off' or 'on', not {value!r}")
    return value == "on"


CONSUMPTION_RECEIPT_ID_DOMAIN = "playbill-consumption-receipt-v1"
# Keep the epoch's original locator; only new receipts get singleton partitions.
CONSUMPTION_EPOCH_PARTITION_ID = "receipts"

ConsumptionOperation: TypeAlias = Literal[
    "playbill.claim.get",
    "playbill.claim_type.get",
    "playbill.coverage.resolve",
    "playbill.discover.match",
    "playbill.expand",
    "playbill.procedure.run.resolve",
    "playbill.query.run",
    "playbill.query_definition.get",
    "playbill.search.match",
    "playbill.subject.get",
]

QUALIFYING_CONSUMPTION_OPERATIONS: tuple[ConsumptionOperation, ...] = (
    "playbill.claim.get",
    "playbill.claim_type.get",
    "playbill.coverage.resolve",
    "playbill.discover.match",
    "playbill.expand",
    "playbill.procedure.run.resolve",
    "playbill.query.run",
    "playbill.query_definition.get",
    "playbill.search.match",
    "playbill.subject.get",
)


class _StrictConsumptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConsumptionContextV1(_StrictConsumptionModel):
    """Credential-derived context supplied only by the outer public boundary."""

    actor_context: GovernedActorContext
    access_profile_id: str = Field(min_length=1, max_length=256)


class ConsumptionEpochV1(_StrictConsumptionModel):
    tag: Literal["playbill-consumption-epoch-v1"] = "playbill-consumption-epoch-v1"
    event_id: Literal["consumption-epoch"] = "consumption-epoch"
    consumption_epoch_generation: int = Field(ge=0)
    accepted_coordinate: AcceptedCoordinate


class ConsumptionObservationGapV1(_StrictConsumptionModel):
    """Reads were served without receipts from this generation on."""

    tag: Literal["playbill-consumption-gap-v1"] = "playbill-consumption-gap-v1"
    event_id: str
    unobserved_from_generation: int = Field(ge=0)
    accepted_coordinate: AcceptedCoordinate

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ConsumptionObservationResumeV1(_StrictConsumptionModel):
    """Receipts are recorded again from this generation on, closing the open gap."""

    tag: Literal["playbill-consumption-resume-v1"] = "playbill-consumption-resume-v1"
    event_id: str
    observed_from_generation: int = Field(ge=0)
    accepted_coordinate: AcceptedCoordinate

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


# Gaps and resumptions alternate in one ordered partition; its last event says
# whether observation is currently open.
CONSUMPTION_OBSERVATION_PARTITION_ID = "observation"


class ConsumptionReceiptV1(_StrictConsumptionModel):
    tag: Literal["playbill-consumption-receipt-v1"] = "playbill-consumption-receipt-v1"
    event_id: str
    receipt_id: str
    reader_principal_id: str = Field(min_length=1, max_length=256)
    access_profile_id: str = Field(min_length=1, max_length=256)
    operation: ConsumptionOperation
    accepted_coordinate: AcceptedCoordinate
    response_artifact_identity: ArtifactIdentity
    response_artifact_digest: str

    @field_validator("event_id", "receipt_id", "response_artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> ConsumptionReceiptV1:
        if self.event_id != self.receipt_id:
            raise ValueError("consumption event and receipt identities differ")
        if self.receipt_id != consumption_receipt_id(self):
            raise ValueError("consumption receipt ID does not reproduce")
        return self


class ConsumptionArtifactAggregateV1(_StrictConsumptionModel):
    artifact_identity: ArtifactIdentity
    total_touch_count: int = Field(ge=0)
    qualifying_touch_count: int = Field(ge=0)
    touches_by_operation: tuple[tuple[ConsumptionOperation, int], ...]


class ConsumptionAggregateV1(_StrictConsumptionModel):
    tag: Literal["playbill-consumption-aggregate-v1"] = "playbill-consumption-aggregate-v1"
    initialized: bool
    consumption_epoch_generation: int | None = Field(default=None, ge=0)
    artifacts: tuple[ConsumptionArtifactAggregateV1, ...]
    # A period served without receipts that no later recording has closed:
    # zero touches say nothing about it.
    observation_gap_open: bool = False
    # Where observation last resumed after a gap; zero-use counts from here.
    observed_since_generation: int | None = Field(default=None, ge=0)


# Folds chain in one partition; each holds at most this many receipts.
CONSUMPTION_FOLD_PARTITION_ID = "folds"
CONSUMPTION_FOLD_BATCH = 4096


class ConsumptionFoldedArtifactV1(_StrictConsumptionModel):
    """One artifact's folded touches: counts by operation and the audit's reader keys."""

    artifact_identity: ArtifactIdentity
    touches_by_operation: tuple[tuple[ConsumptionOperation, int], ...]
    # Digests of the distinct (reader, coordinate, artifact, digest) keys among
    # the folded qualifying touches (``consumption_audit_key``).
    audit_keys: tuple[str, ...]


class ConsumptionFoldV1(_StrictConsumptionModel):
    """Receipts older than the retention window, folded into counters that outlive them."""

    tag: Literal["playbill-consumption-fold-v1"] = "playbill-consumption-fold-v1"
    event_id: str
    folded_before: datetime
    # Every receipt this fold counted, so the same receipt is never counted again.
    receipt_ids: tuple[str, ...]
    artifacts: tuple[ConsumptionFoldedArtifactV1, ...]

    @field_validator("folded_before")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _reproduces(self) -> ConsumptionFoldV1:
        if list(self.receipt_ids) != sorted(set(self.receipt_ids)):
            raise ValueError("consumption fold receipt ids must be sorted and unique")
        if self.event_id != consumption_fold_id(self.receipt_ids):
            raise ValueError("consumption fold ID does not reproduce")
        return self


def consumption_fold_id(receipt_ids: Iterable[str]) -> str:
    return typed_digest(
        Sha256Value, "playbill-consumption-fold-v1", {"receipt_ids": sorted(receipt_ids)}
    ).tagged


def consumption_audit_key(receipt: ConsumptionReceiptV1) -> str:
    """The audit's demand key: one reader at one coordinate touching one artifact digest.

    Operation and access profile are absent, so one reader cannot heat an
    artifact by trying more verbs or profiles.
    """

    return typed_digest(
        Sha256Value,
        "playbill-consumption-audit-key-v1",
        {
            "reader_principal_id": receipt.reader_principal_id,
            "accepted_coordinate": receipt.accepted_coordinate.model_dump(mode="json"),
            "response_artifact_identity": receipt.response_artifact_identity.model_dump(
                mode="json"
            ),
            "response_artifact_digest": receipt.response_artifact_digest,
        },
    ).tagged


def consumption_receipt_id(receipt: ConsumptionReceiptV1) -> str:
    return typed_digest(
        Sha256Value,
        CONSUMPTION_RECEIPT_ID_DOMAIN,
        {
            "reader_principal_id": receipt.reader_principal_id,
            "access_profile_id": receipt.access_profile_id,
            "operation": receipt.operation,
            "accepted_coordinate": receipt.accepted_coordinate.model_dump(mode="json"),
            "response_artifact_identity": receipt.response_artifact_identity.model_dump(
                mode="json"
            ),
            "response_artifact_digest": receipt.response_artifact_digest,
        },
    ).tagged


def build_consumption_receipt(
    *,
    context: ConsumptionContextV1,
    operation: ConsumptionOperation,
    coordinate: AcceptedCoordinate,
    artifact_identity: ArtifactIdentity,
    artifact_digest: str,
) -> ConsumptionReceiptV1:
    placeholder = "sha256:" + "0" * 64
    draft = ConsumptionReceiptV1.model_construct(
        tag="playbill-consumption-receipt-v1",
        event_id=placeholder,
        receipt_id=placeholder,
        reader_principal_id=context.actor_context.actor_id,
        access_profile_id=context.access_profile_id,
        operation=operation,
        accepted_coordinate=coordinate,
        response_artifact_identity=artifact_identity,
        response_artifact_digest=artifact_digest,
    )
    identity = consumption_receipt_id(draft)
    return ConsumptionReceiptV1(
        event_id=identity,
        receipt_id=identity,
        reader_principal_id=context.actor_context.actor_id,
        access_profile_id=context.access_profile_id,
        operation=operation,
        accepted_coordinate=coordinate,
        response_artifact_identity=artifact_identity,
        response_artifact_digest=artifact_digest,
    )


def _generation(instance: PlaybillInstance, coordinate: AcceptedCoordinate) -> int:
    try:
        with instance.accepted_history_reader(at=coordinate) as history:
            return history.sequence
    except FormatError as exc:
        raise ReviewOperationalStoreError(
            "consumption receipt coordinate is not accepted by this instance"
        ) from exc


def record_consumption(
    instance: PlaybillInstance,
    *,
    context: ConsumptionContextV1 | None,
    operation: ConsumptionOperation,
    coordinate: AcceptedCoordinate,
    artifacts: Iterable[tuple[ArtifactIdentity, str]],
) -> tuple[ConsumptionReceiptV1, ...]:
    """Append one idempotent touch per distinct served artifact after success."""

    if context is None:
        return ()
    if not consumption_receipts_enabled():
        note_consumption_unobserved(instance, context=context, coordinate=coordinate)
        return ()
    ordered = tuple(
        sorted(
            set(artifacts),
            key=lambda item: (item[0].qualified.encode("utf-8"), item[1].encode("ascii")),
        )
    )
    if not ordered:
        return ()
    generation = _generation(instance, coordinate)
    store = instance.review_operational_store()
    ensure_consumption_epoch(
        instance,
        coordinate=coordinate,
        generation=generation,
        actor_context=context.actor_context,
    )
    _resume_observation(instance, context=context)
    receipts = tuple(
        build_consumption_receipt(
            context=context,
            operation=operation,
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=digest,
        )
        for identity, digest in ordered
    )
    for offset in range(0, len(receipts), REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT):
        store.append_batch(
            family="consumption",
            entries=tuple(
                (receipt.receipt_id, receipt)
                for receipt in receipts[offset : offset + REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT]
            ),
            coordinate=coordinate,
            generation=generation,
            actor_context=context.actor_context,
            recorded_at=context.actor_context.timestamp,
        )
    return receipts


# Each process checks each instance's observation state once per direction.
_OBSERVATION_CHECKED: set[tuple[str, bool]] = set()
_OBSERVATION_LOCK = threading.Lock()


def _first_check(instance: PlaybillInstance, *, recording: bool) -> bool:
    key = (str(instance.root), recording)
    with _OBSERVATION_LOCK:
        if key in _OBSERVATION_CHECKED:
            return False
        _OBSERVATION_CHECKED.add(key)
        return True


def note_consumption_unobserved(
    instance: PlaybillInstance,
    *,
    context: ConsumptionContextV1 | None,
    coordinate: AcceptedCoordinate,
) -> None:
    """Mark that reads go unrecorded, once, on an instance that has been observing.

    An instance that never recorded receipts has no epoch and is left
    untouched. One that has gets a gap, so a later recording cannot read the
    unrecorded period as zero use. Only the epoch's first event and the small
    observation partition are read.
    """
    if context is None or not _first_check(instance, recording=False):
        return
    store = instance.review_operational_store()
    if (
        store.first_payload(family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID)
        is None
    ):
        return
    if _observation_open_gap(store):
        return
    coordinate, generation = _observation_head(instance)
    gap = ConsumptionObservationGapV1(
        event_id=typed_digest(
            Sha256Value, "playbill-consumption-gap-v1", {"token": secrets.token_hex(16)}
        ).tagged,
        unobserved_from_generation=generation,
        accepted_coordinate=coordinate,
    )
    store.append(
        family="consumption",
        partition_id=CONSUMPTION_OBSERVATION_PARTITION_ID,
        event_id=gap.event_id,
        payload=gap,
        coordinate=coordinate,
        generation=generation,
        actor_context=context.actor_context,
        recorded_at=context.actor_context.timestamp,
    )


def _observation_head(instance: PlaybillInstance) -> tuple[AcceptedCoordinate, int]:
    """The current accepted head and its generation.

    Observation starts and stops now, whatever historical coordinate the read
    that notices it happens to serve; receipts keep the coordinate they read.
    """
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return head, _generation(instance, head)


def _resume_observation(instance: PlaybillInstance, *, context: ConsumptionContextV1) -> None:
    if not _first_check(instance, recording=True):
        return
    store = instance.review_operational_store()
    if not _observation_open_gap(store):
        return
    coordinate, generation = _observation_head(instance)
    resume = ConsumptionObservationResumeV1(
        event_id=typed_digest(
            Sha256Value, "playbill-consumption-resume-v1", {"token": secrets.token_hex(16)}
        ).tagged,
        observed_from_generation=generation,
        accepted_coordinate=coordinate,
    )
    store.append(
        family="consumption",
        partition_id=CONSUMPTION_OBSERVATION_PARTITION_ID,
        event_id=resume.event_id,
        payload=resume,
        coordinate=coordinate,
        generation=generation,
        actor_context=context.actor_context,
        recorded_at=context.actor_context.timestamp,
    )


def _observation(store: ReviewOperationalStore) -> tuple[bool, int | None]:
    """(a gap is open, the generation observation last resumed from)."""
    open_gap = False
    resumed: int | None = None
    for payload in store.partition_payloads(
        family="consumption", partition_id=CONSUMPTION_OBSERVATION_PARTITION_ID
    ):
        if payload.get("tag") == "playbill-consumption-gap-v1":
            ConsumptionObservationGapV1.model_validate(payload)
            open_gap = True
        elif payload.get("tag") == "playbill-consumption-resume-v1":
            resumed = ConsumptionObservationResumeV1.model_validate(
                payload
            ).observed_from_generation
            open_gap = False
        else:
            raise ReviewOperationalStoreError("consumption observation has an unknown payload")
    return open_gap, resumed


def _observation_open_gap(store: ReviewOperationalStore) -> bool:
    return _observation(store)[0]


def ensure_consumption_epoch(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedCoordinate,
    generation: int,
    actor_context: GovernedActorContext,
) -> ConsumptionEpochV1:
    """Initialize the dead-vocabulary observation epoch without a fake touch."""

    epoch = ConsumptionEpochV1(
        consumption_epoch_generation=generation,
        accepted_coordinate=coordinate,
    )
    _event, payload = instance.review_operational_store().ensure_first(
        family="consumption",
        partition_id=CONSUMPTION_EPOCH_PARTITION_ID,
        payload=epoch,
        coordinate=coordinate,
        generation=generation,
        actor_context=actor_context,
        recorded_at=actor_context.timestamp,
    )
    return ConsumptionEpochV1.model_validate(payload)


def consumption_artifacts_for_paths(
    tree: Mapping[str, bytes], paths: Iterable[str]
) -> tuple[tuple[ArtifactIdentity, str], ...]:
    artifacts: list[tuple[ArtifactIdentity, str]] = []
    for path in sorted(set(paths), key=lambda item: item.encode("utf-8")):
        content = tree.get(path)
        if content is None:
            continue
        parsed = parse_dependency_artifact(path, content)
        if parsed is not None:
            artifacts.append((parsed.identity, parsed.artifact_digest))
    return tuple(artifacts)


def consumption_artifacts_for_dependency_closure(
    instance: PlaybillInstance, coordinate: AcceptedCoordinate, root_path: str
) -> tuple[tuple[ArtifactIdentity, str], ...]:
    """Read only the root and the accepted owners reached through its pins."""

    accepted = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    with instance.bind_accepted_projection(accepted) as projection:
        roots = projection.typed.envelopes(paths=(root_path,))
        pending = [row.identity for row in roots]
        visited: set[str] = set()
        result: list[tuple[ArtifactIdentity, str]] = []
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            current = projection.typed.dependency_state(identity)
            if current is None:
                continue
            result.append((current.identity, current.artifact_digest))
            pending.extend(pin.target.qualified for pin in reversed(current.pins))
    return tuple(sorted(result, key=lambda item: item[0].qualified.encode("utf-8")))


@dataclass
class _Folded:
    identity: ArtifactIdentity
    operations: Counter[str] = field(default_factory=Counter)
    audit_keys: set[str] = field(default_factory=set)


_PARSED: dict[str, BaseModel] = {}
_PARSED_LOCK = threading.Lock()
_PARSED_LIMIT = 1 << 17


def _parsed(event: PlaybillReviewOperationalEventV1, payload: dict[str, object]) -> BaseModel:
    """Each verified payload parsed once per process, by its immutable event digest."""

    with _PARSED_LOCK:
        known = _PARSED.get(event.event_digest)
    if known is not None:
        return known
    tag = payload.get("tag")
    model: type[BaseModel]
    if tag == "playbill-consumption-epoch-v1":
        model = ConsumptionEpochV1
    elif tag == "playbill-consumption-receipt-v1":
        model = ConsumptionReceiptV1
    elif tag == "playbill-consumption-fold-v1":
        model = ConsumptionFoldV1
    else:
        raise ReviewOperationalStoreError("consumption partition has an unknown payload")
    parsed = model.model_validate(payload)
    with _PARSED_LOCK:
        if len(_PARSED) >= _PARSED_LIMIT:
            _PARSED.clear()
        _PARSED[event.event_digest] = parsed
    return parsed


@dataclass
class _Raw:
    """One unfolded receipt: where it is stored, and the event that first recorded it."""

    receipt: ConsumptionReceiptV1
    event: PlaybillReviewOperationalEventV1
    partitions: set[str] = field(default_factory=set)


@dataclass
class _Tally:
    """The consumption family as maintained counters, advanced from the store's change feed.

    ``operations`` and ``audit`` count folded and unfolded receipts alike:
    a fold moves a raw receipt's contribution into its counters, so no
    reader ever sums the two separately. Audit keys are a multiset, so the
    distinct count per artifact (``audit_distinct``) is kept as keys come and go.
    """

    cursor: tuple[int, int] | None = None
    # Partition directory -> how many of its events are applied, and its receipt ids.
    seen: dict[str, int] = field(default_factory=dict)
    held: dict[str, list[str]] = field(default_factory=dict)
    epoch: ConsumptionEpochV1 | None = None
    fold_head: str | None = None
    folded_ids: set[str] = field(default_factory=set)
    raw: dict[str, _Raw] = field(default_factory=dict)
    # Partition directory -> the folded receipt ids it still stores.
    spent: dict[str, set[str]] = field(default_factory=dict)
    # Partition directory -> its partition id, for the receipts a fold removes.
    partition_ids: dict[str, str] = field(default_factory=dict)
    legacy_unfolded: int = 0
    legacy_events: int = 0
    identities: dict[str, ArtifactIdentity] = field(default_factory=dict)
    operations: dict[str, Counter[str]] = field(default_factory=dict)
    audit: dict[str, Counter[str]] = field(default_factory=dict)
    audit_distinct: dict[str, int] = field(default_factory=dict)
    # Unfolded receipts by when they were recorded, oldest first: a heap,
    # whose entries folded or removed since are skipped as they come up. A
    # fold's cutoff pops only the entries before it, into ``overdue``
    # (receipt id -> recorded time) until a fold takes them.
    expiry: list[tuple[datetime, str]] = field(default_factory=list)
    overdue: dict[str, datetime] = field(default_factory=dict)

    def due(self, cutoff: datetime) -> list[str]:
        """The unfolded receipts recorded before ``cutoff``, sorted.

        The work is the receipts that became due since the last call (and
        stale heap entries, each popped once), never the receipts still young.
        """

        while self.expiry and self.expiry[0][0] < cutoff:
            recorded, receipt_id = heapq.heappop(self.expiry)
            if receipt_id in self.raw:
                self.overdue[receipt_id] = recorded
        return sorted(
            receipt_id
            for receipt_id, recorded in self.overdue.items()
            if recorded < cutoff and receipt_id in self.raw
        )

    def _count(self, identity: ArtifactIdentity, operation: str, delta: int) -> None:
        key = identity.qualified
        self.identities.setdefault(key, identity)
        counts = self.operations.setdefault(key, Counter())
        counts[operation] += delta
        if counts[operation] <= 0:
            del counts[operation]
        if not counts:
            del self.operations[key]

    def _key(self, identity: str, audit_key: str, delta: int) -> None:
        keys = self.audit.setdefault(identity, Counter())
        before = keys[audit_key]
        keys[audit_key] = before + delta
        if keys[audit_key] <= 0:
            del keys[audit_key]
        if before <= 0 < before + delta:
            self.audit_distinct[identity] = self.audit_distinct.get(identity, 0) + 1
        elif before > 0 >= before + delta:
            self.audit_distinct[identity] -= 1
            if not self.audit_distinct[identity]:
                del self.audit_distinct[identity]
        if not keys:
            del self.audit[identity]

    def _raw_contribution(self, receipt: ConsumptionReceiptV1, delta: int) -> None:
        self._count(receipt.response_artifact_identity, receipt.operation, delta)
        if receipt.operation in QUALIFYING_CONSUMPTION_OPERATIONS:
            self._key(
                receipt.response_artifact_identity.qualified,
                consumption_audit_key(receipt),
                delta,
            )

    def apply(
        self,
        name: str,
        events: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]],
        start: int,
    ) -> None:
        for event, payload in events[start:]:
            self.partition_ids.setdefault(name, event.partition_id)
            if event.partition_id == CONSUMPTION_OBSERVATION_PARTITION_ID:
                continue  # folded in order by _observation
            parsed = _parsed(event, payload)
            if isinstance(parsed, ConsumptionEpochV1):
                if self.epoch is not None and parsed != self.epoch:
                    raise ReviewOperationalStoreError("consumption epoch is not unique")
                self.epoch = parsed
            elif isinstance(parsed, ConsumptionFoldV1):
                self.fold_head = event.event_digest
                self._fold(parsed)
            else:
                assert isinstance(parsed, ConsumptionReceiptV1)
                self._receipt(name, event, parsed)

    def _fold(self, fold: ConsumptionFoldV1) -> None:
        for receipt_id in fold.receipt_ids:
            if receipt_id in self.folded_ids:
                # Folds are serialized and compared against the folds head, so
                # two can never count one receipt; refuse rather than double.
                raise ReviewOperationalStoreError("a consumption receipt was folded twice")
            self.folded_ids.add(receipt_id)
            self.overdue.pop(receipt_id, None)
            raw = self.raw.pop(receipt_id, None)
            if raw is None:
                continue
            self._raw_contribution(raw.receipt, -1)
            for partition in raw.partitions:
                self.spent.setdefault(partition, set()).add(receipt_id)
                if self.partition_ids.get(partition) == CONSUMPTION_EPOCH_PARTITION_ID:
                    self.legacy_unfolded -= 1
        for item in fold.artifacts:
            key = item.artifact_identity.qualified
            for operation, count in item.touches_by_operation:
                self._count(item.artifact_identity, operation, count)
            for audit_key in item.audit_keys:
                self._key(key, audit_key, 1)

    def _receipt(
        self, name: str, event: PlaybillReviewOperationalEventV1, receipt: ConsumptionReceiptV1
    ) -> None:
        receipt_id = receipt.receipt_id
        self.held.setdefault(name, []).append(receipt_id)
        legacy = event.partition_id == CONSUMPTION_EPOCH_PARTITION_ID
        self.legacy_events += int(legacy)
        if receipt_id in self.folded_ids:
            self.spent.setdefault(name, set()).add(receipt_id)
            return
        raw = self.raw.get(receipt_id)
        if raw is None:
            # A retained chained receipt and a new independent retry represent
            # the same touch. Keep the original bytes, count the stable ID once.
            raw = self.raw[receipt_id] = _Raw(receipt, event)
            self._raw_contribution(receipt, 1)
            heapq.heappush(self.expiry, (event.recorded_at, receipt_id))
        raw.partitions.add(name)
        self.legacy_unfolded += int(legacy)

    def removed(self, name: str) -> None:
        """A partition went away: forget the receipts it stored."""

        for receipt_id in self.held.pop(name, ()):
            self.spent.get(name, set()).discard(receipt_id)
            raw = self.raw.get(receipt_id)
            if raw is not None:
                raw.partitions.discard(name)
                if not raw.partitions:
                    self._raw_contribution(raw.receipt, -1)
                    del self.raw[receipt_id]
                    self.overdue.pop(receipt_id, None)
        self.spent.pop(name, None)
        self.seen.pop(name, None)
        self.partition_ids.pop(name, None)


_TALLIES: dict[tuple[str, str], _Tally] = {}
_TALLIES_LOCK = threading.Lock()


def _tally(store: ReviewOperationalStore) -> _Tally:
    """The store's consumption tally, advanced by what changed since it last was.

    The work is the touches since the last read and the new events of the
    partitions they name; only a first read in a process, a rebuilt replay or
    a partition cut back by retention replays the family whole.
    """

    key = (str(store.root), store.instance_id)
    with _TALLIES_LOCK:
        tally = _TALLIES.setdefault(key, _Tally())
        changes = store.family_changes("consumption", tally.cursor)
        shrank = any(
            events is not None and len(events) < tally.seen.get(name, 0)
            for name, events in changes.partitions
        )
        if changes.full or shrank:
            if shrank:
                changes = store.family_changes("consumption", None)
            tally = _TALLIES[key] = _Tally()
        for name, events in changes.partitions:
            if events is None:
                tally.removed(name)
                continue
            start = tally.seen.get(name, 0)
            tally.apply(name, events, start)
            tally.seen[name] = len(events)
        tally.cursor = changes.cursor
        return tally


def consumption_aggregate(instance: PlaybillInstance) -> ConsumptionAggregateV1:
    """Per-artifact touches, folded and unfolded, from the maintained tally.

    Building the answer is the size of the answer: one row per artifact.
    """

    store = instance.review_operational_store()
    tally = _tally(store)
    gap_open, resumed = _observation(store)
    aggregates: list[ConsumptionArtifactAggregateV1] = []
    with _TALLIES_LOCK:
        for key in sorted(tally.operations, key=lambda item: item.encode("utf-8")):
            counts = tally.operations[key]
            qualifying = tuple(
                (operation, counts[operation])
                for operation in QUALIFYING_CONSUMPTION_OPERATIONS
                if counts[operation]
            )
            aggregates.append(
                ConsumptionArtifactAggregateV1(
                    artifact_identity=tally.identities[key],
                    total_touch_count=sum(counts.values()),
                    qualifying_touch_count=sum(count for _, count in qualifying),
                    touches_by_operation=qualifying,
                )
            )
        epoch = tally.epoch
    return ConsumptionAggregateV1(
        initialized=epoch is not None,
        consumption_epoch_generation=(
            None if epoch is None else epoch.consumption_epoch_generation
        ),
        artifacts=tuple(aggregates),
        observation_gap_open=gap_open,
        observed_since_generation=resumed,
    )


def consumption_audit_touch_counts(instance: PlaybillInstance) -> dict[str, int]:
    """Each artifact's distinct qualifying reader keys, folded and raw, under audit's demand cap."""

    tally = _tally(instance.review_operational_store())
    with _TALLIES_LOCK:
        return {
            identity: tally.audit_distinct[identity]
            for identity in sorted(tally.audit_distinct, key=lambda item: item.encode("utf-8"))
        }


@dataclass(frozen=True)
class ConsumptionFoldResult:
    """What one retention pass did: receipts folded, raw partitions and legacy events removed."""

    folded: int = 0
    removed_partitions: int = 0
    removed_legacy_events: int = 0


_FOLD_LOCKS: dict[str, threading.RLock] = {}
_FOLD_DEPTH: dict[str, int] = {}
_FOLD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _fold_lock(store: ReviewOperationalStore) -> Iterator[None]:
    """One fold at a time per store, across threads and processes (re-entrant in a thread)."""

    key = str(store.root)
    with _FOLD_LOCKS_GUARD:
        lock = _FOLD_LOCKS.setdefault(key, threading.RLock())
    with lock:
        depth = _FOLD_DEPTH.get(key, 0)
        descriptor = None
        if depth == 0:
            descriptor = os.open(
                store.exhaust_root / ".consumption-fold.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        _FOLD_DEPTH[key] = depth + 1
        try:
            yield
        finally:
            _FOLD_DEPTH[key] = depth
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


def _before_fold_append(store: ReviewOperationalStore) -> None:
    """A seam between choosing a fold's receipts and appending it (tests interleave here)."""


def fold_consumption_receipts(
    instance: PlaybillInstance, *, now: datetime, days: int
) -> ConsumptionFoldResult:
    """Fold raw receipts recorded before ``now - days`` into counters, then remove them.

    Folds are serialized per store, and each fold event is appended only if
    the folds partition still ends where the receipts were chosen
    (compare-and-swap); a fold that loses re-reads and chooses again, so no
    receipt is ever counted by two folds. Counters are appended first; only
    receipts a fold durably names are removed: their singleton partitions,
    and the legacy chained ``receipts`` partition cut back to its epoch once
    every receipt in it is folded. A crash in between leaves raw receipts a
    fold already counted, which every reader skips and the next pass removes.
    """

    store = instance.review_operational_store()
    cutoff = ensure_utc(now) - timedelta(days=days)
    folded = 0
    with _fold_lock(store):
        for _attempt in range(8):
            tally = _tally(store)
            with _TALLIES_LOCK:
                # From the tally's expiry heap: only the receipts now past the
                # cutoff are visited, not the window's younger ones.
                due = tally.due(cutoff)
                chosen = {receipt_id: tally.raw[receipt_id].receipt for receipt_id in due}
                expected = tally.fold_head
            if not due:
                break
            _before_fold_append(store)
            try:
                folded += _append_folds(instance, store, chosen, cutoff, now, expected)
            except ReviewOperationalConcurrentChangeError:
                continue  # another fold landed: choose again from what is left
            break
        else:
            raise ReviewOperationalStoreError("consumption folds kept racing; try again")
        tally = _tally(store)
        with _TALLIES_LOCK:
            singletons = sorted(
                tally.partition_ids[name]
                for name, receipts in tally.spent.items()
                if receipts and tally.partition_ids.get(name, "").startswith("event:")
            )
            legacy_done = tally.legacy_events > 0 and tally.legacy_unfolded == 0
        removed = store.remove_partitions(family="consumption", partition_ids=singletons)
        legacy = 0
        first = store.first_payload(
            family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID
        )
        if (
            legacy_done
            and first is not None
            and first.get("tag") == "playbill-consumption-epoch-v1"
        ):
            # Every receipt the legacy chain holds is folded: keep only its epoch.
            legacy = store.truncate_partition(
                family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID, keep=1
            )
    return ConsumptionFoldResult(
        folded=folded, removed_partitions=removed, removed_legacy_events=legacy
    )


def _append_folds(
    instance: PlaybillInstance,
    store: ReviewOperationalStore,
    chosen: Mapping[str, ConsumptionReceiptV1],
    cutoff: datetime,
    now: datetime,
    expected: str | None,
) -> int:
    """Append one fold event per batch, each compared against the folds head before it."""

    due = sorted(chosen)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with instance.accepted_history_reader() as history:
        generation = history.sequence
    actor = GovernedActorContext(
        actor_type="system",
        actor_id="consumption-retention",
        org_id=instance.descriptor.instance_id,
        operation_id=f"consumption.fold:{format_datetime(cutoff)}",
        timestamp=ensure_utc(now),
    )
    for start in range(0, len(due), CONSUMPTION_FOLD_BATCH):
        batch = due[start : start + CONSUMPTION_FOLD_BATCH]
        artifacts: dict[str, _Folded] = {}
        for receipt_id in batch:
            receipt = chosen[receipt_id]
            identity = receipt.response_artifact_identity
            folded = artifacts.setdefault(identity.qualified, _Folded(identity))
            folded.operations[receipt.operation] += 1
            if receipt.operation in QUALIFYING_CONSUMPTION_OPERATIONS:
                folded.audit_keys.add(consumption_audit_key(receipt))
        fold = ConsumptionFoldV1(
            event_id=consumption_fold_id(batch),
            folded_before=cutoff,
            receipt_ids=tuple(batch),
            artifacts=tuple(
                ConsumptionFoldedArtifactV1(
                    artifact_identity=folded.identity,
                    touches_by_operation=tuple(
                        (operation, folded.operations[operation])
                        for operation in get_args(ConsumptionOperation)
                        if folded.operations[operation]
                    ),
                    audit_keys=tuple(sorted(folded.audit_keys)),
                )
                for _key, folded in sorted(
                    artifacts.items(), key=lambda item: item[0].encode("utf-8")
                )
            ),
        )
        event = store.append(
            family="consumption",
            partition_id=CONSUMPTION_FOLD_PARTITION_ID,
            event_id=fold.event_id,
            payload=fold,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor,
            recorded_at=ensure_utc(now),
            expected_latest_event_digest=expected,
        )
        expected = event.event_digest
    return len(due)


__all__ = [
    "CONSUMPTION_FOLD_PARTITION_ID",
    "CONSUMPTION_RECEIPT_ID_DOMAIN",
    "ConsumptionAggregateV1",
    "ConsumptionArtifactAggregateV1",
    "ConsumptionFoldResult",
    "ConsumptionFoldV1",
    "ConsumptionFoldedArtifactV1",
    "ConsumptionContextV1",
    "ConsumptionEpochV1",
    "ConsumptionObservationGapV1",
    "ConsumptionObservationResumeV1",
    "ConsumptionOperation",
    "ConsumptionReceiptV1",
    "QUALIFYING_CONSUMPTION_OPERATIONS",
    "build_consumption_receipt",
    "consumption_aggregate",
    "consumption_audit_key",
    "consumption_audit_touch_counts",
    "consumption_fold_id",
    "fold_consumption_receipts",
    "consumption_artifacts_for_dependency_closure",
    "consumption_artifacts_for_paths",
    "consumption_receipt_id",
    "consumption_receipts_enabled",
    "ensure_consumption_epoch",
    "note_consumption_unobserved",
    "CONSUMPTION_OBSERVATION_PARTITION_ID",
    "record_consumption",
]
