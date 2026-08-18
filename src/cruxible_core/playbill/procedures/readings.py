"""Exact-grain Procedure observations with fail-closed contract grading."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import (
    JournalStreamIdentityV1,
    ProcedureExhaustWriter,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.procedures.artifacts import AcceptedProcedureV1
from cruxible_core.playbill.procedures.execution import accepted_procedure_pin_set_digest
from cruxible_core.playbill.procedures.graph import compute_procedure_node_digests_v3
from cruxible_core.playbill.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    ResolutionContractActivationV1,
    procedure_arm_content_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.temporal import ensure_utc, format_datetime

ProcedureReadingGradeV1 = Literal["contract", "observation"]
ProcedureReadingVerdictV1 = Literal["satisfied", "contradicted", "indeterminate"]


class _StrictReadingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


class ProcedureReadingV1(_StrictReadingModel):
    tag: Literal["playbill-procedure-reading-v1"] = "playbill-procedure-reading-v1"
    reading_id: str
    subject_grain: Literal["procedure_unit", "node", "arm"]
    subject: SemanticAddress
    accepted_coordinate: AcceptedCoordinate
    definition_digest: str
    node_id: str | None = None
    node_local_digest: str | None = None
    from_node_id: str | None = None
    from_node_local_digest: str | None = None
    arm_label: Literal["on_true", "on_false"] | None = None
    arm_subtree_digest: str | None = None
    pin_set_digest: str
    grade: ProcedureReadingGradeV1
    measurement_name: str | None = None
    contract_id: str | None = None
    resolution_id: str | None = None
    verdict: ProcedureReadingVerdictV1
    value: object | None = None
    run_id: str | None = None
    run_receipt_digest: str | None = None
    episode_ref: str | None = None
    situation_shape: object | None = None
    evidence_refs: tuple[ProcedureProofReferenceV1, ...] = ()
    claim_attestation_digests: tuple[str, ...] = ()
    observed_at: datetime
    recorded_at: datetime
    actor_context: GovernedActorContext
    idempotency_key: str | None = None

    @field_validator(
        "definition_digest",
        "node_local_digest",
        "from_node_local_digest",
        "arm_subtree_digest",
        "pin_set_digest",
        "run_receipt_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _digest(value, label=str(getattr(info, "field_name", "reading digest")))
        )

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object | None) -> CanonicalValue | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("situation_shape", mode="before")
    @classmethod
    def _situation_shape(cls, value: object | None) -> object | None:
        if value is None:
            return None
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):
            raise ValueError("Procedure reading situation_shape must be a canonical object")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def _evidence(
        cls, value: tuple[ProcedureProofReferenceV1, ...]
    ) -> tuple[ProcedureProofReferenceV1, ...]:
        keys = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("Procedure reading evidence_refs must be sorted and unique")
        return value

    @field_validator("claim_attestation_digests")
    @classmethod
    def _attestations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _digest(item, label="ClaimAttestation digest")
        if value != tuple(sorted(set(value))):
            raise ValueError("ClaimAttestation digests must be sorted and unique")
        return value

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observed_at", "recorded_at", when_used="json")
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_key(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("reading idempotency_key must be nonblank and normalized")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureReadingV1":
        _validate_grain(
            self.subject_grain,
            node_id=self.node_id,
            node_local_digest=self.node_local_digest,
            from_node_id=self.from_node_id,
            from_node_local_digest=self.from_node_local_digest,
            arm_label=self.arm_label,
            arm_subtree_digest=self.arm_subtree_digest,
        )
        if self.observed_at > self.recorded_at:
            raise ValueError("Procedure reading observed_at must not follow recorded_at")
        if (self.run_id is None) != (self.run_receipt_digest is None):
            raise ValueError("Procedure reading run_id and run_receipt_digest are a pair")
        if self.grade == "contract":
            if None in (self.measurement_name, self.contract_id, self.resolution_id):
                raise ValueError("contract-grade reading requires measurement/contract/resolution")
        elif any(
            item is not None
            for item in (self.measurement_name, self.contract_id, self.resolution_id)
        ):
            raise ValueError("observation-grade reading cannot claim contract coordinates")
        if self.reading_id != procedure_reading_id(self):
            raise ValueError("Procedure reading_id does not reproduce")
        return self


def _validate_grain(
    grain: str,
    *,
    node_id: str | None,
    node_local_digest: str | None,
    from_node_id: str | None,
    from_node_local_digest: str | None,
    arm_label: str | None,
    arm_subtree_digest: str | None,
) -> None:
    if grain == "procedure_unit":
        if any(
            item is not None
            for item in (
                node_id,
                node_local_digest,
                from_node_id,
                from_node_local_digest,
                arm_label,
                arm_subtree_digest,
            )
        ):
            raise ValueError("procedure-unit reading cannot carry node/arm fields")
        return
    if node_id is None or node_local_digest is None:
        raise ValueError("node and arm readings require node_id and node_local_digest")
    if grain == "node":
        if any(
            item is not None
            for item in (from_node_id, from_node_local_digest, arm_label, arm_subtree_digest)
        ):
            raise ValueError("node reading cannot carry arm fields")
        return
    if None in (from_node_id, from_node_local_digest, arm_label, arm_subtree_digest):
        raise ValueError("arm reading requires both endpoints, arm label, and subtree digest")


def procedure_reading_id(reading: ProcedureReadingV1) -> str:
    if reading.idempotency_key is not None:
        identity_payload: object = {
            "procedure_artifact_path": reading.subject.artifact_path,
            "actor_org_id": reading.actor_context.org_id,
            "actor_id": reading.actor_context.actor_id,
            "idempotency_key": reading.idempotency_key,
        }
    else:
        payload = reading.model_dump(mode="json")
        payload.pop("tag", None)
        payload.pop("reading_id", None)
        identity_payload = payload
    digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-reading-identity-v1",
        {"reading": identity_payload},
    ).value
    return f"PRD-{digest[:32]}"


def procedure_reading_digest(reading: ProcedureReadingV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-reading-v1",
        {"reading": reading.model_dump(mode="json")},
    ).tagged


def procedure_reading_partition_id(accepted: AcceptedProcedureV1) -> str:
    """Return the sole partition for this Procedure's instance-wide reading domain."""

    identity_digest = hashlib.sha256(
        accepted.procedure.identity.qualified.encode("utf-8")
    ).hexdigest()
    return f"procedure-readings:{identity_digest}"


