"""Service-owned read-touch receipts for accepted Playbill artifacts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.closure import dependency_artifacts, parse_dependency_artifact
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.review_operational import ReviewOperationalStoreError

CONSUMPTION_RECEIPT_ID_DOMAIN = "playbill-consumption-receipt-v1"
CONSUMPTION_PARTITION_ID = "receipts"
CONSUMPTION_EPOCH_EVENT_ID = "consumption-epoch"

ConsumptionOperation: TypeAlias = Literal[
    "playbill.claim.get",
    "playbill.claim_type.get",
    "playbill.coverage.resolve",
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
    matches = tuple(
        item.sequence for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    if len(matches) != 1:
        raise ReviewOperationalStoreError(
            "consumption receipt coordinate is not accepted by this instance"
        )
    return matches[0]


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
    generation = _generation(instance, coordinate)
    ordered = tuple(
        sorted(
            set(artifacts),
            key=lambda item: (item[0].qualified.encode("utf-8"), item[1].encode("ascii")),
        )
    )
    if not ordered:
        return ()
    store = instance.review_operational_store()
    existing = store.events(family="consumption")
    if not any(payload.get("tag") == "playbill-consumption-epoch-v1" for _, payload in existing):
        epoch = ConsumptionEpochV1(
            consumption_epoch_generation=generation,
            accepted_coordinate=coordinate,
        )
        store.append(
            family="consumption",
            partition_id=CONSUMPTION_PARTITION_ID,
            event_id=epoch.event_id,
            payload=epoch,
            coordinate=coordinate,
            generation=generation,
            actor_context=context.actor_context,
            recorded_at=context.actor_context.timestamp,
        )
    receipts: list[ConsumptionReceiptV1] = []
    for identity, digest in ordered:
        receipt = build_consumption_receipt(
            context=context,
            operation=operation,
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=digest,
        )
        store.append(
            family="consumption",
            partition_id=CONSUMPTION_PARTITION_ID,
            event_id=receipt.receipt_id,
            payload=receipt,
            coordinate=coordinate,
            generation=generation,
            actor_context=context.actor_context,
            recorded_at=context.actor_context.timestamp,
        )
        receipts.append(receipt)
    return tuple(receipts)


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
    tree: Mapping[str, bytes], root_path: str
) -> tuple[tuple[ArtifactIdentity, str], ...]:
    """Return one artifact and the accepted dependencies it actually resolves."""

    states = dependency_artifacts(tree)
    by_path = {item.path: item for item in states}
    by_identity = {item.identity.qualified: item for item in states}
    root = by_path.get(root_path)
    if root is None:
        return ()
    pending = [root]
    visited: set[str] = set()
    result: list[tuple[ArtifactIdentity, str]] = []
    while pending:
        current = pending.pop()
        if current.identity.qualified in visited:
            continue
        visited.add(current.identity.qualified)
        result.append((current.identity, current.artifact_digest))
        for pin in reversed(current.pins):
            dependency = by_identity.get(pin.target.qualified)
            if dependency is not None:
                pending.append(dependency)
    return tuple(sorted(result, key=lambda item: item[0].qualified.encode("utf-8")))


def consumption_aggregate(instance: PlaybillInstance) -> ConsumptionAggregateV1:
    events = instance.review_operational_store().events(family="consumption")
    epoch: ConsumptionEpochV1 | None = None
    receipts: list[ConsumptionReceiptV1] = []
    for _event, payload in events:
        if payload.get("tag") == "playbill-consumption-epoch-v1":
            parsed_epoch = ConsumptionEpochV1.model_validate(payload)
            if epoch is not None and parsed_epoch != epoch:
                raise ReviewOperationalStoreError("consumption epoch is not unique")
            epoch = parsed_epoch
        elif payload.get("tag") == "playbill-consumption-receipt-v1":
            receipts.append(ConsumptionReceiptV1.model_validate(payload))
        else:
            raise ReviewOperationalStoreError("consumption partition has an unknown payload")

    by_identity: dict[str, list[ConsumptionReceiptV1]] = {}
    identities: dict[str, ArtifactIdentity] = {}
    for receipt in receipts:
        key = receipt.response_artifact_identity.qualified
        identities[key] = receipt.response_artifact_identity
        by_identity.setdefault(key, []).append(receipt)
    aggregates: list[ConsumptionArtifactAggregateV1] = []
    for key in sorted(by_identity, key=lambda item: item.encode("utf-8")):
        rows = by_identity[key]
        counts = Counter(item.operation for item in rows)
        operations = tuple(
            (operation, counts[operation])
            for operation in QUALIFYING_CONSUMPTION_OPERATIONS
            if counts[operation]
        )
        aggregates.append(
            ConsumptionArtifactAggregateV1(
                artifact_identity=identities[key],
                total_touch_count=len(rows),
                qualifying_touch_count=sum(count for _, count in operations),
                touches_by_operation=operations,
            )
        )
    return ConsumptionAggregateV1(
        initialized=epoch is not None,
        consumption_epoch_generation=(
            None if epoch is None else epoch.consumption_epoch_generation
        ),
        artifacts=tuple(aggregates),
    )


__all__ = [
    "CONSUMPTION_RECEIPT_ID_DOMAIN",
    "ConsumptionAggregateV1",
    "ConsumptionArtifactAggregateV1",
    "ConsumptionContextV1",
    "ConsumptionEpochV1",
    "ConsumptionOperation",
    "ConsumptionReceiptV1",
    "QUALIFYING_CONSUMPTION_OPERATIONS",
    "build_consumption_receipt",
    "consumption_aggregate",
    "consumption_artifacts_for_dependency_closure",
    "consumption_artifacts_for_paths",
    "consumption_receipt_id",
    "record_consumption",
]
