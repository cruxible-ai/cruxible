"""Semantic ResolutionContract activation and append-only operational answers."""

from __future__ import annotations

import hashlib
import pickle
import re
import threading
import weakref
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

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
from cruxible_client.contracts.claims import claim_path, claim_statement_address
from cruxible_client.contracts.errors import ExecutionError, JournalConflictError
from cruxible_client.contracts.persistent import PersistentMap
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.measurements import (
    ClaimAttestationProcedureMeasurement,
    ClaimStatementProcedureMeasurement,
    ProcedureMeasurementDeclaration,
)
from cruxible_client.contracts.resolution_contracts import (
    InvestigationBinding,
    ResolutionContract,
    resolution_contract_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.exhaust import (
    JournalPartitionHeadV1,
    JournalStreamIdentityV1,
    ProcedureExhaustWriter,
    StoredProcedureJournalRecordV1,
)
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload, read_payload_bytes
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.graph_digests import cached_node_digests
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.storage.landing_log import LandingHead

ResolutionVerdictV1 = Literal["satisfied", "contradicted", "indeterminate"]
ResolutionSettlementVerdictV2 = Literal["satisfied", "contradicted"]
ResolutionDispositionVerdictV1 = Literal["upheld", "overturned"]

_OUTCOME_CLASS_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


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


class ResolutionClaimEndpointV1(_StrictResolutionModel):
    """One exact accepted Claim statement used by a settlement relation."""

    tag: Literal["playbill-resolution-claim-endpoint-v1"] = "playbill-resolution-claim-endpoint-v1"
    statement_address: SemanticAddress
    content_digest: str
    accepted_coordinate: AcceptedCoordinate

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        return _digest(value, label="resolution Claim endpoint content_digest")

    @model_validator(mode="after")
    def _claim_statement(self) -> "ResolutionClaimEndpointV1":
        if self.statement_address.selector.scheme != "claim-statement-v1":
            raise ValueError("resolution Claim endpoint must name an exact Claim statement")
        return self


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
    declaration: ProcedureMeasurementDeclaration
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


def resolution_contract_id(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV3,
) -> str:
    payload = activation.model_dump(mode="json")
    tag = payload.pop("tag", None)
    payload.pop("contract_id", None)
    payload.pop("activation_id", None)
    digest = typed_digest(
        ArtifactDigest,
        (
            "playbill-resolution-contract-v3"
            if tag == "playbill-resolution-contract-activation-v3"
            else "playbill-resolution-contract-v2"
            if tag == "playbill-resolution-contract-activation-v2"
            else "playbill-resolution-contract-v1"
        ),
        {"activation": payload},
    ).value
    return f"RSC-{digest[:32]}"


def resolution_activation_id(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV3,
) -> str:
    digest = typed_digest(
        ArtifactDigest,
        activation.tag,
        {
            "contract_id": activation.contract_id,
            "accepted_coordinate": activation.subject.accepted_coordinate.model_dump(mode="json"),
        },
    ).value
    return f"RSA-{digest[:32]}"


def resolution_activation_digest(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV3,
) -> str:
    """Commit one complete activation without changing either historical ID law."""

    return typed_digest(
        ArtifactDigest,
        activation.tag,
        {"activation": activation.model_dump(mode="json")},
    ).tagged


class ResolutionContractActivationV2(ResolutionContractActivationV1):
    """Activation successor committing a prediction and its correctness law."""

    tag: Literal["playbill-resolution-contract-activation-v2"] = (
        "playbill-resolution-contract-activation-v2"  # type: ignore[assignment]
    )
    prediction: ResolutionClaimEndpointV1
    outcome_class: str
    correctness_condition: object

    @field_validator("outcome_class")
    @classmethod
    def _outcome_class(cls, value: str) -> str:
        if not _OUTCOME_CLASS_RE.fullmatch(value):
            raise ValueError("resolution outcome_class must be a stable canonical identifier")
        return value

    @field_validator("correctness_condition", mode="before")
    @classmethod
    def _correctness_condition(cls, value: object) -> CanonicalValue:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict) or not normalized:
            raise ValueError("resolution correctness_condition must be a nonempty canonical object")
        return normalized

    @model_validator(mode="after")
    def _prediction_coordinate(self) -> "ResolutionContractActivationV2":
        if self.prediction.accepted_coordinate != self.subject.accepted_coordinate:
            raise ValueError(
                "resolution prediction and activation must name the same accepted coordinate"
            )
        return self


