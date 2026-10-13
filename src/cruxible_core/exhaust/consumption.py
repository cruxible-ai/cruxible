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

import os
import secrets
import threading
from collections import Counter
from collections.abc import Iterable, Mapping
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


@dataclass
class _ConsumptionState:
    """The consumption family read once: epoch, folded counters and unfolded raw receipts."""

    epoch: ConsumptionEpochV1 | None = None
    folded_ids: set[str] = field(default_factory=set)
    folded: dict[str, _Folded] = field(default_factory=dict)
    # Unfolded raw receipts by receipt id, with the event that recorded them.
    raw: dict[str, tuple[ConsumptionReceiptV1, PlaybillReviewOperationalEventV1]] = field(
        default_factory=dict
    )
    # Raw receipts a fold already counted, still on disk: (partition, receipt id).
    spent: list[tuple[str, str]] = field(default_factory=list)
    legacy_receipts: int = 0
    folds: int = 0


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


def _consumption_state(store: ReviewOperationalStore) -> _ConsumptionState:
    state = _ConsumptionState()
    receipts: list[tuple[ConsumptionReceiptV1, PlaybillReviewOperationalEventV1]] = []
    for event, payload in store.events(family="consumption"):
        if event.partition_id == CONSUMPTION_OBSERVATION_PARTITION_ID:
            continue  # folded in order by _observation
        parsed = _parsed(event, payload)
        if isinstance(parsed, ConsumptionEpochV1):
            if state.epoch is not None and parsed != state.epoch:
                raise ReviewOperationalStoreError("consumption epoch is not unique")
            state.epoch = parsed
        elif isinstance(parsed, ConsumptionFoldV1):
            state.folds += 1
            state.folded_ids.update(parsed.receipt_ids)
            for item in parsed.artifacts:
                folded = state.folded.setdefault(
                    item.artifact_identity.qualified, _Folded(item.artifact_identity)
                )
                folded.operations.update(dict(item.touches_by_operation))
                folded.audit_keys.update(item.audit_keys)
        else:
            assert isinstance(parsed, ConsumptionReceiptV1)
            if event.partition_id == CONSUMPTION_EPOCH_PARTITION_ID:
                state.legacy_receipts += 1
            receipts.append((parsed, event))
    for receipt, event in receipts:
        if receipt.receipt_id in state.folded_ids:
            state.spent.append((event.partition_id, receipt.receipt_id))
        else:
            # A retained chained receipt and a new independent retry represent
            # the same touch. Keep the original bytes, fold the stable ID once.
            state.raw.setdefault(receipt.receipt_id, (receipt, event))
    return state


def consumption_aggregate(instance: PlaybillInstance) -> ConsumptionAggregateV1:
    store = instance.review_operational_store()
    state = _consumption_state(store)
    gap_open, resumed = _observation(store)
    operations: dict[str, Counter[str]] = {}
    identities: dict[str, ArtifactIdentity] = {}
    for key, folded in state.folded.items():
        identities[key] = folded.identity
        operations.setdefault(key, Counter()).update(folded.operations)
    for receipt, _event in state.raw.values():
        key = receipt.response_artifact_identity.qualified
        identities.setdefault(key, receipt.response_artifact_identity)
        operations.setdefault(key, Counter())[receipt.operation] += 1
    aggregates: list[ConsumptionArtifactAggregateV1] = []
    for key in sorted(operations, key=lambda item: item.encode("utf-8")):
        counts = operations[key]
        qualifying = tuple(
            (operation, counts[operation])
            for operation in QUALIFYING_CONSUMPTION_OPERATIONS
            if counts[operation]
        )
        aggregates.append(
            ConsumptionArtifactAggregateV1(
                artifact_identity=identities[key],
                total_touch_count=sum(counts.values()),
                qualifying_touch_count=sum(count for _, count in qualifying),
                touches_by_operation=qualifying,
            )
        )
    return ConsumptionAggregateV1(
        initialized=state.epoch is not None,
        consumption_epoch_generation=(
            None if state.epoch is None else state.epoch.consumption_epoch_generation
        ),
        artifacts=tuple(aggregates),
        observation_gap_open=gap_open,
        observed_since_generation=resumed,
    )


def consumption_audit_touch_counts(instance: PlaybillInstance) -> dict[str, int]:
    """Each artifact's distinct qualifying reader keys, folded and raw, under audit's demand cap."""

    state = _consumption_state(instance.review_operational_store())
    touches: dict[str, set[str]] = {
        key: set(folded.audit_keys) for key, folded in state.folded.items() if folded.audit_keys
    }
    for receipt, _event in state.raw.values():
        if receipt.operation not in QUALIFYING_CONSUMPTION_OPERATIONS:
            continue
        touches.setdefault(receipt.response_artifact_identity.qualified, set()).add(
            consumption_audit_key(receipt)
        )
    return {
        identity: len(touches[identity])
        for identity in sorted(touches, key=lambda item: item.encode("utf-8"))
    }


@dataclass(frozen=True)
class ConsumptionFoldResult:
    """What one retention pass did: receipts folded, raw partitions and legacy events removed."""

    folded: int = 0
    removed_partitions: int = 0
    removed_legacy_events: int = 0


def fold_consumption_receipts(
    instance: PlaybillInstance, *, now: datetime, days: int
) -> ConsumptionFoldResult:
    """Fold raw receipts recorded before ``now - days`` into counters, then remove them.

    Counters are appended first (a fold event per batch, in the consumption
    family's ``folds`` partition); only receipts a fold durably names are
    removed: their singleton partitions, and the legacy chained ``receipts``
    partition cut back to its epoch once every receipt in it is folded. A
    crash in between leaves raw receipts a fold already counted, which every
    reader skips and the next pass removes.
    """

    store = instance.review_operational_store()
    state = _consumption_state(store)
    cutoff = ensure_utc(now) - timedelta(days=days)
    due = sorted(
        receipt_id
        for receipt_id, (_receipt, event) in state.raw.items()
        if event.recorded_at < cutoff
    )
    if not due and not state.spent:
        return ConsumptionFoldResult()
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
            receipt, _event = state.raw[receipt_id]
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
        store.append(
            family="consumption",
            partition_id=CONSUMPTION_FOLD_PARTITION_ID,
            event_id=fold.event_id,
            payload=fold,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor,
            recorded_at=ensure_utc(now),
        )
    # Only what a fold durably names is removed, read back from the store.
    state = _consumption_state(store)
    singletons = sorted(
        partition for partition, _receipt_id in state.spent if partition.startswith("event:")
    )
    removed = store.remove_partitions(family="consumption", partition_ids=singletons)
    legacy = 0
    first = store.partition_payloads(
        family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID
    )[:1]
    if (
        state.legacy_receipts
        and first
        and first[0].get("tag") == "playbill-consumption-epoch-v1"
        and not any(
            event.partition_id == CONSUMPTION_EPOCH_PARTITION_ID
            for _receipt, event in state.raw.values()
        )
    ):
        # Every receipt the legacy chain holds is folded: keep only its epoch.
        legacy = store.truncate_partition(
            family="consumption", partition_id=CONSUMPTION_EPOCH_PARTITION_ID, keep=1
        )
    return ConsumptionFoldResult(
        folded=len(due), removed_partitions=removed, removed_legacy_events=legacy
    )


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
