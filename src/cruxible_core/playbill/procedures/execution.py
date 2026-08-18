"""Authenticated graph-v3 admission and log-sufficient Procedure execution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillExecutionError, PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    JournalEventKindV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureJournalRecordDraftV1,
    StoredProcedureJournalRecordV1,
    journal_payload_bytes,
    parse_journal_payload,
)
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
)
from cruxible_core.playbill.procedures.graph import analyze_procedure_v3
from cruxible_core.playbill.procedures.input_planes import (
    AcceptedStateRunInputV1,
    validate_run_input_vector,
)
from cruxible_core.playbill.procedures.models import (
    CaptureEgressNodeV3,
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    InboxEgressNodeV3,
    MandateSettlementNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    ProviderNodeV3,
    RepeatBodyNodeV3,
    RepeatNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
    iter_pin_bindings,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.temporal import ensure_utc, utc_now

ProcedureRunStatusV1 = Literal["succeeded", "refused", "failed", "budget_exhausted"]


class _StrictExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


def run_value_digest(domain: str, value: object) -> str:
    return typed_digest(
        ArtifactDigest,
        f"playbill-procedure-run-{domain}-v1",
        {"value": normalize_canonical(value)},
    ).tagged


class ProcedureNodePinSetV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-node-pin-set-v1"] = "playbill-procedure-node-pin-set-v1"
    node_id: str
    pins: tuple[ArtifactPin, ...]

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        )
        identities = tuple((pin.role, pin.target.qualified) for pin in value)
        if value != ordered or len(identities) != len(set(identities)):
            raise ValueError("Procedure node pins must be sorted and unique")
        return value


class AcceptedStateRunMaterialV1(_StrictExecutionModel):
    tag: Literal["playbill-accepted-state-run-material-v1"] = (
        "playbill-accepted-state-run-material-v1"
    )
    input: AcceptedStateRunInputV1
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _digest(self) -> "AcceptedStateRunMaterialV1":
        if self.input.result_digest != run_value_digest("state-result", self.value):
            raise ValueError("accepted-state material does not reproduce its result digest")
        return self


class ProcedureRunAdmissionV1(_StrictExecutionModel):
    """The complete direct-invocation binding fixed before any result is visible."""

    tag: Literal["playbill-procedure-run-admission-v1"] = "playbill-procedure-run-admission-v1"
    instance_id: str
    run_id: str
    attempt: int = Field(ge=1)
    accepted_coordinate: AcceptedCoordinate
    procedure_identity: ArtifactIdentity
    procedure_path: str
    procedure_artifact_digest: str
    definition_digest: str
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    full_pins: tuple[ArtifactPin, ...]
    node_pin_sets: tuple[ProcedureNodePinSetV1, ...]
    pin_set_digest: str
    invocation_input: object
    accepted_state_inputs: tuple[AcceptedStateRunInputV1, ...]
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    actor_context: GovernedActorContext
    invocation_origin: Literal["actor"] = "actor"
    journal_stream: JournalStreamIdentityV1
    journal_partition_id: str
    line_spec_digest: str | None = None
    occurrence_id: str | None = None
    deployment_snapshot_digest: str | None = None
    acquisition_policy_digest: str | None = None
    mandate_coordinate_digest: str | None = None
    calibration_coordinate_digest: str | None = None
    epsilon_member: bool = False
    admitted_at: datetime
    admission_binding_digest: str

    @field_validator("invocation_input", mode="before")
    @classmethod
    def _invocation_input(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator(
        "procedure_artifact_digest",
        "definition_digest",
        "pin_set_digest",
        "line_spec_digest",
        "deployment_snapshot_digest",
        "acquisition_policy_digest",
        "mandate_coordinate_digest",
        "calibration_coordinate_digest",
        "admission_binding_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _sha256(value, label=str(getattr(info, "field_name", "run digest")))

    @field_validator("admitted_at")
    @classmethod
    def _admitted_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("accepted_state_inputs")
    @classmethod
    def _state_inputs(
        cls, value: tuple[AcceptedStateRunInputV1, ...]
    ) -> tuple[AcceptedStateRunInputV1, ...]:
        names = tuple(item.input_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("accepted-state run inputs must be sorted and unique")
        return value

    @field_validator("node_pin_sets")
    @classmethod
    def _node_pin_sets(
        cls, value: tuple[ProcedureNodePinSetV1, ...]
    ) -> tuple[ProcedureNodePinSetV1, ...]:
        node_ids = tuple(item.node_id for item in value)
        if node_ids != tuple(sorted(set(node_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Procedure node pin sets must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _direct_e1_shape(self) -> "ProcedureRunAdmissionV1":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("Procedure run admission requires a Procedure identity")
        if self.journal_stream.instance_id != self.instance_id:
            raise ValueError("Procedure run and journal stream instance identities differ")
        validate_run_input_vector(
            self.accepted_state_inputs,
            expected_accepted=self.accepted_coordinate,
        )
        if (
            any(
                value is not None
                for value in (
                    self.line_spec_digest,
                    self.occurrence_id,
                    self.deployment_snapshot_digest,
                    self.acquisition_policy_digest,
                    self.mandate_coordinate_digest,
                    self.calibration_coordinate_digest,
                )
            )
            or self.epsilon_member
        ):
            raise ValueError("PC-E1 direct admission cannot claim Line/deployment policy state")
        expected_pin_digest = procedure_pin_set_digest(self.full_pins, self.node_pin_sets)
        if self.pin_set_digest != expected_pin_digest:
            raise ValueError("Procedure run pin_set_digest does not reproduce")
        expected = procedure_admission_digest(self)
        if self.admission_binding_digest != expected:
            raise ValueError("Procedure run admission_binding_digest does not reproduce")
        return self


class PreparedProcedureRunV1(_StrictExecutionModel):
    tag: Literal["playbill-prepared-procedure-run-v1"] = "playbill-prepared-procedure-run-v1"
    admission: ProcedureRunAdmissionV1
    accepted_state_materials: tuple[AcceptedStateRunMaterialV1, ...]

    @model_validator(mode="after")
    def _materials(self) -> "PreparedProcedureRunV1":
        inputs = self.admission.accepted_state_inputs
        if tuple(item.input for item in self.accepted_state_materials) != inputs:
            raise ValueError("prepared run materials must exactly match admitted state inputs")
        return self


class ProcedureRunRefusalV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-refusal-v1"] = "playbill-procedure-run-refusal-v1"
    code: str
    message: str
    node_id: str | None = None


class ProcedureRunReceiptV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-receipt-v1"] = "playbill-procedure-run-receipt-v1"
    run_id: str
    admission_binding_digest: str
    stream: JournalStreamIdentityV1
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_digests: tuple[str, ...]
    chain_head_digest: str

    @field_validator("admission_binding_digest", "chain_head_digest")
    @classmethod
    def _receipt_digest(cls, value: str, info: object) -> str:
        return _sha256(value, label=str(getattr(info, "field_name", "receipt digest")))

    @field_validator("record_digests")
    @classmethod
    def _record_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Procedure receipt record digests must be nonempty and unique")
        for item in value:
            _sha256(item, label="Procedure receipt record digest")
        return value

    @model_validator(mode="after")
    def _range(self) -> "ProcedureRunReceiptV1":
        if self.last_sequence - self.first_sequence + 1 != len(self.record_digests):
            raise ValueError("Procedure receipt sequence range and records disagree")
        if self.chain_head_digest != self.record_digests[-1]:
            raise ValueError("Procedure receipt head must equal its final record digest")
        return self


class ProcedureRunResultV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-result-v1"] = "playbill-procedure-run-result-v1"
    run_id: str
    status: ProcedureRunStatusV1
    output: object | None = None
    refusal: ProcedureRunRefusalV1 | None = None
    receipt: ProcedureRunReceiptV1

    @field_validator("output", mode="before")
    @classmethod
    def _output(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureRunResultV1":
        if (self.status == "succeeded") != (self.output is not None):
            raise ValueError("only a succeeded Procedure run carries output")
        if (self.status == "refused") != (self.refusal is not None):
            raise ValueError("only a refused Procedure run carries a typed refusal")
        return self


def procedure_run_receipt_digest(receipt: ProcedureRunReceiptV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-receipt-v1",
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


class ProviderInvocationResultV1(_StrictExecutionModel):
    tag: Literal["playbill-provider-invocation-result-v1"] = (
        "playbill-provider-invocation-result-v1"
    )
    output: object
    trace: object = Field(default_factory=dict)

    @field_validator("output", "trace", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class StateTapReaderProtocol(Protocol):
    def read_accepted_state(
        self,
        *,
        query: ArtifactPin,
        parameters: CanonicalValue,
        coordinate: AcceptedCoordinate,
    ) -> object: ...


class ProviderExecutorProtocol(Protocol):
    def execute_provider(
        self,
        *,
        provider: ArtifactPin,
        environment: ArtifactPin,
        contract_in: ArtifactPin,
        contract_out: ArtifactPin,
        payload: CanonicalValue,
        actor_context: GovernedActorContext,
    ) -> ProviderInvocationResultV1: ...


class ContractValidatorProtocol(Protocol):
    def validate_contract(
        self,
        *,
        contract: ArtifactPin,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
    ) -> object: ...


class ProcedureActivationAuthorityProtocol(Protocol):
    def current_procedure_digest(
        self,
        identity: ArtifactIdentity,
        *,
        coordinate: AcceptedCoordinate,
    ) -> str | None: ...


class ProcedureClockProtocol(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@dataclass(frozen=True)
class SystemProcedureClock:
    def now(self) -> datetime:
        return utc_now()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def procedure_pin_set_digest(
    pins: tuple[ArtifactPin, ...],
    node_pin_sets: tuple[ProcedureNodePinSetV1, ...],
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-pin-set-v1",
        {
            "full_pins": [pin.model_dump(mode="json") for pin in pins],
            "node_pin_sets": [item.model_dump(mode="json") for item in node_pin_sets],
        },
    ).tagged


def procedure_admission_digest(admission: ProcedureRunAdmissionV1) -> str:
    payload = admission.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("admission_binding_digest")
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-admission-v1",
        {"admission": payload},
    ).tagged


def _exact_pin(binding: ArtifactPin | ProcedurePinSlotRefV1, *, label: str) -> ArtifactPin:
    if isinstance(binding, ProcedurePinSlotRefV1):
        raise PlaybillExecutionError(f"line_binding_required: {label} uses a LineSpec pin slot")
    return binding


def _node_pin_sets(accepted: AcceptedProcedureV1) -> tuple[ProcedureNodePinSetV1, ...]:
    result: list[ProcedureNodePinSetV1] = []
    for node in accepted.procedure.definition.nodes:
        bindings = iter_pin_bindings(node)
        if any(isinstance(item, ProcedurePinSlotRefV1) for item in bindings):
            raise PlaybillExecutionError(
                f"line_binding_required: Procedure node {node.node_id!r} uses a LineSpec pin slot"
            )
        pins = tuple(item for item in bindings if isinstance(item, ArtifactPin))
        result.append(
            ProcedureNodePinSetV1(
                node_id=node.node_id,
                pins=tuple(
                    sorted(
                        pins,
                        key=lambda pin: (
                            pin.role.encode("utf-8"),
                            pin.target.qualified.encode("utf-8"),
                            pin.artifact_digest.encode("ascii"),
                        ),
                    )
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.node_id.encode("utf-8")))


def accepted_procedure_pin_set_digest(accepted: AcceptedProcedureV1) -> str:
    """Reproduce the direct-runtime pin commitment for one accepted Procedure."""

    return procedure_pin_set_digest(accepted.procedure.pins, _node_pin_sets(accepted))


def prepare_direct_procedure_run(
    accepted: AcceptedProcedureV1,
    *,
    instance_id: str,
    run_id: str,
    accepted_coordinate: AcceptedCoordinate,
    invocation_input: object,
    actor_context: GovernedActorContext,
    state_reader: StateTapReaderProtocol,
    journal_stream: JournalStreamIdentityV1,
    journal_partition_id: str,
    admitted_at: datetime,
    attempt: int = 1,
) -> PreparedProcedureRunV1:
    """Bind exact accepted state and all pins for an actor-authenticated direct run."""

    procedure = accepted.procedure
    if not procedure.directly_runnable:
        raise PlaybillExecutionError("line_binding_required: Procedure has unresolved pin slots")
    if procedure_artifact_digest(procedure).tagged != accepted.artifact_digest:
        raise PlaybillExecutionError("accepted Procedure artifact digest does not reproduce")

    materials: list[AcceptedStateRunMaterialV1] = []
    for node in procedure.definition.nodes:
        if not isinstance(node, StateTapNodeV3):
            continue
        query = _exact_pin(node.query, label=f"state_tap {node.node_id!r}")
        parameters = normalize_canonical(node.parameters)
        value = normalize_canonical(
            state_reader.read_accepted_state(
                query=query,
                parameters=parameters,
                coordinate=accepted_coordinate,
            )
        )
        run_input = AcceptedStateRunInputV1(
            input_name=node.as_,
            read_coordinate=accepted_coordinate,
            query_definition_digest=query.artifact_digest,
            parameters_digest=run_value_digest("state-parameters", parameters),
            result_digest=run_value_digest("state-result", value),
        )
        materials.append(AcceptedStateRunMaterialV1(input=run_input, value=value))
    materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))

    node_pin_sets = _node_pin_sets(accepted)
    full_pins = procedure.pins
    pin_digest = procedure_pin_set_digest(full_pins, node_pin_sets)
    provisional = ProcedureRunAdmissionV1.model_construct(
        _fields_set=None,
        instance_id=instance_id,
        run_id=run_id,
        attempt=attempt,
        accepted_coordinate=accepted_coordinate,
        procedure_identity=procedure.identity,
        procedure_path=accepted.path,
        procedure_artifact_digest=accepted.artifact_digest,
        definition_digest=procedure.definition_digest,
        activation_policy=procedure.activation_policy,
        full_pins=full_pins,
        node_pin_sets=node_pin_sets,
        pin_set_digest=pin_digest,
        invocation_input=invocation_input,
        accepted_state_inputs=tuple(item.input for item in materials),
        budget=procedure.definition.budget,
        hard_caps=procedure.definition.hard_caps,
        actor_context=actor_context,
        invocation_origin="actor",
        journal_stream=journal_stream,
        journal_partition_id=journal_partition_id,
        line_spec_digest=None,
        occurrence_id=None,
        deployment_snapshot_digest=None,
        acquisition_policy_digest=None,
        mandate_coordinate_digest=None,
        calibration_coordinate_digest=None,
        epsilon_member=False,
        admitted_at=ensure_utc(admitted_at),
        admission_binding_digest="sha256:" + "0" * 64,
    )
    admission = ProcedureRunAdmissionV1(
        instance_id=instance_id,
        run_id=run_id,
        attempt=attempt,
        accepted_coordinate=accepted_coordinate,
        procedure_identity=procedure.identity,
        procedure_path=accepted.path,
        procedure_artifact_digest=accepted.artifact_digest,
        definition_digest=procedure.definition_digest,
        activation_policy=procedure.activation_policy,
        full_pins=full_pins,
        node_pin_sets=node_pin_sets,
        pin_set_digest=pin_digest,
        invocation_input=invocation_input,
        accepted_state_inputs=tuple(item.input for item in materials),
        budget=procedure.definition.budget,
        hard_caps=procedure.definition.hard_caps,
        actor_context=actor_context,
        invocation_origin="actor",
        journal_stream=journal_stream,
        journal_partition_id=journal_partition_id,
        admitted_at=ensure_utc(admitted_at),
        admission_binding_digest=procedure_admission_digest(provisional),
    )
    return PreparedProcedureRunV1(
        admission=admission,
        accepted_state_materials=tuple(materials),
    )


class _RunRefusal(Exception):
    def __init__(self, code: str, message: str, *, node_id: str | None = None) -> None:
        super().__init__(message)
        self.refusal = ProcedureRunRefusalV1(code=code, message=message, node_id=node_id)


class _BudgetExceeded(Exception):
    pass


@dataclass
class _RunState:
    outputs: dict[str, CanonicalValue]
    input_payload: CanonicalValue
    parameters: CanonicalValue
    provider_calls: int = 0
    capture_bytes: int = 0


class ProcedureExecutor:
    """Execute one admitted graph while making the journal the operational authority."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        run_index: ProcedureRunIndex,
        fencing_token: str,
        activation_authority: ProcedureActivationAuthorityProtocol,
        contract_validator: ContractValidatorProtocol,
        provider_executor: ProviderExecutorProtocol | None = None,
        clock: ProcedureClockProtocol | None = None,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.run_index = run_index
        self.fencing_token = fencing_token
        self.activation_authority = activation_authority
        self.contract_validator = contract_validator
        self.provider_executor = provider_executor
        self.clock = clock or SystemProcedureClock()

    def execute(
        self,
        prepared: PreparedProcedureRunV1,
        accepted: AcceptedProcedureV1,
    ) -> ProcedureRunResultV1:
        admission = prepared.admission
        self._verify_correspondence(admission, accepted)
        existing_records = self.journal.all_records(
            admission.journal_stream,
            admission.journal_partition_id,
        )
        self.run_index.rebuild(existing_records, bodies=self.bodies)
        indexed = self.run_index.get(admission.run_id)
        if indexed is not None:
            if indexed.admission_binding_digest != admission.admission_binding_digest:
                raise PlaybillExecutionError("run_id collides across distinct admission bindings")
            if indexed.status != "running":
                return self._replay_completed(admission, existing_records)
            effect_state = (
                " with an unresolved durable effect intent"
                if indexed.effect_intent_count != indexed.effect_result_count
                else ""
            )
            raise PlaybillExecutionError(
                "run_recovery_required: admitted run has incomplete exhaust" + effect_state
            )
        self._require_current(admission)
        records: list[StoredProcedureJournalRecordV1] = []
        started_ns = self.clock.monotonic_ns()

        self._append_event(
            admission,
            records,
            "attempt_started",
            {"attempt": admission.attempt, "admitted_at": admission.admitted_at.isoformat()},
        )
        self._append_event(
            admission,
            records,
            "admission_bound",
            admission.model_dump(mode="json"),
        )

        state = _RunState(
            outputs={
                item.input.input_name: normalize_canonical(item.value)
                for item in prepared.accepted_state_materials
            },
            input_payload=normalize_canonical(admission.invocation_input),
            parameters={},
        )
        status: ProcedureRunStatusV1
        output: CanonicalValue | None = None
        refusal: ProcedureRunRefusalV1 | None = None
        failure_message: str | None = None
        try:
            input_contract = _exact_pin(
                accepted.procedure.definition.contract_in,
                label="Procedure contract_in",
            )
            state.input_payload = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=input_contract,
                    payload=state.input_payload,
                    direction="input",
                )
            )
            output = self._walk(
                accepted,
                admission=admission,
                state=state,
                records=records,
                started_ns=started_ns,
            )
            status = "succeeded"
        except _RunRefusal as exc:
            status = "refused"
            refusal = exc.refusal
        except _BudgetExceeded as exc:
            status = "budget_exhausted"
            failure_message = str(exc)
        except Exception as exc:
            status = "failed"
            failure_message = f"{type(exc).__name__}: {exc}"

        final_payload = {
            "status": status,
            "output": output,
            "refusal": None if refusal is None else refusal.model_dump(mode="json"),
            "failure": failure_message,
            "provider_calls": state.provider_calls,
            "capture_bytes": state.capture_bytes,
        }
        self._append_event(
            admission,
            records,
            "attempt_finalized",
            final_payload,
        )
        receipt = self._receipt(admission, records)
        return ProcedureRunResultV1(
            run_id=admission.run_id,
            status=status,
            output=output if status == "succeeded" else None,
            refusal=refusal,
            receipt=receipt,
        )

    def _verify_correspondence(
        self,
        admission: ProcedureRunAdmissionV1,
        accepted: AcceptedProcedureV1,
    ) -> None:
        procedure = accepted.procedure
        if (
            accepted.path != admission.procedure_path
            or accepted.artifact_digest != admission.procedure_artifact_digest
            or procedure.definition_digest != admission.definition_digest
            or procedure.identity != admission.procedure_identity
            or procedure.activation_policy != admission.activation_policy
            or procedure.pins != admission.full_pins
            or procedure.definition.budget != admission.budget
            or procedure.definition.hard_caps != admission.hard_caps
        ):
            raise PlaybillExecutionError("Procedure admission and accepted artifact differ")
        if _node_pin_sets(accepted) != admission.node_pin_sets:
            raise PlaybillExecutionError("Procedure node pins changed after admission")
        expected_state_inputs = {
            node.as_: (
                _exact_pin(node.query, label=f"state_tap {node.node_id!r}"),
                run_value_digest("state-parameters", normalize_canonical(node.parameters)),
            )
            for node in procedure.definition.nodes
            if isinstance(node, StateTapNodeV3)
        }
        actual_state_inputs = {item.input_name: item for item in admission.accepted_state_inputs}
        if set(actual_state_inputs) != set(expected_state_inputs):
            raise PlaybillExecutionError("Procedure admission state_tap input set differs")
        for name, (query, parameters_digest) in expected_state_inputs.items():
            actual = actual_state_inputs[name]
            if (
                actual.query_definition_digest != query.artifact_digest
                or actual.parameters_digest != parameters_digest
                or actual.read_coordinate != admission.accepted_coordinate
            ):
                raise PlaybillExecutionError(
                    f"Procedure admission state_tap input {name!r} differs"
                )

    def _require_current(self, admission: ProcedureRunAdmissionV1) -> None:
        current = self.activation_authority.current_procedure_digest(
            admission.procedure_identity,
            coordinate=admission.accepted_coordinate,
        )
        if current != admission.procedure_artifact_digest:
            raise PlaybillExecutionError("Procedure is not current at the admitted coordinate")

    def _checkpoint_current(self, admission: ProcedureRunAdmissionV1, *, effect: bool) -> None:
        if admission.activation_policy == "abort" or (
            admission.activation_policy == "epoch-check" and effect
        ):
            self._require_current(admission)

    def _check_budget(
        self,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        *,
        started_ns: int,
    ) -> None:
        elapsed_us = (self.clock.monotonic_ns() - started_ns) // 1000
        if elapsed_us > admission.budget.wall_clock.microseconds:
            raise _BudgetExceeded("Procedure wall-clock budget exhausted")
        if state.provider_calls > admission.budget.max_provider_calls:
            raise _BudgetExceeded("Procedure provider-call budget exhausted")
        if state.capture_bytes > admission.budget.max_capture_bytes:
            raise _BudgetExceeded("Procedure capture-byte budget exhausted")
        for value in state.outputs.values():
            count = _item_count(value)
            if count > admission.budget.max_items:
                raise _BudgetExceeded("Procedure item budget exhausted")

    def _walk(
        self,
        accepted: AcceptedProcedureV1,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> CanonicalValue:
        definition = accepted.procedure.definition
        graph = analyze_procedure_v3(definition)
        nodes = {node.node_id: node for node in definition.nodes}
        current = definition.nodes[0].node_id
        while True:
            self._checkpoint_current(admission, effect=False)
            self._check_budget(admission, state, started_ns=started_ns)
            node = nodes[current]
            try:
                branch = self._execute_node(
                    node,
                    admission=admission,
                    state=state,
                    records=records,
                    started_ns=started_ns,
                )
                self._check_budget(admission, state, started_ns=started_ns)
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "succeeded"},
                )
            except _RunRefusal:
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "refused"},
                )
                raise
            except Exception:
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "failed"},
                )
                raise

            edges = graph.edges[node.node_id]
            target: str | None
            if isinstance(node, GuardNodeV3):
                target = edges[branch or "on_false"]
            else:
                target = edges.get("next")
            if target == "$abort":
                if isinstance(node, GuardNodeV3):
                    raise _RunRefusal(
                        node.refusal_code,
                        node.message,
                        node_id=node.node_id,
                    )
                raise _RunRefusal(
                    "procedure.abort",
                    "Procedure reached an explicit abort edge.",
                    node_id=node.node_id,
                )
            if target is None:
                try:
                    return state.outputs[definition.returns]
                except KeyError as exc:  # pragma: no cover - static law should prevent
                    raise PlaybillExecutionError("Procedure return alias was not produced") from exc
            current = target

    def _execute_node(
        self,
        node: object,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> Literal["on_true", "on_false"] | None:
        if isinstance(node, StateTapNodeV3):
            if node.as_ not in state.outputs:
                raise PlaybillExecutionError("admitted state_tap material is absent")
            return None
        if isinstance(node, SourceNodeV3 | ExhaustTapNodeV3):
            raise _RunRefusal(
                "line_binding_required",
                "Source acquisition and exhaust inputs require a Line binding in PC-E2.",
                node_id=node.node_id,
            )
        if isinstance(node, ProviderNodeV3):
            self._run_provider(
                node,
                admission=admission,
                state=state,
                records=records,
            )
            return None
        if isinstance(node, TransformNodeV3):
            resolved = _resolve_template(
                node.spec,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
            contract_in = _exact_pin(node.contract_in, label=f"transform {node.node_id!r} input")
            validated_input = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=contract_in,
                    payload=resolved,
                    direction="input",
                )
            )
            value = _apply_transform(node.transform_kind, validated_input)
            contract = _exact_pin(node.contract_out, label=f"transform {node.node_id!r} output")
            state.outputs[node.as_] = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=contract,
                    payload=value,
                    direction="output",
                )
            )
            return None
        if isinstance(node, ProjectNodeV3):
            value = _resolve_template(
                node.fields,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
            contract = _exact_pin(node.contract_out, label=f"project {node.node_id!r} output")
            state.outputs[node.as_] = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=contract,
                    payload=value,
                    direction="output",
                )
            )
            return None
        if isinstance(node, GuardNodeV3):
            verdict, trace = _evaluate_predicate(
                node.predicate,
                input_payload=state.input_payload,
                outputs=state.outputs,
                parameters=state.parameters,
            )
            self._append_event(
                admission,
                records,
                "branch_evaluated",
                {
                    "node_id": node.node_id,
                    "verdict": verdict,
                    "selected_arm": "on_true" if verdict else "on_false",
                    "operands": trace,
                },
            )
            return "on_true" if verdict else "on_false"
        if isinstance(node, RepeatNodeV3):
            self._run_repeat(
                node,
                admission=admission,
                state=state,
                records=records,
                started_ns=started_ns,
            )
            return None
        if isinstance(
            node,
            CaptureEgressNodeV3
            | InboxEgressNodeV3
            | ProposeChangeSetNodeV3
            | MandateSettlementNodeV3,
        ):
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "verdict": "not_available_in_pc_e1",
                },
            )
            raise _RunRefusal(
                "terminal_not_available",
                "Governed terminal rungs are implemented with Line runtime in PC-E2.",
                node_id=node.node_id,
            )
        raise PlaybillExecutionError(f"unsupported graph-v3 node {type(node).__name__}")

    def _run_provider(
        self,
        node: ProviderNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        if self.provider_executor is None:
            raise _RunRefusal(
                "provider_unavailable",
                "No Provider executor is registered for this Procedure runtime.",
                node_id=node.node_id,
            )
        provider = _exact_pin(node.provider, label=f"provider {node.node_id!r}")
        environment = _exact_pin(node.environment, label=f"provider {node.node_id!r} environment")
        contract_in = _exact_pin(node.contract_in, label=f"provider {node.node_id!r} input")
        contract_out = _exact_pin(node.contract_out, label=f"provider {node.node_id!r} output")
        resolved = normalize_canonical(
            _resolve_template(
                node.input,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
        )
        payload = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_in,
                payload=resolved,
                direction="input",
            )
        )
        if state.provider_calls >= admission.budget.max_provider_calls:
            raise _BudgetExceeded("Procedure provider-call budget exhausted")
        state.provider_calls += 1
        effect_policy = node.effect_policy
        effectful = effect_policy is not None
        effect_id = typed_digest(
            ArtifactDigest,
            "playbill-procedure-effect-intent-v1",
            {
                "run_id": admission.run_id,
                "node_id": node.node_id,
                "provider_digest": provider.artifact_digest,
                "environment_digest": environment.artifact_digest,
                "input_digest": run_value_digest("provider-input", payload),
            },
        ).tagged
        if effect_policy is not None:
            self._checkpoint_current(admission, effect=True)
            self._append_event(
                admission,
                records,
                "effect_intent",
                {
                    "effect_id": effect_id,
                    "node_id": node.node_id,
                    "provider": provider.model_dump(mode="json"),
                    "environment": environment.model_dump(mode="json"),
                    "effect_policy": _exact_pin(
                        effect_policy,
                        label=f"provider {node.node_id!r} effect policy",
                    ).model_dump(mode="json"),
                    "input_digest": run_value_digest("provider-input", payload),
                },
            )
        result = self.provider_executor.execute_provider(
            provider=provider,
            environment=environment,
            contract_in=contract_in,
            contract_out=contract_out,
            payload=payload,
            actor_context=admission.actor_context,
        )
        output = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_out,
                payload=normalize_canonical(result.output),
                direction="output",
            )
        )
        state.outputs[node.as_] = output
        if effectful:
            self._append_event(
                admission,
                records,
                "effect_result",
                {
                    "effect_id": effect_id,
                    "node_id": node.node_id,
                    "output_digest": run_value_digest("provider-output", output),
                    "trace": result.trace,
                },
            )

    def _run_repeat(
        self,
        node: RepeatNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> None:
        attempts: list[CanonicalValue] = []
        for attempt in range(1, node.max_attempts + 1):
            local_outputs: dict[str, CanonicalValue] = {}
            for body in node.body:
                self._run_repeat_body(
                    body,
                    admission=admission,
                    state=state,
                    local_outputs=local_outputs,
                    records=records,
                )
                self._check_budget(admission, state, started_ns=started_ns)
            verdict, trace = _evaluate_predicate(
                node.until,
                input_payload=state.input_payload,
                outputs=local_outputs,
                parameters=state.parameters,
            )
            attempt_value = normalize_canonical(
                {"attempt": attempt, "outputs": local_outputs, "until": verdict}
            )
            attempts.append(attempt_value)
            self._append_event(
                admission,
                records,
                "branch_evaluated",
                {
                    "node_id": node.node_id,
                    "repeat_attempt": attempt,
                    "verdict": verdict,
                    "operands": trace,
                },
            )
            if verdict:
                state.outputs[node.as_] = normalize_canonical(
                    {"attempts": attempts, "final": local_outputs}
                )
                return
        raise _RunRefusal(
            "repeat_exhausted",
            "Procedure repeat exhausted its declared attempt bound.",
            node_id=node.node_id,
        )

    def _run_repeat_body(
        self,
        body: RepeatBodyNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        local_outputs: dict[str, CanonicalValue],
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        combined = {**state.outputs, **local_outputs}
        spec = normalize_canonical(
            _resolve_template(body.spec, input_payload=state.input_payload, outputs=combined)
        )
        if body.operation == "transform":
            contract_in = _exact_pin(body.contract_in, label=f"repeat {body.node_id!r} input")
            contract_out = _exact_pin(body.contract_out, label=f"repeat {body.node_id!r} output")
            validated = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=contract_in,
                    payload=spec,
                    direction="input",
                )
            )
            local_outputs[body.as_] = normalize_canonical(
                self.contract_validator.validate_contract(
                    contract=contract_out,
                    payload=validated,
                    direction="output",
                )
            )
            return
        if self.provider_executor is None or body.provider is None or body.environment is None:
            raise _RunRefusal(
                "provider_unavailable",
                "Repeat provider operation has no registered executor.",
                node_id=body.node_id,
            )
        provider = _exact_pin(body.provider, label=f"repeat {body.node_id!r} provider")
        environment = _exact_pin(body.environment, label=f"repeat {body.node_id!r} environment")
        contract_in = _exact_pin(body.contract_in, label=f"repeat {body.node_id!r} input")
        contract_out = _exact_pin(body.contract_out, label=f"repeat {body.node_id!r} output")
        if state.provider_calls >= admission.budget.max_provider_calls:
            raise _BudgetExceeded("Procedure provider-call budget exhausted")
        state.provider_calls += 1
        validated_input = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_in,
                payload=spec,
                direction="input",
            )
        )
        result = self.provider_executor.execute_provider(
            provider=provider,
            environment=environment,
            contract_in=contract_in,
            contract_out=contract_out,
            payload=validated_input,
            actor_context=admission.actor_context,
        )
        local_outputs[body.as_] = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_out,
                payload=normalize_canonical(result.output),
                direction="output",
            )
        )

    def _append_event(
        self,
        admission: ProcedureRunAdmissionV1,
        records: list[StoredProcedureJournalRecordV1],
        event_kind: JournalEventKindV1,
        payload: object,
    ) -> StoredProcedureJournalRecordV1:
        payload_bytes = journal_payload_bytes(payload)
        metadata = self.bodies.store(payload_bytes)
        head = self.journal.read_head(
            admission.journal_stream,
            admission.journal_partition_id,
        )
        draft = ProcedureJournalRecordDraftV1(
            stream=admission.journal_stream,
            partition_id=admission.journal_partition_id,
            event_kind=event_kind,
            accepted_coordinate=admission.accepted_coordinate,
            procedure_artifact_digest=admission.procedure_artifact_digest,
            definition_digest=admission.definition_digest,
            run_id=admission.run_id,
            attempt=admission.attempt,
            admission_binding_digest=admission.admission_binding_digest,
            payload_digest=metadata.digest,
            actor_context=admission.actor_context,
            recorded_at=self.clock.now(),
        )
        try:
            stored = self.journal.append(
                draft,
                expected_head=head,
                fencing_token=self.fencing_token,
            )
        except PlaybillJournalError:
            raise
        records.append(stored)
        self.run_index.apply_record(stored, payload=normalize_canonical(payload))
        return stored

    def _replay_completed(
        self,
        admission: ProcedureRunAdmissionV1,
        records: tuple[StoredProcedureJournalRecordV1, ...],
    ) -> ProcedureRunResultV1:
        run_records = [
            item
            for item in records
            if item.record.run_id == admission.run_id
            and item.record.admission_binding_digest == admission.admission_binding_digest
        ]
        if not run_records or run_records[-1].record.event_kind != "attempt_finalized":
            raise PlaybillExecutionError("completed run index is not supported by journal exhaust")
        access = BodyAccessContext(principal_id="procedure-run-replay", can_read_body=True)
        payload = parse_journal_payload(
            self.bodies.read(run_records[-1].record.payload_digest, access=access)
        )
        if not isinstance(payload, dict):
            raise PlaybillExecutionError("attempt-finalized payload is not an object")
        receipt = self._receipt(admission, run_records)
        try:
            return ProcedureRunResultV1.model_validate(
                {
                    "run_id": admission.run_id,
                    "status": payload.get("status"),
                    "output": payload.get("output"),
                    "refusal": payload.get("refusal"),
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
        except ValueError as exc:
            raise PlaybillExecutionError(
                "attempt-finalized payload cannot reproduce its run result"
            ) from exc

    @staticmethod
    def _receipt(
        admission: ProcedureRunAdmissionV1,
        records: list[StoredProcedureJournalRecordV1],
    ) -> ProcedureRunReceiptV1:
        if not records:  # pragma: no cover - attempt start always lands first
            raise PlaybillExecutionError("Procedure run produced no exhaust records")
        return ProcedureRunReceiptV1(
            run_id=admission.run_id,
            admission_binding_digest=admission.admission_binding_digest,
            stream=admission.journal_stream,
            partition_id=admission.journal_partition_id,
            first_sequence=records[0].record.sequence,
            last_sequence=records[-1].record.sequence,
            record_digests=tuple(item.record_digest for item in records),
            chain_head_digest=records[-1].record_digest,
        )


def _resolve_path(value: object, path: tuple[str, ...], *, reference: str) -> object:
    current = value
    for member in path:
        if isinstance(current, dict) and member in current:
            current = current[member]
            continue
        if isinstance(current, list) and member.isdigit() and int(member) < len(current):
            current = current[int(member)]
            continue
        raise PlaybillExecutionError(f"runtime reference {reference!r} does not resolve")
    return current


def _resolve_reference(
    value: str,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    item: CanonicalValue | None = None,
) -> object:
    if value == "$input":
        return input_payload
    if value.startswith("$input."):
        return _resolve_path(input_payload, tuple(value[7:].split(".")), reference=value)
    if value.startswith("$steps."):
        parts = tuple(value[7:].split("."))
        alias = parts[0]
        if alias not in outputs:
            raise PlaybillExecutionError(f"runtime reference {value!r} names absent output")
        return _resolve_path(outputs[alias], parts[1:], reference=value)
    if value == "$item":
        if item is None:
            raise PlaybillExecutionError("$item is unavailable outside item transforms")
        return item
    if value.startswith("$item."):
        if item is None:
            raise PlaybillExecutionError("$item is unavailable outside item transforms")
        return _resolve_path(item, tuple(value[6:].split(".")), reference=value)
    return value


def _resolve_template(
    value: object,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    item: CanonicalValue | None = None,
) -> CanonicalValue:
    if isinstance(value, str):
        return normalize_canonical(
            _resolve_reference(
                value,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
            )
        )
    if isinstance(value, list | tuple):
        return [
            _resolve_template(
                member,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
            )
            for member in value
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_template(
                member,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
            )
            for key, member in value.items()
        }
    return normalize_canonical(value)


def _item_count(value: CanonicalValue) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return len(items)
    return 1


def _extract_items(value: object, *, label: str) -> list[CanonicalValue]:
    if isinstance(value, list):
        return [normalize_canonical(item) for item in value]
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return [normalize_canonical(item) for item in value["items"]]
    raise PlaybillExecutionError(f"{label} requires a list or object with an items list")


def _apply_transform(kind: str, spec: CanonicalValue) -> CanonicalValue:
    if kind == "adapter":
        return spec
    if not isinstance(spec, dict):
        raise PlaybillExecutionError(f"transform {kind!r} requires an object spec")
    if kind == "shape_items":
        items = _extract_items(spec.get("items"), label=kind)
        fields = spec.get("fields", {})
        if not isinstance(fields, dict):
            raise PlaybillExecutionError("shape_items fields must be an object")
        shaped: list[CanonicalValue] = []
        for item in items:
            base = (
                dict(item) if spec.get("include_input") is True and isinstance(item, dict) else {}
            )
            for name, template in fields.items():
                base[name] = _resolve_template(
                    template,
                    input_payload={},
                    outputs={},
                    item=item,
                )
            shaped.append(normalize_canonical(base))
        return {"items": shaped, "input_count": len(items), "output_count": len(shaped)}
    if kind == "filter_items":
        items = _extract_items(spec.get("items"), label=kind)
        where = spec.get("where", {})
        if not isinstance(where, dict):
            raise PlaybillExecutionError("filter_items where must be an object")
        kept = [
            item
            for item in items
            if isinstance(item, dict)
            and all(item.get(key) == value for key, value in where.items())
        ]
        return normalize_canonical(
            {"items": kept, "input_count": len(items), "output_count": len(kept)}
        )
    if kind == "dedupe_items":
        items = _extract_items(spec.get("items"), label=kind)
        keys = spec.get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise PlaybillExecutionError("dedupe_items keys must be a string list")
        key_names = [str(item) for item in keys]
        seen: set[bytes] = set()
        output: list[CanonicalValue] = []
        for item in items:
            identity = canonical_bytes(
                [item.get(key) for key in key_names] if isinstance(item, dict) else item
            )
            if identity in seen:
                continue
            seen.add(identity)
            output.append(item)
        return normalize_canonical(
            {"items": output, "input_count": len(items), "output_count": len(output)}
        )
    if kind == "join_items":
        left = _extract_items(spec.get("left_items"), label=kind)
        right = _extract_items(spec.get("right_items"), label=kind)
        left_key = spec.get("left_key")
        right_key = spec.get("right_key")
        fields = spec.get("fields", {})
        if (
            not isinstance(left_key, str)
            or not isinstance(right_key, str)
            or not isinstance(fields, dict)
        ):
            raise PlaybillExecutionError("join_items keys and fields are malformed")
        joined_output: list[CanonicalValue] = []
        for left_item in left:
            for right_item in right:
                if not isinstance(left_item, dict) or not isinstance(right_item, dict):
                    continue
                if left_item.get(left_key) != right_item.get(right_key):
                    continue
                joined = {"left": left_item, "right": right_item}
                joined_output.append(
                    {
                        name: _resolve_template(
                            template,
                            input_payload={},
                            outputs={},
                            item=normalize_canonical(joined),
                        )
                        for name, template in fields.items()
                    }
                )
        return normalize_canonical({"items": joined_output, "output_count": len(joined_output)})
    if kind == "aggregate_items":
        items = _extract_items(spec.get("items"), label=kind)
        return normalize_canonical({"count": len(items)})
    raise PlaybillExecutionError(f"unsupported deterministic transform {kind!r}")


def _operand_value(
    operand: PredicateOperandV1,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    parameters: CanonicalValue,
) -> CanonicalValue:
    if operand.kind == "literal":
        return normalize_canonical(operand.value)
    if operand.kind == "parameter":
        if not isinstance(parameters, dict) or operand.parameter_name not in parameters:
            raise PlaybillExecutionError("guard parameter is absent")
        return normalize_canonical(parameters[operand.parameter_name])
    if operand.kind in {"input", "exists"} and operand.input_name is not None:
        if not isinstance(input_payload, dict) or operand.input_name not in input_payload:
            return False if operand.kind == "exists" else _missing_operand(operand.input_name)
        value = _resolve_path(
            input_payload[operand.input_name],
            operand.path,
            reference=operand.input_name,
        )
        return True if operand.kind == "exists" else normalize_canonical(value)
    if operand.alias is None or operand.alias not in outputs:
        return False if operand.kind == "exists" else _missing_operand(str(operand.alias))
    value = outputs[operand.alias]
    if operand.path:
        value = normalize_canonical(
            _resolve_path(value, operand.path, reference=f"$steps.{operand.alias}")
        )
    if operand.kind == "exists":
        return True
    if operand.kind == "count":
        return _item_count(value)
    if operand.kind == "truncated":
        return bool(isinstance(value, dict) and value.get("truncated") is True)
    return normalize_canonical(value)


def _missing_operand(name: str) -> CanonicalValue:
    raise PlaybillExecutionError(f"guard operand {name!r} is absent")


def _compare(left: CanonicalValue, operator: str, right: CanonicalValue) -> bool:
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if isinstance(left, bool) or isinstance(right, bool):
        raise PlaybillExecutionError("ordered guard operands cannot be booleans")
    if isinstance(left, int) and isinstance(right, int):
        comparable_left: int | str = left
        comparable_right: int | str = right
    elif isinstance(left, str) and isinstance(right, str):
        comparable_left = left
        comparable_right = right
    else:
        raise PlaybillExecutionError("ordered guard operands must share an int or string type")
    if operator in {"gt", "after"}:
        return comparable_left > comparable_right  # type: ignore[operator]
    if operator in {"gte", "on_or_after"}:
        return comparable_left >= comparable_right  # type: ignore[operator]
    if operator in {"lt", "before"}:
        return comparable_left < comparable_right  # type: ignore[operator]
    if operator in {"lte", "on_or_before"}:
        return comparable_left <= comparable_right  # type: ignore[operator]
    raise PlaybillExecutionError(f"unknown guard operator {operator!r}")


def _evaluate_predicate(
    predicate: GuardPredicateV1,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    parameters: CanonicalValue,
) -> tuple[bool, CanonicalValue]:
    if predicate.left is not None and predicate.right is not None and predicate.operator:
        left = _operand_value(
            predicate.left,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        right = _operand_value(
            predicate.right,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        verdict = _compare(left, predicate.operator, right)
        return verdict, normalize_canonical(
            {"left": left, "operator": predicate.operator, "right": right}
        )
    if predicate.not_of is not None:
        child, trace = _evaluate_predicate(
            predicate.not_of,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        return not child, {"not": trace}
    children = predicate.all_of if predicate.all_of is not None else predicate.any_of or ()
    results = [
        _evaluate_predicate(
            child,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        for child in children
    ]
    verdicts = [item[0] for item in results]
    verdict = all(verdicts) if predicate.all_of is not None else any(verdicts)
    return verdict, normalize_canonical(
        {"all" if predicate.all_of is not None else "any": [item[1] for item in results]}
    )


__all__ = [
    "AcceptedStateRunMaterialV1",
    "ContractValidatorProtocol",
    "PreparedProcedureRunV1",
    "ProcedureActivationAuthorityProtocol",
    "ProcedureClockProtocol",
    "ProcedureExecutor",
    "ProcedureNodePinSetV1",
    "ProcedureRunAdmissionV1",
    "ProcedureRunReceiptV1",
    "ProcedureRunRefusalV1",
    "ProcedureRunResultV1",
    "ProcedureRunStatusV1",
    "ProviderExecutorProtocol",
    "ProviderInvocationResultV1",
    "StateTapReaderProtocol",
    "SystemProcedureClock",
    "accepted_procedure_pin_set_digest",
    "prepare_direct_procedure_run",
    "procedure_admission_digest",
    "procedure_pin_set_digest",
    "procedure_run_receipt_digest",
    "run_value_digest",
]