def _grain_fields(
    accepted: AcceptedProcedureV1,
    *,
    grain: Literal["procedure_unit", "node", "arm"],
    node_id: str | None,
    from_node_id: str | None,
    arm_label: Literal["on_true", "on_false"] | None,
) -> dict[str, Any]:
    digests = compute_procedure_node_digests_v3(accepted.procedure.definition)
    if grain == "procedure_unit":
        return {
            "subject": SemanticAddress.procedure_unit(accepted.path),
            "node_id": None,
            "node_local_digest": None,
            "from_node_id": None,
            "from_node_local_digest": None,
            "arm_label": None,
            "arm_subtree_digest": None,
        }
    if node_id is None or node_id not in digests:
        raise PlaybillExecutionError("reading node_id does not name a Procedure node")
    if grain == "node":
        return {
            "subject": SemanticAddress.procedure_node(accepted.path, node_id),
            "node_id": node_id,
            "node_local_digest": digests[node_id].local_digest,
            "from_node_id": None,
            "from_node_local_digest": None,
            "arm_label": None,
            "arm_subtree_digest": None,
        }
    if from_node_id is None or arm_label is None or from_node_id not in digests:
        raise PlaybillExecutionError("arm reading lacks a valid source endpoint")
    graph_edges = {node.node_id: node for node in accepted.procedure.definition.nodes}
    source = graph_edges[from_node_id]
    actual_target = getattr(source, arm_label)
    if actual_target != node_id:
        raise PlaybillExecutionError("arm reading does not name an authored graph edge")
    arm_digest = procedure_arm_content_digest(
        from_node_id=from_node_id,
        from_node_local_digest=digests[from_node_id].local_digest,
        arm_label=arm_label,
        target_node_id=node_id,
        target_node_local_digest=digests[node_id].local_digest,
        target_subtree_digest=digests[node_id].subtree_digest,
    )
    return {
        "subject": SemanticAddress.procedure_arm(
            accepted.path,
            from_node_id=from_node_id,
            arm_label=arm_label,
            target_node_id=node_id,
        ),
        "node_id": node_id,
        "node_local_digest": digests[node_id].local_digest,
        "from_node_id": from_node_id,
        "from_node_local_digest": digests[from_node_id].local_digest,
        "arm_label": arm_label,
        "arm_subtree_digest": arm_digest,
    }