class ResolutionContractActivationV3(_StrictResolutionModel):
    """An exact governed test and observation window, with no owning Procedure."""

    tag: Literal["playbill-resolution-contract-activation-v3"] = (
        "playbill-resolution-contract-activation-v3"
    )
    contract_id: str
    activation_id: str
    contract: ResolutionContract
    investigation: InvestigationBinding
    activated_at: datetime

    @field_validator("activated_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("activated_at", when_used="json")
    def _serialize(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _binding(self) -> "ResolutionContractActivationV3":
        if self.contract.lifecycle.state != "live":
            raise ValueError("resolution activation requires a live contract version")
        ref = self.investigation.contract
        if (
            self.contract.identity != ref.identity
            or resolution_contract_digest(self.contract).tagged != ref.artifact_digest
            or self.contract.hypothesis != self.investigation.hypothesis
        ):
            raise ValueError("resolution activation does not reproduce its governed contract")
        if self.contract_id != resolution_contract_id(
            self
        ) or self.activation_id != resolution_activation_id(self):
            raise ValueError("resolution activation identity does not reproduce")
        return self

    @property
    def subject(self) -> ResolutionSubjectV1:
        hypothesis = self.investigation.hypothesis
        return ResolutionSubjectV1(
            address=claim_statement_address(claim_path(hypothesis.identity.name)),
            content_digest=hypothesis.statement_digest,
            accepted_coordinate=hypothesis.coordinate,
        )

    @property
    def prediction(self) -> ResolutionClaimEndpointV1:
        return ResolutionClaimEndpointV1(
            statement_address=self.subject.address,
            content_digest=self.subject.content_digest,
            accepted_coordinate=self.subject.accepted_coordinate,
        )

    @property
    def procedure_artifact_digest(self) -> None:
        return None

    @property
    def definition_digest(self) -> str:
        return self.investigation.contract.artifact_digest

    @property
    def measurement_name(self) -> str:
        return self.contract.identity.name

    @property
    def outcome_class(self) -> str:
        return self.contract.outcome_class

    @property
    def correctness_condition(self) -> object:
        return self.contract.rule.model_dump(mode="json")

    @property
    def check_at(self) -> datetime:
        return self.investigation.window.starts_at

    @property
    def expires_at(self) -> datetime:
        return self.investigation.window.ends_at


def build_independent_activation(
    contract: ResolutionContract, investigation: InvestigationBinding, *, activated_at: datetime
) -> ResolutionContractActivationV3:
    provisional = ResolutionContractActivationV3.model_construct(
        contract=contract,
        investigation=investigation,
        activated_at=ensure_utc(activated_at),
        contract_id="",
        activation_id="",
    )
    bound = provisional.model_copy(update={"contract_id": resolution_contract_id(provisional)})
    return ResolutionContractActivationV3.model_validate(
        bound.model_copy(update={"activation_id": resolution_activation_id(bound)}).model_dump()
    )


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
    accepted: AcceptedProcedure,
    *,
    accepted_coordinate: AcceptedCoordinate,
    activated_at: datetime,
) -> tuple[ResolutionContractActivationV1, ...]:
    """Derive every activation without a mutable open-contract transaction."""

    activated_at = ensure_utc(activated_at)
    definition = accepted.procedure.definition
    node_digests = cached_node_digests(
        definition,
        definition_digest=accepted.procedure.definition_digest,
    )
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
                raise ExecutionError("node measurement lacks node_id")
            node_local = node_digests[node_id].local_digest
            address = SemanticAddress.procedure_node(accepted.path, node_id)
            content_digest = node_local
        else:
            node_id = declaration.node_id
            from_node_id = declaration.from_node_id
            arm_label = declaration.arm_label
            if node_id is None or from_node_id is None or arm_label is None:  # pragma: no cover
                raise ExecutionError("arm measurement lacks an endpoint")
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


def build_resolution_contract_activation_v2(
    activation: ResolutionContractActivationV1,
    *,
    prediction: ResolutionClaimEndpointV1,
    outcome_class: str,
    correctness_condition: object,
) -> ResolutionContractActivationV2:
    """Successor one derived activation with a pre-observation settlement contract."""

    values = {
        name: getattr(activation, name)
        for name in ResolutionContractActivationV1.model_fields
        if name not in {"tag", "contract_id", "activation_id"}
    }
    provisional = ResolutionContractActivationV2.model_construct(
        **values,
        contract_id="",
        activation_id="",
        prediction=prediction,
        outcome_class=outcome_class,
        correctness_condition=correctness_condition,
    )
    contract_id = resolution_contract_id(provisional)
    with_contract = provisional.model_copy(update={"contract_id": contract_id})
    return ResolutionContractActivationV2.model_validate(
        with_contract.model_copy(
            update={"activation_id": resolution_activation_id(with_contract)}
        ).model_dump(mode="python")
    )


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
        resolution.tag,
        {"resolution": payload},
    ).value
    return f"RSR-{digest[:32]}"


def procedure_resolution_digest(resolution: ProcedureResolutionV1) -> str:
    return typed_digest(
        ArtifactDigest,
        resolution.tag,
        {"resolution": resolution.model_dump(mode="json")},
    ).tagged


class ProcedureResolutionV2(ProcedureResolutionV1):
    """Settlement successor naming the exact outcome Claim and mechanical result."""

    tag: Literal["playbill-procedure-resolution-v2"] = "playbill-procedure-resolution-v2"  # type: ignore[assignment]
    verdict: ResolutionSettlementVerdictV2
    settlement: ResolutionClaimEndpointV1
    settlement_outcome: bool

    @model_validator(mode="after")
    def _settlement_proof(self) -> "ProcedureResolutionV2":
        if not self.evidence_refs:
            raise ValueError("settlement resolution requires at least one exact proof reference")
        if any(proof.kind == "claim_attestation" for proof in self.evidence_refs):
            raise ValueError("Claim attestations cannot prove a mechanical settlement outcome")
        return self


class ProcedureResolutionV3(ProcedureResolutionV2):
    """Same mechanical evidence, bound to an independent governed contract."""

    tag: Literal["playbill-resolution-v3"] = "playbill-resolution-v3"  # type: ignore[assignment]


def resolution_contract_partition_id(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV3,
) -> str:
    """Return the sole journal partition for one derived contract activation."""

    return f"resolutions:{activation.contract_id}"


def resolution_event_accepted_coordinate(
    activation: ResolutionContractActivationV1
    | ResolutionContractActivationV2
    | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV1 | ProcedureResolutionV2,
) -> AcceptedCoordinate:
    """Bind v1 history as frozen and v2 events to their exact settlement coordinate."""

    if isinstance(
        activation, (ResolutionContractActivationV2, ResolutionContractActivationV3)
    ) and isinstance(resolution, ProcedureResolutionV2):
        return resolution.settlement.accepted_coordinate
    return activation.subject.accepted_coordinate


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


