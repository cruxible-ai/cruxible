"""Deterministic settled-outcome fold over the existing resolution journal."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import StoredProcedureJournalRecordV1
from cruxible_core.playbill.procedures.resolution import (
    ProcedureResolutionBook,
    ProcedureResolutionV2,
    ResolutionContractActivationV1,
    ResolutionContractActivationV2,
    SettledOutcomeRelationV1,
    build_settled_outcome_relation,
    resolution_contract_partition_id,
    settled_outcome_relation_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

_PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

SettledOutcomeHistoryStatusV1 = Literal[
    "open",
    "indeterminate",
    "overturned",
    "unrelated_resolution",
    "settled_false",
    "settled_true",
]


class _StrictSettledOutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a tagged lowercase SHA-256 digest") from exc
    return value


def _sorted_unique_strings(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be byte-sorted and unique")
    return value


class SettledOutcomesAccessProfileV1(_StrictSettledOutcomeModel):
    """Exact read gate and per-proof visibility used by one deterministic fold."""

    tag: Literal["playbill-settled-outcomes-access-profile-v1"] = (
        "playbill-settled-outcomes-access-profile-v1"
    )
    profile_id: str
    can_read_resolution_bodies: bool
    visible_proof_digests: tuple[str, ...] = ()

    @field_validator("profile_id")
    @classmethod
    def _profile_id(cls, value: str) -> str:
        if not _PROFILE_ID_RE.fullmatch(value):
            raise ValueError("settled-outcomes profile_id must be a stable identifier")
        return value

    @field_validator("visible_proof_digests")
    @classmethod
    def _proof_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            _digest(digest, label="visible proof digest")
        return _sorted_unique_strings(value, label="visible proof digests")


class SettledOutcomesQueryRequestV1(_StrictSettledOutcomeModel):
    tag: Literal["playbill-settled-outcomes-query-request-v1"] = (
        "playbill-settled-outcomes-query-request-v1"
    )
    accepted_coordinate: AcceptedCoordinate
    evaluation_time: datetime
    access_profile: SettledOutcomesAccessProfileV1
    contract_ids: tuple[str, ...] = ()
    outcome_classes: tuple[str, ...] = ()
    procedure_artifact_digests: tuple[str, ...] = ()

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @field_validator("contract_ids", "outcome_classes")
    @classmethod
    def _filters(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _sorted_unique_strings(
            value,
            label=str(getattr(info, "field_name", "settled-outcomes filter")),
        )

    @field_validator("procedure_artifact_digests")
    @classmethod
    def _procedure_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            _digest(digest, label="Procedure artifact digest filter")
        return _sorted_unique_strings(value, label="Procedure artifact digest filters")


def settled_outcomes_query_request_digest(request: SettledOutcomesQueryRequestV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-settled-outcomes-query-request-v1",
        {"request": request.model_dump(mode="json")},
    ).tagged


class SettledOutcomeHistoryV1(_StrictSettledOutcomeModel):
    """Explicit non-row classification for one resolution contract history."""

    tag: Literal["playbill-settled-outcome-history-v1"] = "playbill-settled-outcome-history-v1"
    contract_id: str
    status: SettledOutcomeHistoryStatusV1
    resolution_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "SettledOutcomeHistoryV1":
        if (self.status == "open") != (self.resolution_id is None):
            raise ValueError("only an open resolution history lacks a resolution_id")
        return self


def classify_settled_outcome_history(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV2,
    book: ProcedureResolutionBook,
) -> SettledOutcomeHistoryV1:
    """Classify current replay state without treating non-settlement as a row."""

    resolutions = book.resolutions.get(activation.contract_id, ())
    if not resolutions:
        return SettledOutcomeHistoryV1(contract_id=activation.contract_id, status="open")
    latest = resolutions[-1]
    dispositions = book.dispositions.get(latest.resolution_id, ())
    if dispositions and dispositions[-1].verdict == "overturned":
        return SettledOutcomeHistoryV1(
            contract_id=activation.contract_id,
            status="overturned",
            resolution_id=latest.resolution_id,
        )
    if not isinstance(latest, ProcedureResolutionV2):
        status: SettledOutcomeHistoryStatusV1 = (
            "indeterminate" if latest.verdict == "indeterminate" else "unrelated_resolution"
        )
        return SettledOutcomeHistoryV1(
            contract_id=activation.contract_id,
            status=status,
            resolution_id=latest.resolution_id,
        )
    return SettledOutcomeHistoryV1(
        contract_id=activation.contract_id,
        status="settled_true" if latest.settlement_outcome else "settled_false",
        resolution_id=latest.resolution_id,
    )


class SettledOutcomeRowV1(_StrictSettledOutcomeModel):
    """One exact visible prediction/settlement pair; no convention can mint it."""

    tag: Literal["playbill-settled-outcome-row-v1"] = "playbill-settled-outcome-row-v1"
    relation: SettledOutcomeRelationV1
    relation_digest: str

    @field_validator("relation_digest")
    @classmethod
    def _relation_digest(cls, value: str) -> str:
        return _digest(value, label="settled outcome row relation_digest")

    @model_validator(mode="after")
    def _reproduce(self) -> "SettledOutcomeRowV1":
        expected = settled_outcome_relation_digest(
            self.relation.activation_digest,
            self.relation.resolution_digest,
        )
        if (
            self.relation_digest != self.relation.relation_digest
            or expected != self.relation_digest
        ):
            raise ValueError("settled outcome row relation digest does not reproduce")
        return self


class SettledOutcomesQueryResultV1(_StrictSettledOutcomeModel):
    tag: Literal["playbill-settled-outcomes-query-result-v1"] = (
        "playbill-settled-outcomes-query-result-v1"
    )
    request_digest: str
    accepted_coordinate: AcceptedCoordinate
    evaluation_time: datetime
    rows: tuple[SettledOutcomeRowV1, ...]
    result_digest: str

    @field_validator("request_digest", "result_digest")
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "query digest")))

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @field_validator("rows")
    @classmethod
    def _rows(cls, value: tuple[SettledOutcomeRowV1, ...]) -> tuple[SettledOutcomeRowV1, ...]:
        keys = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("settled outcome rows must be canonically sorted and unique")
        return value

    @model_validator(mode="after")
    def _result_digest(self) -> "SettledOutcomesQueryResultV1":
        if self.result_digest != settled_outcomes_query_result_digest(self):
            raise ValueError("settled-outcomes query result digest does not reproduce")
        return self


def settled_outcomes_query_result_digest(result: SettledOutcomesQueryResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(
        ArtifactDigest,
        "playbill-settled-outcomes-query-result-v1",
        payload,
    ).tagged


class SettledOutcomesQueryReceiptV1(_StrictSettledOutcomeModel):
    tag: Literal["playbill-settled-outcomes-query-receipt-v1"] = (
        "playbill-settled-outcomes-query-receipt-v1"
    )
    request_digest: str
    result_digest: str
    accepted_coordinate: AcceptedCoordinate
    evaluation_time: datetime
    visible_row_count: int = Field(ge=0)

    @field_validator("request_digest", "result_digest")
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "receipt digest")))

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered


def settled_outcomes_query_receipt_digest(receipt: SettledOutcomesQueryReceiptV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-settled-outcomes-query-receipt-v1",
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


def _build_result(
    request: SettledOutcomesQueryRequestV1,
    rows: tuple[SettledOutcomeRowV1, ...],
) -> SettledOutcomesQueryResultV1:
    values = {
        "request_digest": settled_outcomes_query_request_digest(request),
        "accepted_coordinate": request.accepted_coordinate,
        "evaluation_time": request.evaluation_time,
        "rows": rows,
    }
    provisional = SettledOutcomesQueryResultV1.model_construct(
        **cast(dict[str, Any], values),
        result_digest="sha256:" + "0" * 64,
    )
    return SettledOutcomesQueryResultV1.model_validate(
        {**values, "result_digest": settled_outcomes_query_result_digest(provisional)}
    )


def query_settled_outcomes(
    request: SettledOutcomesQueryRequestV1,
    *,
    activations: tuple[ResolutionContractActivationV1 | ResolutionContractActivationV2, ...],
    records_by_partition: Mapping[str, tuple[StoredProcedureJournalRecordV1, ...]],
    bodies: ContentAddressedBodyStore,
) -> tuple[SettledOutcomesQueryResultV1, SettledOutcomesQueryReceiptV1]:
    """Fold exact v2 histories, applying visibility before constructing any row."""

    activation_ids = tuple(item.contract_id for item in activations)
    if activation_ids != tuple(sorted(set(activation_ids), key=lambda item: item.encode("utf-8"))):
        raise PlaybillExecutionError(
            "settled-outcomes activations must be byte-sorted by unique contract_id"
        )

    rows: list[SettledOutcomeRowV1] = []
    access = request.access_profile
    if access.can_read_resolution_bodies:
        visible_proofs = frozenset(access.visible_proof_digests)
        for activation in activations:
            if not isinstance(activation, ResolutionContractActivationV2):
                if request.outcome_classes:
                    raise PlaybillExecutionError(
                        "settled-outcomes outcome_class filter requires v2 activations"
                    )
                continue
            if request.contract_ids and activation.contract_id not in request.contract_ids:
                continue
            if request.outcome_classes and activation.outcome_class not in request.outcome_classes:
                continue
            if (
                request.procedure_artifact_digests
                and activation.procedure_artifact_digest not in request.procedure_artifact_digests
            ):
                continue

            partition_id = resolution_contract_partition_id(activation)
            records = tuple(
                stored
                for stored in records_by_partition.get(partition_id, ())
                if stored.record.recorded_at <= request.evaluation_time
            )
            if any(stored.record.partition_id != partition_id for stored in records):
                raise PlaybillExecutionError(
                    "settled-outcomes record mapping crosses a resolution partition"
                )
            book = ProcedureResolutionBook((activation,))
            book.replay(records, bodies=bodies)
            history = classify_settled_outcome_history(activation, book)
            if history.status not in {"settled_false", "settled_true"}:
                continue
            resolution = book.resolution_by_id(history.resolution_id or "")
            if not isinstance(resolution, ProcedureResolutionV2):  # pragma: no cover - classifier
                raise PlaybillExecutionError("settled history lacks its v2 resolution")
            if any(proof.digest not in visible_proofs for proof in resolution.evidence_refs):
                continue
            relation = build_settled_outcome_relation(activation, resolution)
            rows.append(
                SettledOutcomeRowV1(
                    relation=relation,
                    relation_digest=relation.relation_digest,
                )
            )

    ordered_rows = tuple(
        sorted(rows, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    )
    result = _build_result(request, ordered_rows)
    receipt = SettledOutcomesQueryReceiptV1(
        request_digest=result.request_digest,
        result_digest=result.result_digest,
        accepted_coordinate=request.accepted_coordinate,
        evaluation_time=request.evaluation_time,
        visible_row_count=len(result.rows),
    )
    return result, receipt


__all__ = [
    "SettledOutcomeHistoryStatusV1",
    "SettledOutcomeHistoryV1",
    "SettledOutcomeRowV1",
    "SettledOutcomesAccessProfileV1",
    "SettledOutcomesQueryReceiptV1",
    "SettledOutcomesQueryRequestV1",
    "SettledOutcomesQueryResultV1",
    "classify_settled_outcome_history",
    "query_settled_outcomes",
    "settled_outcomes_query_receipt_digest",
    "settled_outcomes_query_request_digest",
    "settled_outcomes_query_result_digest",
]