def build_procedure_reading(
    accepted: AcceptedProcedureV1,
    *,
    accepted_coordinate: AcceptedCoordinate,
    subject_grain: Literal["procedure_unit", "node", "arm"],
    grade: ProcedureReadingGradeV1,
    verdict: ProcedureReadingVerdictV1,
    observed_at: datetime,
    recorded_at: datetime,
    actor_context: GovernedActorContext,
    node_id: str | None = None,
    from_node_id: str | None = None,
    arm_label: Literal["on_true", "on_false"] | None = None,
    activation: ResolutionContractActivationV1 | None = None,
    resolution_id: str | None = None,
    value: object | None = None,
    run_id: str | None = None,
    run_receipt_digest: str | None = None,
    episode_ref: str | None = None,
    situation_shape: object | None = None,
    evidence_refs: tuple[ProcedureProofReferenceV1, ...] = (),
    claim_attestation_digests: tuple[str, ...] = (),
    idempotency_key: str | None = None,
) -> ProcedureReadingV1:
    fields = _grain_fields(
        accepted,
        grain=subject_grain,
        node_id=node_id,
        from_node_id=from_node_id,
        arm_label=arm_label,
    )
    provisional = ProcedureReadingV1.model_construct(
        reading_id="",
        subject_grain=subject_grain,
        accepted_coordinate=accepted_coordinate,
        definition_digest=accepted.procedure.definition_digest,
        pin_set_digest=accepted_procedure_pin_set_digest(accepted),
        grade=grade,
        measurement_name=None if activation is None else activation.measurement_name,
        contract_id=None if activation is None else activation.contract_id,
        resolution_id=resolution_id,
        verdict=verdict,
        value=value,
        run_id=run_id,
        run_receipt_digest=run_receipt_digest,
        episode_ref=episode_ref,
        situation_shape=situation_shape,
        evidence_refs=evidence_refs,
        claim_attestation_digests=claim_attestation_digests,
        observed_at=ensure_utc(observed_at),
        recorded_at=ensure_utc(recorded_at),
        actor_context=actor_context,
        idempotency_key=idempotency_key,
        **fields,
    )
    return ProcedureReadingV1.model_validate(
        provisional.model_copy(update={"reading_id": procedure_reading_id(provisional)}).model_dump(
            mode="python"
        )
    )


class ProcedureReadingLawResultV1(_StrictReadingModel):
    tag: Literal["playbill-procedure-reading-law-v1"] = "playbill-procedure-reading-law-v1"
    verdict: Literal["accepted", "refused"]
    reading_digest: str | None = None
    refusal_code: str | None = None
    message: str | None = None


def _refused(code: str, message: str) -> ProcedureReadingLawResultV1:
    return ProcedureReadingLawResultV1(
        verdict="refused",
        refusal_code=code,
        message=message,
    )