def build_procedure_resolution_v2(
    activation: ResolutionContractActivationV2 | ResolutionContractActivationV3,
    *,
    sequence: int,
    verdict: ResolutionSettlementVerdictV2,
    settlement: ResolutionClaimEndpointV1,
    settlement_outcome: bool,
    value: object | None,
    evidence_refs: tuple[ProcedureProofReferenceV1, ...],
    observed_at: datetime,
    recorded_at: datetime,
    actor_context: GovernedActorContext,
    note: str | None = None,
) -> ProcedureResolutionV2:
    model = (
        ProcedureResolutionV3
        if isinstance(activation, ResolutionContractActivationV3)
        else ProcedureResolutionV2
    )
    provisional = model.model_construct(
        resolution_id="",
        contract_id=activation.contract_id,
        sequence=sequence,
        subject=activation.subject,
        measurement_name=activation.measurement_name,
        verdict=verdict,
        settlement=settlement,
        settlement_outcome=settlement_outcome,
        value=value,
        evidence_refs=evidence_refs,
        observed_at=ensure_utc(observed_at),
        recorded_at=ensure_utc(recorded_at),
        actor_context=actor_context,
        note=note,
    )
    return model.model_validate(
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


def _prediction_decimal(value: object) -> Decimal | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, dict) and tuple(value) == ("$decimal",):
        spelling = value.get("$decimal")
        if isinstance(spelling, str):
            try:
                return Decimal(spelling)
            except InvalidOperation:
                return None
    return None


def evaluate_prediction_correctness_condition(
    condition: object,
    *,
    prediction_value: object,
    settlement_value: object,
    evidence_present: bool,
) -> bool | None:
    """Evaluate the closed served equality/threshold/presence rule vocabulary."""

    if not isinstance(condition, dict):
        return None
    operator = condition.get("operator")
    if operator == "equality":
        return normalize_canonical(prediction_value) == normalize_canonical(settlement_value)
    if operator == "presence":
        return prediction_value is evidence_present if isinstance(prediction_value, bool) else None
    if operator != "threshold" or not isinstance(prediction_value, bool):
        return None
    observed = _prediction_decimal(settlement_value)
    threshold = _prediction_decimal(condition.get("threshold"))
    comparison = condition.get("comparison")
    if not isinstance(comparison, str):
        return None
    if observed is None or threshold is None:
        return None
    actual = {
        "gt": observed > threshold,
        "gte": observed >= threshold,
        "lt": observed < threshold,
        "lte": observed <= threshold,
    }.get(comparison)
    return None if actual is None else actual == prediction_value


def _evaluate_served_prediction_settlement(
    activation: ResolutionContractActivationV2 | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV2,
) -> ProcedureResolutionLawResultV1 | None:
    value = resolution.value
    if not isinstance(value, dict) or value.get("tag") != (
        "playbill-prediction-settlement-value-v1"
    ):
        return None
    evidence_kind = value.get("evidence_kind")
    if not isinstance(evidence_kind, str):
        return _resolution_refused(
            "resolution.prediction_value_invalid",
            "Served prediction settlement must name its evidence kind.",
        )
    required_proof = {
        "observation_claim": "claim_statement",
        "terminal": "run_receipt",
    }.get(evidence_kind)
    if required_proof is None:
        return _resolution_refused(
            "resolution.prediction_value_invalid",
            "Served prediction settlement names an unknown evidence kind.",
        )
    proofs = tuple(proof for proof in resolution.evidence_refs if proof.kind == required_proof)
    if not proofs:
        return _resolution_refused(
            "resolution.measurement_proof_missing",
            f"Served prediction settlement requires {required_proof!r} proof.",
        )
    if required_proof == "claim_statement" and any(
        proof.subject != resolution.settlement.statement_address for proof in proofs
    ):
        return _resolution_refused(
            "resolution.measurement_subject_mismatch",
            "Settlement proof targets another Claim statement.",
        )
    if resolution.observed_at > activation.expires_at:
        return _resolution_refused(
            "resolution.prediction_deadline_passed",
            "Settlement evidence falls outside the prediction validity window.",
        )
    outcome = evaluate_prediction_correctness_condition(
        activation.correctness_condition,
        prediction_value=value.get("prediction_value"),
        settlement_value=value.get("settlement_value"),
        evidence_present=value.get("evidence_present") is True,
    )
    if outcome is None:
        return _resolution_refused(
            "resolution.prediction_unsettleable_rule",
            "Prediction correctness rule cannot evaluate these canonical values.",
        )
    if outcome != resolution.settlement_outcome:
        return _resolution_refused(
            "resolution.settlement_evidence_mismatch",
            "Settlement outcome does not reproduce from its correctness rule and evidence.",
        )
    return ProcedureResolutionLawResultV1(
        verdict="accepted",
        resolution_digest=procedure_resolution_digest(resolution),
    )


def evaluate_procedure_resolution(
    activation: ResolutionContractActivationV1 | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV1,
) -> ProcedureResolutionLawResultV1:
    """Enforce declaration-before-observation, clock, proof-kind, and expectation laws."""

    if isinstance(activation, ResolutionContractActivationV3):
        if not isinstance(resolution, ProcedureResolutionV3):
            return _resolution_refused(
                "resolution.version_mismatch",
                "Independent contracts require an independent settlement.",
            )
        if (
            resolution.contract_id != activation.contract_id
            or resolution.subject != activation.subject
            or resolution.measurement_name != activation.measurement_name
        ):
            return _resolution_refused(
                "resolution.activation_mismatch",
                "Settlement must bind its exact governed test and window.",
            )
        if (
            resolution.observed_at <= activation.activated_at
            or not activation.check_at <= resolution.observed_at <= activation.expires_at
        ):
            return _resolution_refused(
                "resolution.outside_window",
                "Evidence must follow contract acceptance and lie within its fixed "
                "observation window.",
            )
        result = _evaluate_served_prediction_settlement(activation, resolution)
        return result or _resolution_refused(
            "resolution.prediction_value_invalid",
            "Independent settlement requires mechanical observation evidence.",
        )
    if isinstance(resolution, ProcedureResolutionV3):
        return _resolution_refused(
            "resolution.version_mismatch",
            "Independent settlement cannot reinterpret a Procedure measurement.",
        )
    activation_is_v2 = isinstance(activation, ResolutionContractActivationV2)
    resolution_is_v2 = isinstance(resolution, ProcedureResolutionV2)
    if activation_is_v2 != resolution_is_v2:
        return _resolution_refused(
            "resolution.version_mismatch",
            "Resolution and accepted activation must use the same wire generation.",
        )
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
    if resolution_is_v2 and resolution.observed_at == activation.activated_at:
        return _resolution_refused(
            "resolution.settlement_not_after_activation",
            "Settlement observation must follow activation of its correctness contract.",
        )
    if resolution.verdict == "satisfied" and resolution.observed_at < activation.check_at:
        return _resolution_refused(
            "resolution.before_check_at",
            "A satisfied resolution must be observed at or after check_at.",
        )
    if isinstance(activation, ResolutionContractActivationV2) and isinstance(
        resolution, ProcedureResolutionV2
    ):
        served = _evaluate_served_prediction_settlement(activation, resolution)
        if served is not None:
            return served
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
        ClaimAttestationProcedureMeasurement | ClaimStatementProcedureMeasurement,
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
    if isinstance(measurement, ClaimStatementProcedureMeasurement):
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
        resolution_digest=procedure_resolution_digest(resolution),
    )


