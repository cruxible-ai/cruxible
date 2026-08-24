"""Semantic ResolutionContract activation and append-only operational answers."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.graph import compute_procedure_node_digests_v3
from cruxible_client.contracts.procedures.measurements import (
    ClaimAttestationProcedureMeasurementV1,
    ClaimStatementProcedureMeasurementV1,
    ProcedureMeasurementDeclarationV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    JournalStreamIdentityV1,
    ProcedureExhaustWriter,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

ResolutionVerdictV1 = Literal["satisfied", "contradicted", "indeterminate"]
ResolutionDispositionVerdictV1 = Literal["upheld", "overturned"]


class _StrictResolutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a tagged lowercase SHA-256 digest") from exc
    return value


class ProcedureProofReferenceV1(_StrictResolutionModel):
    """One typed content address cited by a resolution or reading."""

    tag: Literal["playbill-procedure-proof-reference-v1"] = "playbill-procedure-proof-reference-v1"
    kind: Literal[
        "capture",
        "claim_attestation",
        "claim_statement",
        "query_receipt",
        "run_receipt",
        "journal_record",
        "promotion",
    ]
    digest: str
    subject: SemanticAddress | None = None

    @field_validator("digest")
    @classmethod
    def _proof_digest(cls, value: str) -> str:
        return _digest(value, label="proof reference digest")


class ResolutionSubjectV1(_StrictResolutionModel):
    """Exact semantic grain committed by a ResolutionContract activation."""

    tag: Literal["playbill-resolution-subject-v1"] = "playbill-resolution-subject-v1"
    address: SemanticAddress
    content_digest: str
    accepted_coordinate: AcceptedCoordinate

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        return _digest(value, label="resolution subject content_digest")


class ResolutionContractActivationV1(_StrictResolutionModel):
    """Deterministic projection row derived from accepted Procedure intent."""

    tag: Literal["playbill-resolution-contract-activation-v1"] = (
        "playbill-resolution-contract-activation-v1"
    )
    contract_id: str
    activation_id: str
    procedure_identity: ArtifactIdentity
    procedure_path: str
    procedure_artifact_digest: str
    definition_digest: str
    measurement_name: str
    declaration: ProcedureMeasurementDeclarationV1
    subject_grain: Literal["procedure_unit", "node", "arm"]
    subject: ResolutionSubjectV1
    node_id: str | None = None
    node_local_digest: str | None = None
    from_node_id: str | None = None
    from_node_local_digest: str | None = None
    arm_label: Literal["on_true", "on_false"] | None = None
    arm_subtree_digest: str | None = None
    activated_at: datetime
    check_at: datetime
    expires_at: datetime

    @field_validator(
        "procedure_artifact_digest",
        "definition_digest",
        "node_local_digest",
        "from_node_local_digest",
        "arm_subtree_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _digest(value, label=str(getattr(info, "field_name", "activation digest")))
        )

    @field_validator("activated_at", "check_at", "expires_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("activated_at", "check_at", "expires_at", when_used="json")
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "ResolutionContractActivationV1":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("ResolutionContract activation must name a Procedure")
        if self.measurement_name != self.declaration.name:
            raise ValueError("activation measurement name differs from its declaration")
        if self.subject_grain != self.declaration.subject_grain:
            raise ValueError("activation subject grain differs from its declaration")
        if not self.activated_at <= self.check_at < self.expires_at:
            raise ValueError("activation requires activated_at <= check_at < expires_at")
        _validate_grain_fields(
            self.subject_grain,
            node_id=self.node_id,
            node_local_digest=self.node_local_digest,
            from_node_id=self.from_node_id,
            from_node_local_digest=self.from_node_local_digest,
            arm_label=self.arm_label,
            arm_subtree_digest=self.arm_subtree_digest,
        )
        if self.contract_id != resolution_contract_id(self):
            raise ValueError("ResolutionContract contract_id does not reproduce")
        if self.activation_id != resolution_activation_id(self):
            raise ValueError("ResolutionContract activation_id does not reproduce")
        return self


def _validate_grain_fields(
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
            raise ValueError("procedure-unit resolution subject cannot carry node/arm fields")
        return
    if node_id is None or node_local_digest is None:
        raise ValueError("node and arm resolution subjects require target node digest")
    if grain == "node":
        if any(
            item is not None
            for item in (from_node_id, from_node_local_digest, arm_label, arm_subtree_digest)
        ):
            raise ValueError("node resolution subject cannot carry arm fields")
        return
    if None in (from_node_id, from_node_local_digest, arm_label, arm_subtree_digest):
        raise ValueError("arm resolution subject requires endpoint and subtree digests")


def resolution_contract_id(activation: ResolutionContractActivationV1) -> str:
    payload = activation.model_dump(mode="json")
    payload.pop("tag", None)
    payload.pop("contract_id", None)
    payload.pop("activation_id", None)
    digest = typed_digest(
        ArtifactDigest,
        "playbill-resolution-contract-v1",
        {"activation": payload},
    ).value
    return f"RSC-{digest[:32]}"


def resolution_activation_id(activation: ResolutionContractActivationV1) -> str:
    digest = typed_digest(
        ArtifactDigest,
        "playbill-resolution-contract-activation-v1",
        {
            "contract_id": activation.contract_id,
            "accepted_coordinate": activation.subject.accepted_coordinate.model_dump(mode="json"),
        },
    ).value
    return f"RSA-{digest[:32]}"


def procedure_arm_content_digest(
    *,
    from_node_id: str,
    from_node_local_digest: str,
    arm_label: str,
    target_node_id: str,
    target_node_local_digest: str,
    target_subtree_digest: str,
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-arm-v1",
        {
            "from_node_id": from_node_id,
            "from_node_local_digest": from_node_local_digest,
            "arm_label": arm_label,
            "target_node_id": target_node_id,
            "target_node_local_digest": target_node_local_digest,
            "target_subtree_digest": target_subtree_digest,
        },
    ).tagged


def derive_resolution_activations(
    accepted: AcceptedProcedureV1,
    *,
    accepted_coordinate: AcceptedCoordinate,
    activated_at: datetime,
) -> tuple[ResolutionContractActivationV1, ...]:
    """Derive every activation without a mutable open-contract transaction."""

    activated_at = ensure_utc(activated_at)
    definition = accepted.procedure.definition
    node_digests = compute_procedure_node_digests_v3(definition)
    activations: list[ResolutionContractActivationV1] = []
    for declaration in definition.measurements:
        node_id: str | None = None
        node_local: str | None = None
        from_node_id: str | None = None
        from_node_local: str | None = None
        arm_label: Literal["on_true", "on_false"] | None = None
        arm_subtree: str | None = None
        if declaration.subject_grain == "procedure_unit":
            address = SemanticAddress.procedure_unit(accepted.path)
            content_digest = accepted.procedure.definition_digest
        elif declaration.subject_grain == "node":
            node_id = declaration.node_id
            if node_id is None:  # pragma: no cover - declaration validator owns this
                raise PlaybillExecutionError("node measurement lacks node_id")
            node_local = node_digests[node_id].local_digest
            address = SemanticAddress.procedure_node(accepted.path, node_id)
            content_digest = node_local
        else:
            node_id = declaration.node_id
            from_node_id = declaration.from_node_id
            arm_label = declaration.arm_label
            if node_id is None or from_node_id is None or arm_label is None:  # pragma: no cover
                raise PlaybillExecutionError("arm measurement lacks an endpoint")
            node_local = node_digests[node_id].local_digest
            from_node_local = node_digests[from_node_id].local_digest
            arm_subtree = procedure_arm_content_digest(
                from_node_id=from_node_id,
                from_node_local_digest=from_node_local,
                arm_label=arm_label,
                target_node_id=node_id,
                target_node_local_digest=node_local,
                target_subtree_digest=node_digests[node_id].subtree_digest,
            )
            address = SemanticAddress.procedure_arm(
                accepted.path,
                from_node_id=from_node_id,
                arm_label=arm_label,
                target_node_id=node_id,
            )
            content_digest = arm_subtree
        subject = ResolutionSubjectV1(
            address=address,
            content_digest=content_digest,
            accepted_coordinate=accepted_coordinate,
        )
        provisional = ResolutionContractActivationV1.model_construct(
            contract_id="",
            activation_id="",
            procedure_identity=accepted.procedure.identity,
            procedure_path=accepted.path,
            procedure_artifact_digest=accepted.artifact_digest,
            definition_digest=accepted.procedure.definition_digest,
            measurement_name=declaration.name,
            declaration=declaration,
            subject_grain=declaration.subject_grain,
            subject=subject,
            node_id=node_id,
            node_local_digest=node_local,
            from_node_id=from_node_id,
            from_node_local_digest=from_node_local,
            arm_label=arm_label,
            arm_subtree_digest=arm_subtree,
            activated_at=activated_at,
            check_at=activated_at + timedelta(microseconds=declaration.check_after.microseconds),
            expires_at=activated_at
            + timedelta(microseconds=declaration.expires_after.microseconds),
        )
        contract_id = resolution_contract_id(provisional)
        with_contract = provisional.model_copy(update={"contract_id": contract_id})
        activations.append(
            ResolutionContractActivationV1.model_validate(
                with_contract.model_copy(
                    update={"activation_id": resolution_activation_id(with_contract)}
                ).model_dump(mode="python")
            )
        )
    return tuple(sorted(activations, key=lambda item: item.measurement_name.encode("utf-8")))


class ProcedureResolutionV1(_StrictResolutionModel):
    tag: Literal["playbill-procedure-resolution-v1"] = "playbill-procedure-resolution-v1"
    resolution_id: str
    contract_id: str
    sequence: int = Field(ge=1)
    subject: ResolutionSubjectV1
    measurement_name: str
    verdict: ResolutionVerdictV1
    value: object | None = None
    evidence_refs: tuple[ProcedureProofReferenceV1, ...] = ()
    observed_at: datetime
    recorded_at: datetime
    actor_context: GovernedActorContext
    note: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object | None) -> CanonicalValue | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observed_at", "recorded_at", when_used="json")
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)

    @field_validator("evidence_refs")
    @classmethod
    def _evidence(
        cls, value: tuple[ProcedureProofReferenceV1, ...]
    ) -> tuple[ProcedureProofReferenceV1, ...]:
        keys = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("resolution evidence references must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureResolutionV1":
        if self.observed_at > self.recorded_at:
            raise ValueError("resolution observed_at must not follow recorded_at")
        if self.verdict in {"satisfied", "contradicted"} and not self.evidence_refs:
            raise ValueError(f"resolution verdict {self.verdict!r} requires evidence")
        if self.verdict == "contradicted" and not (self.note or "").strip():
            raise ValueError("contradicted resolution requires a note")
        if self.resolution_id != procedure_resolution_id(self):
            raise ValueError("Procedure resolution_id does not reproduce")
        return self


def procedure_resolution_id(resolution: ProcedureResolutionV1) -> str:
    payload = resolution.model_dump(mode="json")
    payload.pop("tag", None)
    payload.pop("resolution_id", None)
    digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-resolution-v1",
        {"resolution": payload},
    ).value
    return f"RSR-{digest[:32]}"


def resolution_contract_partition_id(activation: ResolutionContractActivationV1) -> str:
    """Return the sole journal partition for one derived contract activation."""

    return f"resolutions:{activation.contract_id}"


def build_procedure_resolution(
    activation: ResolutionContractActivationV1,
    *,
    sequence: int,
    verdict: ResolutionVerdictV1,
    value: object | None,
    evidence_refs: tuple[ProcedureProofReferenceV1, ...],
    observed_at: datetime,
    recorded_at: datetime,
    actor_context: GovernedActorContext,
    note: str | None = None,
) -> ProcedureResolutionV1:
    provisional = ProcedureResolutionV1.model_construct(
        resolution_id="",
        contract_id=activation.contract_id,
        sequence=sequence,
        subject=activation.subject,
        measurement_name=activation.measurement_name,
        verdict=verdict,
        value=value,
        evidence_refs=evidence_refs,
        observed_at=ensure_utc(observed_at),
        recorded_at=ensure_utc(recorded_at),
        actor_context=actor_context,
        note=note,
    )
    return ProcedureResolutionV1.model_validate(
        provisional.model_copy(
            update={"resolution_id": procedure_resolution_id(provisional)}
        ).model_dump(mode="python")
    )


class ProcedureResolutionLawResultV1(_StrictResolutionModel):
    tag: Literal["playbill-procedure-resolution-law-v1"] = "playbill-procedure-resolution-law-v1"
    verdict: Literal["accepted", "refused"]
    resolution_digest: str | None = None
    refusal_code: str | None = None
    message: str | None = None


def _resolution_refused(code: str, message: str) -> ProcedureResolutionLawResultV1:
    return ProcedureResolutionLawResultV1(
        verdict="refused",
        refusal_code=code,
        message=message,
    )


def _expectation_holds(
    expectation: object,
    *,
    value: CanonicalValue | None,
    evidence_count: int,
) -> bool:
    min_count = getattr(expectation, "min_count", None)
    max_count = getattr(expectation, "max_count", None)
    condition = getattr(expectation, "condition", None)
    condition_scope = getattr(expectation, "condition_scope", "all")
    rows: list[CanonicalValue]
    total = evidence_count
    if isinstance(value, list):
        rows = value
        total = len(rows)
    elif isinstance(value, dict):
        raw_items = value.get("items")
        rows = raw_items if isinstance(raw_items, list) else [value]
        for key in ("total_results", "count"):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool):
                total = count
                break
        else:
            if isinstance(raw_items, list):
                total = len(raw_items)
    else:
        rows = []
    if min_count is not None and total < min_count:
        return False
    if max_count is not None and total > max_count:
        return False
    if condition is None:
        return True
    if not isinstance(condition, dict):  # pragma: no cover - declaration validator owns this
        return False
    matches = [
        isinstance(row, dict)
        and all(row.get(key) == expected for key, expected in condition.items())
        for row in rows
    ]
    return bool(matches) and (all(matches) if condition_scope == "all" else any(matches))


def evaluate_procedure_resolution(
    activation: ResolutionContractActivationV1,
    resolution: ProcedureResolutionV1,
) -> ProcedureResolutionLawResultV1:
    """Enforce declaration-before-observation, clock, proof-kind, and expectation laws."""

    if (
        resolution.contract_id != activation.contract_id
        or resolution.subject != activation.subject
        or resolution.measurement_name != activation.measurement_name
    ):
        return _resolution_refused(
            "resolution.activation_mismatch",
            "Resolution does not name its exact accepted activation.",
        )
    if resolution.observed_at < activation.activated_at:
        return _resolution_refused(
            "resolution.predates_activation",
            "Resolution evidence predates the accepted declaration.",
        )
    if resolution.verdict == "satisfied" and resolution.observed_at < activation.check_at:
        return _resolution_refused(
            "resolution.before_check_at",
            "A satisfied resolution must be observed at or after check_at.",
        )
    measurement = activation.declaration.measurement
    required_proof_kind = {
        "accepted_query": "query_receipt",
        "claim_attestation": "claim_attestation",
        "claim_statement": "claim_statement",
    }[measurement.kind]
    if resolution.verdict in {"satisfied", "contradicted"} and not any(
        proof.kind == required_proof_kind for proof in resolution.evidence_refs
    ):
        return _resolution_refused(
            "resolution.measurement_proof_missing",
            f"Resolution requires {required_proof_kind!r} proof for the declared measurement.",
        )
    if isinstance(
        measurement,
        ClaimAttestationProcedureMeasurementV1 | ClaimStatementProcedureMeasurementV1,
    ):
        subject = measurement.claim_statement
        if any(
            proof.kind == required_proof_kind and proof.subject != subject
            for proof in resolution.evidence_refs
        ):
            return _resolution_refused(
                "resolution.measurement_subject_mismatch",
                "Resolution proof targets another Claim statement.",
            )
    if isinstance(measurement, ClaimStatementProcedureMeasurementV1):
        holds = isinstance(resolution.value, str) and (
            resolution.value in measurement.acceptable_verdicts
        )
    else:
        holds = _expectation_holds(
            measurement.expect,
            value=(None if resolution.value is None else normalize_canonical(resolution.value)),
            evidence_count=sum(
                proof.kind == required_proof_kind for proof in resolution.evidence_refs
            ),
        )
    if resolution.verdict == "satisfied" and not holds:
        return _resolution_refused(
            "resolution.expectation_not_satisfied",
            "Resolution value does not satisfy the declared expectation.",
        )
    if resolution.verdict == "contradicted" and holds:
        return _resolution_refused(
            "resolution.expectation_not_contradicted",
            "Resolution value satisfies the declaration and cannot be called contradicted.",
        )
    return ProcedureResolutionLawResultV1(
        verdict="accepted",
        resolution_digest=typed_digest(
            ArtifactDigest,
            "playbill-procedure-resolution-v1",
            {"resolution": resolution.model_dump(mode="json")},
        ).tagged,
    )


class ProcedureResolutionDispositionV1(_StrictResolutionModel):
    tag: Literal["playbill-procedure-resolution-disposition-v1"] = (
        "playbill-procedure-resolution-disposition-v1"
    )
    disposition_id: str
    resolution_id: str
    sequence: int = Field(ge=1)
    verdict: ResolutionDispositionVerdictV1
    reviewer_actor_context: GovernedActorContext
    recorded_at: datetime
    note: str | None = None

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("recorded_at", when_used="json")
    def _serialize_recorded_at(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _identifier(self) -> "ProcedureResolutionDispositionV1":
        if self.disposition_id != resolution_disposition_id(self):
            raise ValueError("resolution disposition_id does not reproduce")
        return self


def resolution_disposition_id(disposition: ProcedureResolutionDispositionV1) -> str:
    payload = disposition.model_dump(mode="json")
    payload.pop("tag", None)
    payload.pop("disposition_id", None)
    digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-resolution-disposition-v1",
        {"disposition": payload},
    ).value
    return f"RSD-{digest[:32]}"


def build_resolution_disposition(
    resolution: ProcedureResolutionV1,
    *,
    sequence: int,
    verdict: ResolutionDispositionVerdictV1,
    reviewer_actor_context: GovernedActorContext,
    recorded_at: datetime,
    note: str | None = None,
) -> ProcedureResolutionDispositionV1:
    provisional = ProcedureResolutionDispositionV1.model_construct(
        disposition_id="",
        resolution_id=resolution.resolution_id,
        sequence=sequence,
        verdict=verdict,
        reviewer_actor_context=reviewer_actor_context,
        recorded_at=ensure_utc(recorded_at),
        note=note,
    )
    return ProcedureResolutionDispositionV1.model_validate(
        provisional.model_copy(
            update={"disposition_id": resolution_disposition_id(provisional)}
        ).model_dump(mode="python")
    )


class ProcedureResolutionBook:
    """Replay-only latest-non-overturned lookup over verified journal records."""

    def __init__(
        self,
        activations: tuple[ResolutionContractActivationV1, ...],
    ) -> None:
        self.activations = {item.contract_id: item for item in activations}
        if len(self.activations) != len(activations):
            raise PlaybillExecutionError("duplicate ResolutionContract activation")
        self.resolutions: dict[str, list[ProcedureResolutionV1]] = defaultdict(list)
        self.dispositions: dict[str, list[ProcedureResolutionDispositionV1]] = defaultdict(list)

    def replay(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> None:
        self.resolutions.clear()
        self.dispositions.clear()
        access = BodyAccessContext(principal_id="procedure-resolution-replay", can_read_body=True)
        for stored in records:
            if stored.record.event_kind not in {"resolution", "resolution_disposition"}:
                continue
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            try:
                if stored.record.event_kind == "resolution":
                    resolution = ProcedureResolutionV1.model_validate(payload)
                    activation = self.activations.get(resolution.contract_id)
                    if activation is None:
                        raise PlaybillExecutionError(
                            "resolution names no accepted derived activation"
                        )
                    if (
                        resolution.subject != activation.subject
                        or resolution.measurement_name != activation.measurement_name
                        or stored.record.accepted_coordinate
                        != activation.subject.accepted_coordinate
                        or stored.record.procedure_artifact_digest
                        != activation.procedure_artifact_digest
                    ):
                        raise PlaybillExecutionError(
                            "resolution differs from its exact accepted activation"
                        )
                    law = evaluate_procedure_resolution(activation, resolution)
                    if law.verdict == "refused":
                        raise PlaybillExecutionError(
                            law.message or "resolution failed its accepted declaration"
                        )
                    prior = self.resolutions[resolution.contract_id]
                    if resolution.sequence != len(prior) + 1:
                        raise PlaybillExecutionError("resolution sequence is discontinuous")
                    if prior:
                        prior_dispositions = self.dispositions.get(prior[-1].resolution_id, ())
                        if not prior_dispositions or prior_dispositions[-1].verdict != "overturned":
                            raise PlaybillExecutionError(
                                "resolution contract is closed until its latest answer "
                                "is overturned"
                            )
                    prior.append(resolution)
                else:
                    disposition = ProcedureResolutionDispositionV1.model_validate(payload)
                    disposition_target = self.resolution_by_id(disposition.resolution_id)
                    if disposition_target is None:
                        raise PlaybillExecutionError("disposition names an absent resolution")
                    contract_resolutions = self.resolutions[disposition_target.contract_id]
                    if contract_resolutions[-1].resolution_id != disposition.resolution_id:
                        raise PlaybillExecutionError(
                            "an answered overturn cannot be revised on an older resolution"
                        )
                    prior_dispositions = self.dispositions[disposition.resolution_id]
                    if disposition.sequence != len(prior_dispositions) + 1:
                        raise PlaybillExecutionError(
                            "resolution disposition sequence is discontinuous"
                        )
                    prior_dispositions.append(disposition)
            except ValueError as exc:
                raise PlaybillExecutionError("resolution exhaust payload is invalid") from exc

    def resolution_by_id(self, resolution_id: str) -> ProcedureResolutionV1 | None:
        for values in self.resolutions.values():
            for resolution in values:
                if resolution.resolution_id == resolution_id:
                    return resolution
        return None

    def latest_non_overturned(self, contract_id: str) -> ProcedureResolutionV1 | None:
        for resolution in reversed(self.resolutions.get(contract_id, ())):
            dispositions = self.dispositions.get(resolution.resolution_id, ())
            if not dispositions or dispositions[-1].verdict != "overturned":
                return resolution
        return None


def append_procedure_resolution(
    writer: ProcedureExhaustWriter,
    *,
    activation: ResolutionContractActivationV1,
    resolution: ProcedureResolutionV1,
    stream: JournalStreamIdentityV1,
) -> StoredProcedureJournalRecordV1:
    partition_id = resolution_contract_partition_id(activation)
    law = evaluate_procedure_resolution(activation, resolution)
    if law.verdict == "refused":
        raise PlaybillExecutionError(law.message or "resolution law refused")
    existing = writer.journal.all_records(stream, partition_id)
    book = ProcedureResolutionBook((activation,))
    book.replay(existing, bodies=writer.bodies)
    prior = book.resolutions.get(activation.contract_id, ())
    if resolution.sequence != len(prior) + 1:
        raise PlaybillExecutionError("resolution sequence is discontinuous")
    if prior:
        dispositions = book.dispositions.get(prior[-1].resolution_id, ())
        if not dispositions or dispositions[-1].verdict != "overturned":
            raise PlaybillExecutionError(
                "resolution contract is closed until its latest answer is overturned"
            )
    return writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="resolution",
        accepted_coordinate=activation.subject.accepted_coordinate,
        procedure_artifact_digest=activation.procedure_artifact_digest,
        definition_digest=activation.definition_digest,
        actor_context=resolution.actor_context,
        recorded_at=resolution.recorded_at,
        payload=resolution.model_dump(mode="json"),
    )


def append_resolution_disposition(
    writer: ProcedureExhaustWriter,
    *,
    activation: ResolutionContractActivationV1,
    resolution: ProcedureResolutionV1,
    disposition: ProcedureResolutionDispositionV1,
    stream: JournalStreamIdentityV1,
) -> StoredProcedureJournalRecordV1:
    partition_id = resolution_contract_partition_id(activation)
    if disposition.resolution_id != resolution.resolution_id:
        raise PlaybillExecutionError("resolution disposition names another resolution")
    existing = writer.journal.all_records(stream, partition_id)
    book = ProcedureResolutionBook((activation,))
    book.replay(existing, bodies=writer.bodies)
    stored_resolution = book.resolution_by_id(resolution.resolution_id)
    if stored_resolution != resolution:
        raise PlaybillExecutionError("resolution disposition target is absent from exhaust")
    contract_resolutions = book.resolutions[resolution.contract_id]
    if contract_resolutions[-1].resolution_id != resolution.resolution_id:
        raise PlaybillExecutionError(
            "an answered overturn cannot be revised on an older resolution"
        )
    prior = book.dispositions.get(resolution.resolution_id, ())
    if disposition.sequence != len(prior) + 1:
        raise PlaybillExecutionError("resolution disposition sequence is discontinuous")
    return writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="resolution_disposition",
        accepted_coordinate=activation.subject.accepted_coordinate,
        procedure_artifact_digest=activation.procedure_artifact_digest,
        definition_digest=activation.definition_digest,
        actor_context=disposition.reviewer_actor_context,
        recorded_at=disposition.recorded_at,
        payload=disposition.model_dump(mode="json"),
    )


class AcceptedAuthorityBasisV1(_StrictResolutionModel):
    """One exact accepted authority candidate revalidated at verdict time."""

    tag: Literal["playbill-accepted-authority-basis-v1"] = "playbill-accepted-authority-basis-v1"
    kind: Literal["standing_mandate", "resolution"]
    basis_digest: str
    accepted_coordinate: AcceptedCoordinate
    valid_from: datetime
    valid_until: datetime
    current_artifact_digest: str
    artifact_digest: str
    resolution_verdict: ResolutionVerdictV1 | None = None
    resolution_overturned: bool = False
    accepted_promotion_digest: str | None = None

    @field_validator(
        "basis_digest",
        "current_artifact_digest",
        "artifact_digest",
        "accepted_promotion_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        return (
            None
            if value is None
            else _digest(value, label=str(getattr(info, "field_name", "authority digest")))
        )

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _validity(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _shape(self) -> "AcceptedAuthorityBasisV1":
        if self.valid_from >= self.valid_until:
            raise ValueError("authority basis requires a finite validity interval")
        if self.kind == "standing_mandate":
            if self.resolution_verdict is not None or self.accepted_promotion_digest is not None:
                raise ValueError("StandingMandate basis cannot claim resolution state")
        elif self.resolution_verdict is None or self.accepted_promotion_digest is None:
            raise ValueError("resolution basis requires verdict and accepted promotion")
        return self


def resolve_authority_basis(
    requested_basis_digests: tuple[str, ...],
    *,
    accepted_basis: Mapping[str, AcceptedAuthorityBasisV1],
    evaluation_time: datetime,
) -> tuple[str, ...]:
    """Return only exact, current, effective authority; absence never supports."""

    if requested_basis_digests != tuple(sorted(set(requested_basis_digests))):
        raise ValueError("requested authority basis digests must be sorted and unique")
    evaluation_time = ensure_utc(evaluation_time)
    resolved: list[str] = []
    for digest in requested_basis_digests:
        _digest(digest, label="requested authority basis")
        candidate = accepted_basis.get(digest)
        if candidate is None:
            continue
        if not candidate.valid_from <= evaluation_time < candidate.valid_until:
            continue
        if candidate.artifact_digest != candidate.current_artifact_digest:
            continue
        if candidate.kind == "resolution" and (
            candidate.resolution_verdict != "satisfied" or candidate.resolution_overturned
        ):
            continue
        resolved.append(candidate.basis_digest)
    return tuple(sorted(set(resolved)))


__all__ = [
    "AcceptedAuthorityBasisV1",
    "ProcedureProofReferenceV1",
    "ProcedureResolutionBook",
    "ProcedureResolutionDispositionV1",
    "ProcedureResolutionLawResultV1",
    "ProcedureResolutionV1",
    "ResolutionContractActivationV1",
    "ResolutionDispositionVerdictV1",
    "ResolutionSubjectV1",
    "ResolutionVerdictV1",
    "append_procedure_resolution",
    "append_resolution_disposition",
    "build_procedure_resolution",
    "build_resolution_disposition",
    "derive_resolution_activations",
    "evaluate_procedure_resolution",
    "procedure_resolution_id",
    "procedure_arm_content_digest",
    "resolution_activation_id",
    "resolution_contract_id",
    "resolution_contract_partition_id",
    "resolution_disposition_id",
    "resolve_authority_basis",
]