def evaluate_procedure_reading(
    reading: ProcedureReadingV1,
    *,
    accepted: AcceptedProcedureV1,
    accepted_coordinate: AcceptedCoordinate,
    activations: tuple[ResolutionContractActivationV1, ...] = (),
    resolution_book: ProcedureResolutionBook | None = None,
) -> ProcedureReadingLawResultV1:
    """Refuse any exact-grain or contract-grade mismatch; never downgrade."""

    expected = _grain_fields(
        accepted,
        grain=reading.subject_grain,
        node_id=reading.node_id,
        from_node_id=reading.from_node_id,
        arm_label=reading.arm_label,
    )
    if reading.accepted_coordinate != accepted_coordinate:
        return _refused("reading.coordinate_mismatch", "Reading names another accepted coordinate.")
    if reading.definition_digest != accepted.procedure.definition_digest:
        return _refused("reading.definition_mismatch", "Reading definition digest is stale.")
    for field_name, value in expected.items():
        if getattr(reading, field_name) != value:
            return _refused(
                "reading.semantic_grain_mismatch",
                f"Reading field {field_name} differs from the authored Procedure grain.",
            )
    if reading.pin_set_digest != accepted_procedure_pin_set_digest(accepted):
        return _refused(
            "reading.pin_set_mismatch", "Reading pin set differs from the exact Procedure pins."
        )
    if reading.grade == "observation":
        return ProcedureReadingLawResultV1(
            verdict="accepted",
            reading_digest=procedure_reading_digest(reading),
        )
    activation = next(
        (
            item
            for item in activations
            if item.contract_id == reading.contract_id
            and item.measurement_name == reading.measurement_name
        ),
        None,
    )
    if activation is None or resolution_book is None:
        return _refused(
            "reading.contract_activation_missing",
            "Contract-grade reading names no exact accepted activation.",
        )
    if (
        activation.subject.address != reading.subject
        or activation.subject.accepted_coordinate != reading.accepted_coordinate
        or activation.definition_digest != reading.definition_digest
        or activation.node_id != reading.node_id
        or activation.node_local_digest != reading.node_local_digest
        or activation.from_node_id != reading.from_node_id
        or activation.from_node_local_digest != reading.from_node_local_digest
        or activation.arm_label != reading.arm_label
        or activation.arm_subtree_digest != reading.arm_subtree_digest
    ):
        return _refused(
            "reading.contract_subject_mismatch",
            "Contract activation and reading do not name the same exact semantic grain.",
        )
    resolution = resolution_book.latest_non_overturned(activation.contract_id)
    if resolution is None or resolution.resolution_id != reading.resolution_id:
        return _refused(
            "reading.current_resolution_missing",
            "Contract-grade reading requires the latest non-overturned resolution.",
        )
    if resolution.verdict != reading.verdict or resolution.value != reading.value:
        return _refused(
            "reading.resolution_result_mismatch",
            "Reading verdict/value differs from its exact resolution.",
        )
    if reading.observed_at != resolution.observed_at:
        return _refused(
            "reading.resolution_time_mismatch",
            "Reading observed_at differs from its exact resolution.",
        )
    return ProcedureReadingLawResultV1(
        verdict="accepted",
        reading_digest=procedure_reading_digest(reading),
    )


def append_procedure_reading(
    writer: ProcedureExhaustWriter,
    *,
    reading: ProcedureReadingV1,
    accepted: AcceptedProcedureV1,
    accepted_coordinate: AcceptedCoordinate,
    stream: JournalStreamIdentityV1,
    bodies: ContentAddressedBodyStore,
    activations: tuple[ResolutionContractActivationV1, ...] = (),
    resolution_book: ProcedureResolutionBook | None = None,
) -> StoredProcedureJournalRecordV1:
    """Append idempotently in the frozen instance/Procedure/actor/key domain."""

    partition_id = procedure_reading_partition_id(accepted)

    law = evaluate_procedure_reading(
        reading,
        accepted=accepted,
        accepted_coordinate=accepted_coordinate,
        activations=activations,
        resolution_book=resolution_book,
    )
    if law.verdict == "refused":
        raise PlaybillExecutionError(law.message or "Procedure reading law refused")

    if reading.idempotency_key is not None:
        access = BodyAccessContext(principal_id="procedure-reading-replay", can_read_body=True)
        for stored in writer.journal.all_records(stream, partition_id):
            if stored.record.event_kind != "procedure_reading":
                continue
            existing = ProcedureReadingV1.model_validate(
                parse_journal_payload(bodies.read(stored.record.payload_digest, access=access))
            )
            same_domain = (
                existing.idempotency_key == reading.idempotency_key
                and existing.subject.artifact_path == reading.subject.artifact_path
                and existing.actor_context.org_id == reading.actor_context.org_id
                and existing.actor_context.actor_id == reading.actor_context.actor_id
            )
            if not same_domain:
                continue
            if procedure_reading_digest(existing) != procedure_reading_digest(reading):
                raise PlaybillExecutionError(
                    "Procedure reading idempotency key was retried with a different payload"
                )
            return stored
    return writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="procedure_reading",
        accepted_coordinate=reading.accepted_coordinate,
        procedure_artifact_digest=accepted.artifact_digest,
        definition_digest=reading.definition_digest,
        actor_context=reading.actor_context,
        recorded_at=reading.recorded_at,
        payload=reading.model_dump(mode="json"),
        run_id=reading.run_id,
    )


__all__ = [
    "ProcedureReadingGradeV1",
    "ProcedureReadingLawResultV1",
    "ProcedureReadingV1",
    "ProcedureReadingVerdictV1",
    "append_procedure_reading",
    "build_procedure_reading",
    "evaluate_procedure_reading",
    "procedure_reading_digest",
    "procedure_reading_id",
    "procedure_reading_partition_id",
]