class SettledOutcomeRelationV1(_StrictResolutionModel):
    """Reconstructable committed pair from prediction activation to settlement."""

    tag: Literal["playbill-settled-outcome-relation-v1"] = "playbill-settled-outcome-relation-v1"
    activation: ResolutionContractActivationV2
    resolution: ProcedureResolutionV2
    activation_digest: str
    resolution_digest: str
    relation_digest: str

    @field_validator("activation_digest", "resolution_digest", "relation_digest")
    @classmethod
    def _digests(cls, value: str, info: object) -> str:
        return _digest(value, label=str(getattr(info, "field_name", "relation digest")))

    @model_validator(mode="after")
    def _reproduce(self) -> "SettledOutcomeRelationV1":
        if self.activation_digest != resolution_activation_digest(self.activation):
            raise ValueError("settled relation activation digest does not reproduce")
        if self.resolution_digest != procedure_resolution_digest(self.resolution):
            raise ValueError("settled relation resolution digest does not reproduce")
        law = evaluate_procedure_resolution(self.activation, self.resolution)
        if law.verdict != "accepted":
            raise ValueError("settled relation pair fails its ResolutionContract law")
        if self.relation_digest != settled_outcome_relation_digest(
            self.activation_digest,
            self.resolution_digest,
        ):
            raise ValueError("settled outcome relation digest does not reproduce")
        return self


class SettledOutcomeRelationV2(SettledOutcomeRelationV1):
    tag: Literal["playbill-settled-outcome-relation-v2"] = "playbill-settled-outcome-relation-v2"  # type: ignore[assignment]
    activation: ResolutionContractActivationV3  # type: ignore[assignment]
    resolution: ProcedureResolutionV3


def settled_outcome_relation_digest(activation_digest: str, resolution_digest: str) -> str:
    _digest(activation_digest, label="settled relation activation digest")
    _digest(resolution_digest, label="settled relation resolution digest")
    return typed_digest(
        ArtifactDigest,
        "playbill-settled-outcome-relation-v1",
        {
            "activation_digest": activation_digest,
            "resolution_digest": resolution_digest,
        },
    ).tagged


def build_settled_outcome_relation(
    activation: ResolutionContractActivationV2 | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV2,
) -> SettledOutcomeRelationV1 | SettledOutcomeRelationV2:
    activation_digest = resolution_activation_digest(activation)
    resolution_digest = procedure_resolution_digest(resolution)
    model = (
        SettledOutcomeRelationV2
        if isinstance(activation, ResolutionContractActivationV3)
        else SettledOutcomeRelationV1
    )
    return model.model_validate(
        dict(
            activation=activation,
            resolution=resolution,
            activation_digest=activation_digest,
            resolution_digest=resolution_digest,
            relation_digest=settled_outcome_relation_digest(
                activation_digest,
                resolution_digest,
            ),
        )
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


_RESOLUTION_EVENTS = frozenset({"resolution", "resolution_disposition"})
_REPLAY_ACCESS = BodyAccessContext(principal_id="procedure-resolution-replay", can_read_body=True)

_Activation = (
    ResolutionContractActivationV1 | ResolutionContractActivationV2 | ResolutionContractActivationV3
)


def _parse_resolution(payload: object) -> ProcedureResolutionV1 | ProcedureResolutionV2:
    if not isinstance(payload, dict):
        raise ValueError("resolution payload must be an object")
    if payload.get("tag") == "playbill-procedure-resolution-v1":
        return ProcedureResolutionV1.model_validate(payload)
    if payload.get("tag") == "playbill-resolution-v3":
        return ProcedureResolutionV3.model_validate(payload)
    if payload.get("tag") == "playbill-procedure-resolution-v2":
        return ProcedureResolutionV2.model_validate(payload)
    raise ValueError("resolution payload has an unknown version tag")


def _check_resolution(
    stored: StoredProcedureJournalRecordV1,
    resolution: ProcedureResolutionV1 | ProcedureResolutionV2,
    activation: _Activation | None,
) -> None:
    """One resolution record against its exact accepted activation and law."""
    if activation is None:
        raise ExecutionError("resolution names no accepted derived activation")
    if (
        resolution.subject != activation.subject
        or resolution.measurement_name != activation.measurement_name
        or stored.record.accepted_coordinate
        != resolution_event_accepted_coordinate(activation, resolution)
        or stored.record.procedure_artifact_digest != activation.procedure_artifact_digest
        or stored.record.partition_id != resolution_contract_partition_id(activation)
    ):
        raise ExecutionError("resolution differs from its exact accepted activation")
    law = evaluate_procedure_resolution(activation, resolution)
    if law.verdict == "refused":
        raise ExecutionError(law.message or "resolution failed its accepted declaration")


def _check_disposition(
    stored: StoredProcedureJournalRecordV1,
    target: ProcedureResolutionV1 | ProcedureResolutionV2,
    activation: _Activation,
) -> None:
    """One disposition record against its target resolution's contract partition."""
    if (
        stored.record.partition_id != resolution_contract_partition_id(activation)
        or stored.record.accepted_coordinate
        != resolution_event_accepted_coordinate(activation, target)
        or stored.record.procedure_artifact_digest != activation.procedure_artifact_digest
    ):
        raise ExecutionError("resolution disposition differs from its contract partition")


class ProcedureResolutionBook:
    """Replay-only latest-non-overturned lookup over verified journal records."""

    def __init__(
        self,
        activations: tuple[
            ResolutionContractActivationV1
            | ResolutionContractActivationV2
            | ResolutionContractActivationV3,
            ...,
        ],
    ) -> None:
        self.activations = {item.contract_id: item for item in activations}
        if len(self.activations) != len(activations):
            raise ExecutionError("duplicate ResolutionContract activation")
        self.unavailable: tuple[UnavailablePayload, ...] = ()
        self.resolutions: dict[str, list[ProcedureResolutionV1 | ProcedureResolutionV2]] = (
            defaultdict(list)
        )
        self.dispositions: dict[str, list[ProcedureResolutionDispositionV1]] = defaultdict(list)

    def replay(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> None:
        self.resolutions.clear()
        self.dispositions.clear()
        self.unavailable = ()
        for stored in records:
            if stored.record.event_kind not in _RESOLUTION_EVENTS:
                continue
            payload = read_payload(bodies, stored.record.payload_digest, access=_REPLAY_ACCESS)
            if isinstance(payload, UnavailablePayload):
                self.unavailable = (payload,)
                self.resolutions.clear()
                self.dispositions.clear()
                return
            try:
                if stored.record.event_kind == "resolution":
                    resolution = _parse_resolution(payload)
                    _check_resolution(
                        stored, resolution, self.activations.get(resolution.contract_id)
                    )
                    prior = self.resolutions[resolution.contract_id]
                    if resolution.sequence != len(prior) + 1:
                        raise ExecutionError("resolution sequence is discontinuous")
                    if prior:
                        prior_dispositions = self.dispositions.get(prior[-1].resolution_id, ())
                        if not prior_dispositions or prior_dispositions[-1].verdict != "overturned":
                            raise ExecutionError(
                                "resolution contract is closed until its latest answer "
                                "is overturned"
                            )
                    prior.append(resolution)
                else:
                    disposition = ProcedureResolutionDispositionV1.model_validate(payload)
                    disposition_target = self.resolution_by_id(disposition.resolution_id)
                    if disposition_target is None:
                        raise ExecutionError("disposition names an absent resolution")
                    _check_disposition(
                        stored,
                        disposition_target,
                        self.activations[disposition_target.contract_id],
                    )
                    contract_resolutions = self.resolutions[disposition_target.contract_id]
                    if contract_resolutions[-1].resolution_id != disposition.resolution_id:
                        raise ExecutionError(
                            "an answered overturn cannot be revised on an older resolution"
                        )
                    prior_dispositions = self.dispositions[disposition.resolution_id]
                    if disposition.sequence != len(prior_dispositions) + 1:
                        raise ExecutionError("resolution disposition sequence is discontinuous")
                    prior_dispositions.append(disposition)
            except ValueError as exc:
                raise ExecutionError("resolution exhaust payload is invalid") from exc

    def resolution_by_id(
        self, resolution_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None:
        for values in self.resolutions.values():
            for resolution in values:
                if resolution.resolution_id == resolution_id:
                    return resolution
        return None

    def latest_non_overturned(
        self, contract_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None:
        for resolution in reversed(self.resolutions.get(contract_id, ())):
            dispositions = self.dispositions.get(resolution.resolution_id, ())
            if not dispositions or dispositions[-1].verdict != "overturned":
                return resolution
        return None


class ResolutionLookup(Protocol):
    """What a reading law asks of a resolution history."""

    def latest_non_overturned(
        self, contract_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None: ...


@dataclass(frozen=True)
class _ResolutionFold:
    """An immutable fold of one partition's resolution events, through one journal head.

    It answers what ``ProcedureResolutionBook.replay`` of the same records answers,
    from maintained state: per contract the resolution count and latest
    resolution, per resolution its disposition count and last verdict, keyed
    lookups by resolution ID. Values are kept pickled and every read hands out
    a fresh copy, so no caller can change what a later one sees. Every map is
    persistent: extending by new records shares the rest. Payload availability is
    maintained separately from sequenced CAS changes, indexed by affected book.

    Why the latest non-overturned resolution is the contract's last one or
    none: a later resolution is admitted only once its predecessor's last
    disposition is an overturn, and a disposition may only name the contract's
    last resolution, so every earlier resolution stays overturned.
    """

    head_sequence: int
    head_digest: str | None
    counts: PersistentMap[int]
    last: PersistentMap[str]
    resolutions: PersistentMap[tuple[str, bytes]]
    dispositions: PersistentMap[tuple[int, str, bytes]]
    records: PersistentMap[bytes]
    event_kinds: frozenset[str]
    review_counts: PersistentMap[int]
    reviews: PersistentMap[bytes]
    unavailable: tuple[UnavailablePayload, ...] = ()

    @classmethod
    def empty(cls) -> _ResolutionFold:
        return cls(
            0,
            None,
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            PersistentMap(),
            frozenset(),
            review_counts=PersistentMap(),
            reviews=PersistentMap(),
        )

    def extend(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        activations: Mapping[str, _Activation],
        bodies: ContentAddressedBodyStore,
        head: JournalPartitionHeadV1 | None,
    ) -> _ResolutionFold:
        """This fold with ``records`` (the ones after its head, in order) folded on."""
        if self.unavailable:
            return replace(
                self,
                head_sequence=0 if head is None else head.sequence,
                head_digest=None if head is None else head.record_digest,
            )
        counts, last, resolutions = self.counts, self.last, self.resolutions
        review_counts, reviews = self.review_counts, self.reviews
        dispositions, kept, kinds = (
            self.dispositions,
            self.records,
            self.event_kinds,
        )
        for stored in records:
            kind = stored.record.event_kind
            if kind not in kinds:
                kinds = kinds | {kind}
            if kind not in _RESOLUTION_EVENTS:
                continue
            digest = stored.record.payload_digest
            payload = read_payload(bodies, digest, access=_REPLAY_ACCESS)
            if isinstance(payload, UnavailablePayload):
                return replace(
                    _ResolutionFold.empty(),
                    head_sequence=0 if head is None else head.sequence,
                    head_digest=None if head is None else head.record_digest,
                    unavailable=(payload,),
                )
            try:
                if kind == "resolution":
                    resolution = _parse_resolution(payload)
                    contract_id = resolution.contract_id
                    _check_resolution(stored, resolution, activations.get(contract_id))
                    count = counts.get(contract_id, 0)
                    if resolution.sequence != count + 1:
                        raise ExecutionError("resolution sequence is discontinuous")
                    if count:
                        state = dispositions.get(last[contract_id])
                        if state is None or state[1] != "overturned":
                            raise ExecutionError(
                                "resolution contract is closed until its latest answer "
                                "is overturned"
                            )
                    counts = counts.set(contract_id, count + 1)
                    last = last.set(contract_id, resolution.resolution_id)
                    if resolution.resolution_id not in resolutions:
                        resolutions = resolutions.set(
                            resolution.resolution_id, (contract_id, _frozen(resolution))
                        )
                    if resolution.resolution_id not in kept:
                        kept = kept.set(resolution.resolution_id, _frozen(stored))
                else:
                    disposition = ProcedureResolutionDispositionV1.model_validate(payload)
                    entry = resolutions.get(disposition.resolution_id)
                    if entry is None:
                        raise ExecutionError("disposition names an absent resolution")
                    target = pickle.loads(entry[1])
                    _check_disposition(stored, target, activations[target.contract_id])
                    if last.get(target.contract_id) != disposition.resolution_id:
                        raise ExecutionError(
                            "an answered overturn cannot be revised on an older resolution"
                        )
                    count = dispositions.get(disposition.resolution_id, (0, "", b""))[0]
                    if disposition.sequence != count + 1:
                        raise ExecutionError("resolution disposition sequence is discontinuous")
                    dispositions = dispositions.set(
                        disposition.resolution_id,
                        (count + 1, disposition.verdict, _frozen(disposition)),
                    )
                    review_sequence = review_counts.get(target.contract_id, 0) + 1
                    review_counts = review_counts.set(target.contract_id, review_sequence)
                    reviews = reviews.set(
                        f"{target.contract_id}:{review_sequence}", _frozen(disposition)
                    )
            except ValueError as exc:
                raise ExecutionError("resolution exhaust payload is invalid") from exc
        return _ResolutionFold(
            head_sequence=0 if head is None else head.sequence,
            head_digest=None if head is None else head.record_digest,
            counts=counts,
            last=last,
            resolutions=resolutions,
            dispositions=dispositions,
            records=kept,
            event_kinds=kinds,
            review_counts=review_counts,
            reviews=reviews,
        )


def _frozen(value: object) -> bytes:
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


class ResolutionBookView:
    """Read-only answers of one partition's resolution fold; every value is a detached copy."""

    def __init__(self, fold: _ResolutionFold) -> None:
        self._fold = fold

    @property
    def unavailable(self) -> tuple[UnavailablePayload, ...]:
        return self._fold.unavailable

    def resolution_count(self, contract_id: str) -> int:
        return self._fold.counts.get(contract_id, 0)

    def latest_resolution(
        self, contract_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None:
        resolution_id = self._fold.last.get(contract_id)
        return None if resolution_id is None else self.resolution_by_id(resolution_id)

    def disposition_count(self, resolution_id: str) -> int:
        return self._fold.dispositions.get(resolution_id, (0, "", b""))[0]

    def last_disposition_verdict(self, resolution_id: str) -> str | None:
        state = self._fold.dispositions.get(resolution_id)
        return None if state is None else state[1]

    def last_disposition(self, resolution_id: str) -> ProcedureResolutionDispositionV1 | None:
        state = self._fold.dispositions.get(resolution_id)
        return None if state is None else pickle.loads(state[2])

    def review_count(self, contract_id: str) -> int:
        return self._fold.review_counts.get(contract_id, 0)

    def review_history(
        self, contract_id: str, *, limit: int
    ) -> tuple[ProcedureResolutionDispositionV1, ...]:
        count = self.review_count(contract_id)
        return tuple(
            pickle.loads(self._fold.reviews[f"{contract_id}:{sequence}"])
            for sequence in range(count, max(0, count - limit), -1)
        )

    def resolution_by_id(
        self, resolution_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None:
        entry = self._fold.resolutions.get(resolution_id)
        return None if entry is None else pickle.loads(entry[1])

    def latest_non_overturned(
        self, contract_id: str
    ) -> ProcedureResolutionV1 | ProcedureResolutionV2 | None:
        resolution_id = self._fold.last.get(contract_id)
        if resolution_id is None or self.last_disposition_verdict(resolution_id) == "overturned":
            return None
        return self.resolution_by_id(resolution_id)

    def resolution_record(self, resolution_id: str) -> StoredProcedureJournalRecordV1 | None:
        frozen = self._fold.records.get(resolution_id)
        return None if frozen is None else pickle.loads(frozen)

    def has_event_kind(self, kind: str) -> bool:
        return kind in self._fold.event_kinds


@dataclass(eq=False)
class _CachedResolutionFold:
    fold: _ResolutionFold
    bodies: _ResolutionBodyChanges
    changed: set[str] = field(default_factory=set)
    blocked: dict[str, UnavailablePayload] = field(default_factory=dict)
    subscriptions: dict[str, weakref.WeakSet[_CachedResolutionFold]] = field(default_factory=dict)


class _ResolutionBodyChanges:
    """One CAS suffix cursor, with payload-to-book subscriptions.

    A clean request reads only the landing head. Each new completion visits
    only books using that digest; each such book rechecks only changed inputs.
    Weak subscriptions expire with entries in the bounded book cache.
    """

    def __init__(self, head: LandingHead) -> None:
        self.head = head
        self.users: weakref.WeakValueDictionary[str, weakref.WeakSet[_CachedResolutionFold]] = (
            weakref.WeakValueDictionary()
        )

    def catch_up(self, bodies: ContentAddressedBodyStore, head: LandingHead) -> None:
        if head.sequence == self.head.sequence:
            return
        for row in bodies.landings.after(
            self.head.sequence, through=head.sequence, identity=head.identity
        ):
            if not row.intent:
                for book in self.users.get(row.digest, ()):
                    book.changed.add(row.digest)
        self.head = head

    def subscribe(
        self,
        cached: _CachedResolutionFold,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        bodies: ContentAddressedBodyStore,
    ) -> None:
        for stored in records:
            if stored.record.event_kind not in _RESOLUTION_EVENTS:
                continue
            digest = stored.record.payload_digest
            # Journal payloads initially bypass the evidence landing log. Enrol
            # each newly consumed input once, so byte verification/sweeps can
            # publish damage and repairs for it as for other retained bodies.
            log = bodies.landings
            head = log.head()
            if log.last_completion(digest, identity=head.identity, sequence=head.sequence) is None:
                log.ensure_landed(digest)
                head = log.head()
            # Consume enrolment before subscribing the new input: its verified
            # fold read below covers this completion, without a second read on
            # the next clean request. Existing subscribers still see changes.
            self.catch_up(bodies, head)
            users = self.users.setdefault(digest, weakref.WeakSet())
            users.add(cached)
            cached.subscriptions[digest] = users


_BOOK_CAPACITY = 256
_BOOKS: OrderedDict[tuple[object, ...], _CachedResolutionFold] = OrderedDict()
_BODY_CHANGES: OrderedDict[str, _ResolutionBodyChanges] = OrderedDict()
_BOOKS_LOCK = threading.Lock()


def resolution_book_at_head(
    journal: object,
    stream: JournalStreamIdentityV1,
    activation: _Activation,
    *,
    bodies: ContentAddressedBodyStore,
) -> tuple[ResolutionBookView, JournalPartitionHeadV1 | None]:
    """Incremental journal fold with change-driven CAS availability.

    Cold books validate the whole partition. Warm books consume journal and
    landing-log suffixes only. CAS completions invalidate just their subscribed
    books/inputs; a damaged book stays unavailable until repair, when a failed
    cold fold may be rebuilt. No request stats the retained payload population.
    """
    partition_id = resolution_contract_partition_id(activation)
    activations = {activation.contract_id: activation}
    read_head = getattr(journal, "read_head", None)
    select = getattr(journal, "select_records", None)
    all_records = getattr(journal, "all_records")
    if read_head is None or select is None:
        whole = _ResolutionFold.empty().extend(
            tuple(all_records(stream, partition_id)),
            activations=activations,
            bodies=bodies,
            head=None,
        )
        return ResolutionBookView(whole), None
    key = (
        str(getattr(journal, "root", id(journal))),
        str(bodies.root),
        stream.instance_id,
        stream.journal_family,
        stream.stream_id,
        partition_id,
        hashlib.sha256(canonical_bytes(activation.model_dump(mode="json"))).hexdigest(),
    )
    # Serialize publication of the fold and its dependency subscriptions. No
    # journal/accepted-state lock is acquired here beyond ordinary indexed reads.
    with _BOOKS_LOCK:
        head: JournalPartitionHeadV1 = read_head(stream, partition_id)
        landing_head = bodies.landings.head()
        root = str(bodies.root)
        changes = _BODY_CHANGES.get(root)
        if (
            changes is None
            or changes.head.identity != landing_head.identity
            or changes.head.sequence > landing_head.sequence
        ):
            changes = _ResolutionBodyChanges(landing_head)
            _BODY_CHANGES[root] = changes
        changes.catch_up(bodies, landing_head)
        _BODY_CHANGES.move_to_end(root)
        while len(_BODY_CHANGES) > _BOOK_CAPACITY:
            _BODY_CHANGES.popitem(last=False)
        cached = _BOOKS.get(key)
        if cached is None or cached.bodies is not changes:
            cached = _CachedResolutionFold(_ResolutionFold.empty(), changes)
        else:
            for digest in cached.changed:
                payload = read_payload_bytes(bodies, digest, access=_REPLAY_ACCESS)
                if isinstance(payload, UnavailablePayload):
                    cached.blocked[digest] = payload
                else:
                    cached.blocked.pop(digest, None)
            cached.changed.clear()
        fold = cached.fold
        if not cached.blocked:
            added = None
            if not fold.unavailable and 0 < fold.head_sequence <= head.sequence:
                if fold.head_sequence == head.sequence:
                    if fold.head_digest == head.record_digest:
                        added = ()
                else:
                    suffix = tuple(
                        select(
                            stream,
                            partition_id=partition_id,
                            first_sequence=fold.head_sequence + 1,
                            last_sequence=head.sequence,
                        )
                    )
                    if (
                        len(suffix) == head.sequence - fold.head_sequence
                        and suffix[0].record.previous_record_digest == fold.head_digest
                        and suffix[-1].record_digest == head.record_digest
                    ):
                        added = suffix
            if added is None:
                # Cold/replaced journal or repair of an unavailable cold fold.
                fold = _ResolutionFold.empty()
                added = tuple(all_records(stream, partition_id)[: head.sequence])
            changes.subscribe(cached, added, bodies)
            fold = fold.extend(added, activations=activations, bodies=bodies, head=head)
            cached.fold = fold
            cached.blocked.update((item.digest, item) for item in fold.unavailable)
        if head.sequence > 0:
            _BOOKS[key] = cached
            _BOOKS.move_to_end(key)
            while len(_BOOKS) > _BOOK_CAPACITY:
                _BOOKS.popitem(last=False)
        if cached.blocked:
            fold = replace(
                _ResolutionFold.empty(),
                head_sequence=head.sequence,
                head_digest=head.record_digest,
                unavailable=tuple(cached.blocked.values()),
            )
        return ResolutionBookView(fold), head


def append_procedure_resolution(
    writer: ProcedureExhaustWriter,
    *,
    activation: ResolutionContractActivationV1
    | ResolutionContractActivationV2
    | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV1 | ProcedureResolutionV2,
    stream: JournalStreamIdentityV1,
    expected_head: JournalPartitionHeadV1 | None = None,
) -> StoredProcedureJournalRecordV1:
    """Append one resolution; ``expected_head`` makes it a compare-and-set.

    The sequence and closed-contract checks read the partition as it is now;
    a producer that decided the sequence from an earlier read passes the head
    of that read, so a competing resolution landed in between refuses this
    append as a journal conflict instead of retaining a second answer.
    """

    partition_id = resolution_contract_partition_id(activation)
    law = evaluate_procedure_resolution(activation, resolution)
    if law.verdict == "refused":
        raise ExecutionError(law.message or "resolution law refused")
    book, _head = resolution_book_at_head(writer.journal, stream, activation, bodies=writer.bodies)
    if book.unavailable:
        raise ExecutionError("resolution exhaust payload is unavailable")
    count = book.resolution_count(activation.contract_id)
    if resolution.sequence != count + 1:
        raise ExecutionError("resolution sequence is discontinuous")
    if count:
        latest = book.latest_resolution(activation.contract_id)
        assert latest is not None  # counted above
        if book.last_disposition_verdict(latest.resolution_id) != "overturned":
            raise ExecutionError(
                "resolution contract is closed until its latest answer is overturned"
            )
    return writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="resolution",
        accepted_coordinate=resolution_event_accepted_coordinate(activation, resolution),
        procedure_artifact_digest=activation.procedure_artifact_digest,
        definition_digest=activation.definition_digest,
        actor_context=resolution.actor_context,
        recorded_at=resolution.recorded_at,
        payload=resolution.model_dump(mode="json"),
        expected_head=expected_head,
    )


def append_resolution_disposition(
    writer: ProcedureExhaustWriter,
    *,
    activation: ResolutionContractActivationV1
    | ResolutionContractActivationV2
    | ResolutionContractActivationV3,
    resolution: ProcedureResolutionV1 | ProcedureResolutionV2,
    disposition: ProcedureResolutionDispositionV1,
    stream: JournalStreamIdentityV1,
    expected_head: JournalPartitionHeadV1 | None = None,
) -> StoredProcedureJournalRecordV1:
    partition_id = resolution_contract_partition_id(activation)
    if disposition.resolution_id != resolution.resolution_id:
        raise ExecutionError("resolution disposition names another resolution")
    book, _head = resolution_book_at_head(writer.journal, stream, activation, bodies=writer.bodies)
    # Validate against the immutable book at the caller's decision head. A
    # competitor landing before this reread is a CAS conflict, before semantic
    # checks can misclassify its valid new sequence/latest answer as corruption.
    # A competitor landing after this reread is caught by writer.append's CAS.
    if expected_head is not None and _head != expected_head:
        raise JournalConflictError("resolution disposition expected head is stale or substituted")
    if book.unavailable:
        raise ExecutionError("resolution exhaust payload is unavailable")
    stored_resolution = book.resolution_by_id(resolution.resolution_id)
    if stored_resolution != resolution:
        raise ExecutionError("resolution disposition target is absent from exhaust")
    latest = book.latest_resolution(resolution.contract_id)
    if latest is None or latest.resolution_id != resolution.resolution_id:
        raise ExecutionError("an answered overturn cannot be revised on an older resolution")
    if disposition.sequence != book.disposition_count(resolution.resolution_id) + 1:
        raise ExecutionError("resolution disposition sequence is discontinuous")
    return writer.append(
        stream=stream,
        partition_id=partition_id,
        event_kind="resolution_disposition",
        accepted_coordinate=resolution_event_accepted_coordinate(activation, resolution),
        procedure_artifact_digest=activation.procedure_artifact_digest,
        definition_digest=activation.definition_digest,
        actor_context=disposition.reviewer_actor_context,
        recorded_at=disposition.recorded_at,
        payload=disposition.model_dump(mode="json"),
        expected_head=expected_head,
    )


__all__ = [
    "ResolutionBookView",
    "ResolutionLookup",
    "resolution_book_at_head",
    "ProcedureProofReferenceV1",
    "ProcedureResolutionBook",
    "ProcedureResolutionDispositionV1",
    "ProcedureResolutionLawResultV1",
    "ProcedureResolutionV1",
    "ProcedureResolutionV2",
    "ResolutionClaimEndpointV1",
    "ResolutionContractActivationV1",
    "ResolutionContractActivationV2",
    "ResolutionDispositionVerdictV1",
    "ResolutionSettlementVerdictV2",
    "ResolutionSubjectV1",
    "ResolutionVerdictV1",
    "SettledOutcomeRelationV1",
    "append_procedure_resolution",
    "append_resolution_disposition",
    "build_procedure_resolution",
    "build_procedure_resolution_v2",
    "build_resolution_contract_activation_v2",
    "build_resolution_disposition",
    "build_settled_outcome_relation",
    "derive_resolution_activations",
    "evaluate_prediction_correctness_condition",
    "evaluate_procedure_resolution",
    "procedure_resolution_digest",
    "procedure_resolution_id",
    "procedure_arm_content_digest",
    "resolution_activation_digest",
    "resolution_activation_id",
    "resolution_contract_id",
    "resolution_contract_partition_id",
    "resolution_disposition_id",
    "resolution_event_accepted_coordinate",
    "settled_outcome_relation_digest",
]
