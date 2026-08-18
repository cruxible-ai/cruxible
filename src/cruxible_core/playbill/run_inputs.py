"""§8.5.2 Line run admission: freeze every input plane before a result is visible.

Admission is the moment the run stops being negotiable.  It binds the occurrence
and attempt, the three discriminated input planes, the nonsecret deployment and
provider/source binding snapshot, the acquisition-policy coordinate and its
deterministic selection receipt, the mandate and calibration reads, the budget,
the sensitivity policy, the derived taint, and epsilon membership, and only then
computes one admission-binding digest.  Nothing computed after that point may
change the tuple: a source step may still produce a Capture at a coordinate that
did not exist at admission, but that Capture belongs to the acquisition manifest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.acquisition_policies import (
    AcquisitionCandidateV1,
    SourceAcquisitionPolicyV1,
    SourceSelectionReceiptV1,
    acquisition_policy_digest,
    select_sources,
)
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.capture_journal import CaptureLandingEventV1
from cruxible_core.playbill.captures import CaptureContractV1, capture_contract_digest
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    ExhaustReducerProtocol,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    VerifiedExhaustRecordV1,
    parse_journal_payload,
    verify_journal_range,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineLeaseV1,
    LineRuntimeRefusal,
    line_deployment_digest,
)
from cruxible_core.playbill.occurrences import LineOccurrenceV1, line_occurrence_digest
from cruxible_core.playbill.procedures.acquisition import ProcedureSourceAcquirerProtocol
from cruxible_core.playbill.procedures.artifacts import AcceptedProcedureV1
from cruxible_core.playbill.procedures.closure import close_procedure_pin_slots
from cruxible_core.playbill.procedures.execution import (
    AcceptedStateRunMaterialV1,
    ExhaustRunMaterialV1,
    LandedCaptureRunMaterialV1,
    PreparedProcedureRunV1,
    ProcedureRunAdmissionV1,
    StateTapReaderProtocol,
    procedure_admission_digest,
    procedure_node_pin_sets,
    procedure_pin_set_digest,
    resolve_procedure_pin,
    run_value_digest,
)
from cruxible_core.playbill.procedures.input_planes import (
    AcceptedStateRunInputV1,
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
)
from cruxible_core.playbill.procedures.line_specs import AcceptedLineSpecV1
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    ProcedureBudgetV3,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.procedures.resolution import (
    AcceptedAuthorityBasisV1,
    resolve_authority_basis,
)
from cruxible_core.playbill.procedures.terminal_dependencies import (
    AcquisitionInputOutcomeV1,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.source_readers import ProducerBindingV1
from cruxible_core.temporal import ensure_utc

# ---------------------------------------------------------------------------
# Nonsecret binding vocabulary
# ---------------------------------------------------------------------------

#: Field-name vocabulary a binding snapshot may never carry.  A locator, a
#: credential, or a session handle is operational plumbing; it can never enter a
#: digest a Claim depends on, so the snapshot refuses it structurally.
CREDENTIAL_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "certificate",
        "connection",
        "cookie",
        "credential",
        "credentials",
        "dsn",
        "key",
        "passphrase",
        "password",
        "private",
        "secret",
        "session",
        "signature",
        "token",
        "url",
        "uri",
    }
)

_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)("
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|\bbearer\s+\S"
    r"|\bbasic\s+[A-Za-z0-9+/=]{8,}"
    r"|[a-z][a-z0-9+.-]*://[^/\s@]+:[^/\s@]+@"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bghp_[A-Za-z0-9]{20,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\bsk-[A-Za-z0-9]{20,}\b"
    r")"
)

_NAME_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def assert_nonsecret_binding(payload: object, *, label: str) -> None:
    """Refuse any credential-shaped name or value before a snapshot is digested."""

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, member in value.items():
                tokens = set(_NAME_SPLIT_RE.split(str(key).casefold()))
                if tokens & CREDENTIAL_NAME_TOKENS:
                    raise LineRuntimeRefusal(
                        "playbill.line.binding_snapshot_credential_shaped",
                        f"{label} field {path}.{key} is credential-shaped and is never bound.",
                    )
                visit(member, f"{path}.{key}")
            return
        if isinstance(value, list | tuple):
            for index, member in enumerate(value):
                visit(member, f"{path}[{index}]")
            return
        if isinstance(value, str) and _CREDENTIAL_VALUE_RE.search(value):
            raise LineRuntimeRefusal(
                "playbill.line.binding_snapshot_credential_shaped",
                f"{label} value at {path} is credential-shaped and is never bound.",
            )

    visit(normalize_canonical(payload), "$")


class _StrictAdmissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


# ---------------------------------------------------------------------------
# Nonsecret deployment / provider / source binding snapshot
# ---------------------------------------------------------------------------


class ProviderBindingSnapshotV1(_StrictAdmissionModel):
    """One provider/source binding described without a locator or a secret."""

    tag: Literal["playbill-provider-binding-snapshot-v1"] = "playbill-provider-binding-snapshot-v1"
    provider: ArtifactIdentity
    provider_artifact_digest: str
    producer_binding_digest: str
    logical_source_identity: str
    adapter_digest: str
    binding_epoch: int = Field(ge=0)
    capture_contract_digest: str

    _digests = field_validator(
        "provider_artifact_digest",
        "producer_binding_digest",
        "adapter_digest",
        "capture_contract_digest",
    )(_tagged)


def provider_binding_snapshot(
    *,
    binding: ProducerBindingV1,
    provider_artifact_digest: str,
    contract: CaptureContractV1,
) -> ProviderBindingSnapshotV1:
    return ProviderBindingSnapshotV1(
        provider=binding.provider,
        provider_artifact_digest=provider_artifact_digest,
        producer_binding_digest=binding.digest,
        logical_source_identity=binding.logical_source_identity,
        adapter_digest=binding.adapter_digest,
        binding_epoch=binding.binding_epoch,
        capture_contract_digest=capture_contract_digest(contract).tagged,
    )


class LineDeploymentBindingSnapshotV1(_StrictAdmissionModel):
    """The nonsecret operational binding one run was admitted against."""

    tag: Literal["playbill-line-deployment-binding-snapshot-v1"] = (
        "playbill-line-deployment-binding-snapshot-v1"
    )
    line_id: str
    deployment_id: str
    deployment_revision: int = Field(ge=1)
    deployment_digest: str
    runner_id: str
    runner_class: str
    backend_id: str
    backend_kind: str
    logical_stream: JournalStreamIdentityV1
    control_partition_id: str
    run_partition_id: str
    provider_bindings: tuple[ProviderBindingSnapshotV1, ...] = ()

    _deployment = field_validator("deployment_digest")(_tagged)

    @model_validator(mode="after")
    def _nonsecret(self) -> "LineDeploymentBindingSnapshotV1":
        ordered = tuple(
            sorted(
                self.provider_bindings,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        )
        if ordered != self.provider_bindings:
            raise ValueError("binding snapshot provider bindings must be canonically sorted")
        assert_nonsecret_binding(
            self.model_dump(mode="json"),
            label="Line deployment binding snapshot",
        )
        return self


def deployment_binding_snapshot_digest(snapshot: LineDeploymentBindingSnapshotV1) -> str:
    payload = snapshot.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        "playbill-line-deployment-binding-snapshot-v1",
        payload,
    ).tagged


def build_deployment_binding_snapshot(
    deployment: LineDeploymentV1,
    *,
    provider_bindings: tuple[ProviderBindingSnapshotV1, ...] = (),
) -> LineDeploymentBindingSnapshotV1:
    binding = deployment.journal_binding
    return LineDeploymentBindingSnapshotV1(
        line_id=deployment.line_id,
        deployment_id=deployment.deployment_id,
        deployment_revision=deployment.revision,
        deployment_digest=line_deployment_digest(deployment),
        runner_id=deployment.runner.runner_id,
        runner_class=deployment.runner.runner_class,
        backend_id=binding.backend_id,
        backend_kind=binding.backend_kind,
        logical_stream=binding.logical_stream,
        control_partition_id=binding.control_partition_id,
        run_partition_id=binding.run_partition_id,
        provider_bindings=tuple(
            sorted(
                provider_bindings,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Mandate and calibration coordinates
# ---------------------------------------------------------------------------


class ProcedureMandateReadV1(_StrictAdmissionModel):
    """One mandate read, resolved only through the accepted authority resolver."""

    tag: Literal["playbill-procedure-mandate-read-v1"] = "playbill-procedure-mandate-read-v1"
    accepted_coordinate: AcceptedCoordinate
    requested_basis_digests: tuple[str, ...] = ()
    resolved_basis_digests: tuple[str, ...] = ()
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _sorted(self) -> "ProcedureMandateReadV1":
        for values in (self.requested_basis_digests, self.resolved_basis_digests):
            if values != tuple(sorted(set(values))):
                raise ValueError("mandate basis digests must be sorted and unique")
        if not set(self.resolved_basis_digests).issubset(set(self.requested_basis_digests)):
            raise ValueError("resolved mandate basis must be a subset of the requested basis")
        return self


def mandate_read_digest(read: ProcedureMandateReadV1) -> str:
    payload = read.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-procedure-mandate-read-v1", payload).tagged


def read_mandate_basis(
    requested_basis_digests: tuple[str, ...],
    *,
    accepted_basis: Mapping[str, AcceptedAuthorityBasisV1],
    accepted_coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
) -> ProcedureMandateReadV1:
    """Bind the mandate coordinate through ``resolve_authority_basis`` and nothing else."""

    resolved = resolve_authority_basis(
        requested_basis_digests,
        accepted_basis=accepted_basis,
        evaluation_time=evaluation_time,
    )
    return ProcedureMandateReadV1(
        accepted_coordinate=accepted_coordinate,
        requested_basis_digests=requested_basis_digests,
        resolved_basis_digests=resolved,
        evaluated_at=evaluation_time,
    )


class ProcedureCalibrationReadV1(_StrictAdmissionModel):
    """One calibration coordinate; an absent calibration is bound explicitly."""

    tag: Literal["playbill-procedure-calibration-read-v1"] = (
        "playbill-procedure-calibration-read-v1"
    )
    accepted_coordinate: AcceptedCoordinate
    calibration_pin: ArtifactPin | None = None
    epsilon: object
    epsilon_member: bool

    @field_validator("epsilon", mode="before")
    @classmethod
    def _epsilon(cls, value: object) -> object:
        return normalize_canonical(value)


def calibration_read_digest(read: ProcedureCalibrationReadV1) -> str:
    payload = read.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-procedure-calibration-read-v1", payload).tagged


def epsilon_membership(
    *,
    line_id: str,
    occurrence_epoch: int,
    occurrence_digest: str,
    epsilon: object,
) -> bool:
    """Draw epsilon membership from occurrence identity alone; retries never move it."""

    if not isinstance(epsilon, dict) or tuple(epsilon) != ("$decimal",):
        raise LineRuntimeRefusal(
            "playbill.line.epsilon_not_canonical",
            "LineSpec epsilon must be a canonical decimal wrapper.",
        )
    try:
        fraction = Fraction(Decimal(str(epsilon["$decimal"])))
    except (InvalidOperation, ValueError) as exc:
        raise LineRuntimeRefusal(
            "playbill.line.epsilon_not_canonical",
            "LineSpec epsilon is not a finite decimal.",
        ) from exc
    if fraction <= 0:
        return False
    if fraction >= 1:
        return True
    draw = int(
        typed_digest(
            Sha256Value,
            "playbill-line-epsilon-membership-v1",
            {
                "line_id": line_id,
                "occurrence_digest": occurrence_digest,
                "occurrence_epoch": occurrence_epoch,
            },
        ).tagged.split(":", 1)[1],
        16,
    )
    return draw * fraction.denominator < fraction.numerator * (1 << 256)


# ---------------------------------------------------------------------------
# Sensitivity policy
# ---------------------------------------------------------------------------


class ProcedureInputSensitivityV1(_StrictAdmissionModel):
    tag: Literal["playbill-procedure-input-sensitivity-v1"] = (
        "playbill-procedure-input-sensitivity-v1"
    )
    input_name: str
    capture_contract_digest: str
    selector_privacy: Literal["direct_allowed", "pseudonymous_required"]
    body_retention: Literal["never_materialize", "optional", "required_for_duration"]
    erasure: Literal["prohibited", "authorized_by_rule"]

    _digest = field_validator("capture_contract_digest")(_tagged)


class ProcedureSensitivityPolicyV1(_StrictAdmissionModel):
    """The exact retention/erasure/privacy law each admitted Capture input carries."""

    tag: Literal["playbill-procedure-sensitivity-policy-v1"] = (
        "playbill-procedure-sensitivity-policy-v1"
    )
    inputs: tuple[ProcedureInputSensitivityV1, ...] = ()

    @field_validator("inputs")
    @classmethod
    def _sorted(
        cls, value: tuple[ProcedureInputSensitivityV1, ...]
    ) -> tuple[ProcedureInputSensitivityV1, ...]:
        names = tuple(item.input_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("sensitivity policy inputs must be sorted and unique")
        return value


def sensitivity_policy_digest(policy: ProcedureSensitivityPolicyV1) -> str:
    payload = policy.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-procedure-sensitivity-policy-v1", payload).tagged


def build_sensitivity_policy(
    contracts_by_input: Mapping[str, CaptureContractV1],
) -> ProcedureSensitivityPolicyV1:
    entries = []
    for input_name in sorted(contracts_by_input, key=lambda item: item.encode("utf-8")):
        contract = contracts_by_input[input_name]
        retention = contract.retention_erasure_policy
        entries.append(
            ProcedureInputSensitivityV1(
                input_name=input_name,
                capture_contract_digest=capture_contract_digest(contract).tagged,
                selector_privacy=retention.selector_privacy,
                body_retention=retention.body_retention,
                erasure=retention.erasure,
            )
        )
    return ProcedureSensitivityPolicyV1(inputs=tuple(entries))


# ---------------------------------------------------------------------------
# Exhaust-tap admission reads
# ---------------------------------------------------------------------------


def journal_cursor(partition_id: str, sequence: int) -> str:
    return f"playbill-journal-cursor-v1:{partition_id}:{sequence:020d}"


class ProcedureExhaustTapReadV1(_StrictAdmissionModel):
    """One authenticated exhaust range and the value its exact reducer produced."""

    tag: Literal["playbill-procedure-exhaust-tap-read-v1"] = (
        "playbill-procedure-exhaust-tap-read-v1"
    )
    journal_identity: str
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    chain_head_digest: str
    reducer_digest: str
    value: object

    _digests = field_validator("chain_head_digest", "reducer_digest")(_tagged)

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _range(self) -> "ProcedureExhaustTapReadV1":
        if self.first_sequence > self.last_sequence:
            raise ValueError("exhaust tap range must be increasing")
        return self

    @property
    def first_cursor(self) -> str:
        return journal_cursor(self.partition_id, self.first_sequence)

    @property
    def last_cursor(self) -> str:
        return journal_cursor(self.partition_id, self.last_sequence)


@runtime_checkable
class ProcedureExhaustTapReaderProtocol(Protocol):
    def read_exhaust_tap(
        self,
        *,
        journal_identity: str,
        reducer_or_query: ArtifactPin,
    ) -> ProcedureExhaustTapReadV1: ...


class JournalExhaustTapReader:
    """Read one exhaust range through the verified journal seam, never a cache."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        instance_id: str,
        partition_id: str,
        reducers: Mapping[str, ExhaustReducerProtocol],
        access: BodyAccessContext | None = None,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.instance_id = instance_id
        self.partition_id = partition_id
        self.reducers = reducers
        self.access = access or BodyAccessContext(
            principal_id="procedure-exhaust-tap",
            can_read_body=True,
        )

    def read_exhaust_tap(
        self,
        *,
        journal_identity: str,
        reducer_or_query: ArtifactPin,
    ) -> ProcedureExhaustTapReadV1:
        reducer = self.reducers.get(reducer_or_query.artifact_digest)
        if reducer is None or reducer.reducer_digest != reducer_or_query.artifact_digest:
            raise LineRuntimeRefusal(
                "playbill.line.exhaust_reducer_unresolved",
                "No registered reducer reproduces this exhaust_tap pin.",
            )
        stream = JournalStreamIdentityV1(
            instance_id=self.instance_id,
            journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
            stream_id=journal_identity,
        )
        head = self.journal.read_head(stream, self.partition_id)
        if head.sequence == 0:
            raise LineRuntimeRefusal(
                "playbill.line.exhaust_range_empty",
                "An exhaust_tap input requires one nonempty authenticated range.",
            )
        journal_range = self.journal.range_from_sequences(
            stream,
            self.partition_id,
            first_sequence=1,
            last_sequence=head.sequence,
        )
        records = self.journal.read_exact_range(journal_range)
        verify_journal_range(journal_range, records)
        verified = tuple(
            VerifiedExhaustRecordV1(
                record_digest=stored.record_digest,
                sequence=stored.record.sequence,
                event_kind=stored.record.event_kind,
                generation_digest=stored.record.accepted_coordinate.generation_root,
                payload_digest=stored.record.payload_digest,
                payload=parse_journal_payload(
                    self.bodies.read(stored.record.payload_digest, access=self.access)
                ),
            )
            for stored in records
        )
        return ProcedureExhaustTapReadV1(
            journal_identity=journal_identity,
            partition_id=self.partition_id,
            first_sequence=journal_range.first_sequence,
            last_sequence=journal_range.last_sequence,
            chain_head_digest=journal_range.expected_head_digest,
            reducer_digest=reducer.reducer_digest,
            value=normalize_canonical(reducer.reduce(verified)),
        )


# ---------------------------------------------------------------------------
# Deterministic multi-source selection through PC-C's planner
# ---------------------------------------------------------------------------


class LineSourceSelectionV1(_StrictAdmissionModel):
    """What PC-C's planner decided about the landed plane, before any live read."""

    tag: Literal["playbill-line-source-selection-v1"] = "playbill-line-source-selection-v1"
    receipt: SourceSelectionReceiptV1 | None = None
    landed_inputs: tuple[LandedCaptureRunInputV1, ...] = ()
    outcomes: tuple[AcquisitionInputOutcomeV1, ...] = ()
    deferred_input_names: tuple[str, ...] = ()

    @field_validator("deferred_input_names")
    @classmethod
    def _deferred(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("deferred acquisition inputs must be sorted and unique")
        return value


def select_line_run_sources(
    policy: SourceAcquisitionPolicyV1,
    candidates: tuple[AcquisitionCandidateV1, ...],
    *,
    anchor: CaptureLandingEventV1 | None,
    evaluation_time: datetime,
    source_input_names: frozenset[str],
    default_authorizations: tuple[str, ...] = (),
) -> LineSourceSelectionV1:
    """Run PC-C's planner over the landed plane and defer only what it cannot decide.

    An input the landed plane cannot serve is *not* an acquisition result.  When a
    ``source`` node serves that input, the decision defers to the real read at
    execution, where ``on_unavailable``/``on_stale`` legitimately apply.  An input
    with no source node is landed-only, so the planner's decision is final.
    """

    if anchor is None:
        if candidates:
            raise LineRuntimeRefusal(
                "playbill.line.selection_anchor_absent",
                "Landed-plane candidates require the exact anchor cursor that cut them.",
            )
        missing = tuple(
            sorted(
                {rule.input_name for rule in policy.inputs} - source_input_names,
                key=lambda item: item.encode("utf-8"),
            )
        )
        if missing:
            raise LineRuntimeRefusal(
                "playbill.line.selection_anchor_absent",
                f"Landed-only acquisition inputs {list(missing)} have no anchor to select from.",
            )
        return LineSourceSelectionV1(
            deferred_input_names=tuple(
                sorted(
                    {rule.input_name for rule in policy.inputs},
                    key=lambda item: item.encode("utf-8"),
                )
            )
        )
    receipt = select_sources(
        policy,
        candidates,
        anchor=anchor,
        evaluation_time=evaluation_time,
        default_authorizations=default_authorizations,
    )
    by_digest = {candidate.capture_digest: candidate for candidate in candidates}
    landed: list[LandedCaptureRunInputV1] = []
    outcomes: list[AcquisitionInputOutcomeV1] = []
    deferred: list[str] = []
    for decision in receipt.decisions:
        if decision.input_name == "coherence":
            raise LineRuntimeRefusal(
                "playbill.line.selection_incoherent",
                f"Acquisition coherence refused: {list(decision.reason_codes)}",
            )
        if decision.disposition == "selected":
            for digest in decision.selected_capture_digests:
                candidate = by_digest[digest]
                landed.append(
                    LandedCaptureRunInputV1(
                        input_name=decision.input_name,
                        capture_digest=digest,
                        capture_contract_digest=(candidate.envelope.capture_contract_digest),
                        landing_cursor=candidate.landing_event.cursor,
                    )
                )
            outcomes.append(
                AcquisitionInputOutcomeV1(
                    input_name=decision.input_name,
                    disposition="selected",
                    capture_digests=tuple(sorted(decision.selected_capture_digests)),
                )
            )
            continue
        if decision.input_name in source_input_names:
            deferred.append(decision.input_name)
            continue
        if decision.disposition == "refused":
            raise LineRuntimeRefusal(
                "playbill.line.acquisition_refused",
                f"Acquisition input {decision.input_name!r} refused: {list(decision.reason_codes)}",
            )
        outcomes.append(
            AcquisitionInputOutcomeV1(
                input_name=decision.input_name,
                disposition=decision.disposition,
            )
        )
    return LineSourceSelectionV1(
        receipt=receipt,
        landed_inputs=tuple(sorted(landed, key=lambda item: item.input_name.encode("utf-8"))),
        outcomes=tuple(sorted(outcomes, key=lambda item: item.input_name.encode("utf-8"))),
        deferred_input_names=tuple(sorted(set(deferred), key=lambda item: item.encode("utf-8"))),
    )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def line_run_budget(accepted_line: AcceptedLineSpecV1) -> ProcedureBudgetV3:
    """Read the run budget from the accepted LineSpec, never from a runtime hint."""

    budgets = accepted_line.line.budgets
    if not isinstance(budgets, dict):  # pragma: no cover - LineSpec law forbids it
        raise LineRuntimeRefusal(
            "playbill.line.budget_not_canonical",
            "LineSpec budgets must be a canonical object.",
        )
    try:
        return ProcedureBudgetV3(
            wall_clock={"microseconds": int(budgets["max_wall_clock_microseconds"])},  # type: ignore[arg-type]
            max_provider_calls=int(budgets["max_provider_calls"]),
            max_capture_bytes=int(budgets["max_capture_bytes"]),
            max_items=int(budgets["max_items"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LineRuntimeRefusal(
            "playbill.line.budget_not_canonical",
            "LineSpec budgets do not close the Procedure budget shape.",
        ) from exc


def admit_line_procedure_run(
    *,
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
    policy: SourceAcquisitionPolicyV1,
    deployment: LineDeploymentV1,
    lease: LineLeaseV1,
    occurrence: LineOccurrenceV1,
    attempt: int,
    run_id: str,
    accepted_coordinate: AcceptedCoordinate,
    invocation_input: object,
    actor_context: GovernedActorContext,
    state_reader: StateTapReaderProtocol,
    selection: LineSourceSelectionV1,
    binding_snapshot: LineDeploymentBindingSnapshotV1,
    mandate_read: ProcedureMandateReadV1,
    sensitivity_policy: ProcedureSensitivityPolicyV1,
    interface_digests: Mapping[str, str],
    admitted_at: datetime,
    exhaust_reader: ProcedureExhaustTapReaderProtocol | None = None,
    acquirer: ProcedureSourceAcquirerProtocol | None = None,
    calibration_pin: ArtifactPin | None = None,
    taint_labels: tuple[str, ...] = (),
) -> PreparedProcedureRunV1:
    """Freeze one Line run's complete admission tuple before any node can fire."""

    line = accepted_line.line
    if accepted_line.artifact_digest != deployment.line_spec_digest:
        raise LineRuntimeRefusal(
            "playbill.line.deployment_spec_mismatch",
            "Accepted LineSpec is not the digest this deployment is bound to.",
        )
    if lease.deployment_digest != line_deployment_digest(deployment):
        raise LineRuntimeRefusal(
            "playbill.line.lease_not_current",
            "Lease was granted against a different deployment revision.",
        )
    if occurrence.line_id != deployment.line_id or (
        occurrence.occurrence_epoch != line.occurrence_epoch
    ):
        raise LineRuntimeRefusal(
            "playbill.line.occurrence_epoch_stale",
            "Occurrence names another Line identity or a stale occurrence epoch.",
        )
    if line.procedure.artifact_digest != accepted_procedure.artifact_digest:
        raise LineRuntimeRefusal(
            "playbill.line.procedure_pin_mismatch",
            "Accepted Procedure is not the exact artifact the LineSpec pins.",
        )
    if line.acquisition_policy is None or line.acquisition_policy.artifact_digest != (
        acquisition_policy_digest(policy).tagged
    ):
        raise LineRuntimeRefusal(
            "playbill.line.acquisition_policy_mismatch",
            "Accepted SourceAcquisitionPolicy is not the exact artifact the LineSpec pins.",
        )
    if binding_snapshot.deployment_digest != line_deployment_digest(deployment):
        raise LineRuntimeRefusal(
            "playbill.line.binding_snapshot_stale",
            "Binding snapshot was taken against another deployment revision.",
        )

    closure = close_procedure_pin_slots(
        accepted_procedure.procedure,
        bindings=line.slot_bindings,
        interface_digests=interface_digests,
    )
    slot_pins = {binding.slot_name: binding.artifact_pin for binding in line.slot_bindings}
    definition = accepted_procedure.procedure.definition

    materials: list[AcceptedStateRunMaterialV1] = []
    for node in definition.nodes:
        if not isinstance(node, StateTapNodeV3):
            continue
        query = resolve_procedure_pin(
            node.query, label=f"state_tap {node.node_id!r}", slot_pins=slot_pins
        )
        parameters = normalize_canonical(node.parameters)
        value = normalize_canonical(
            state_reader.read_accepted_state(
                query=query,
                parameters=parameters,
                coordinate=accepted_coordinate,
            )
        )
        materials.append(
            AcceptedStateRunMaterialV1(
                input=AcceptedStateRunInputV1(
                    input_name=node.as_,
                    read_coordinate=accepted_coordinate,
                    query_definition_digest=query.artifact_digest,
                    parameters_digest=run_value_digest("state-parameters", parameters),
                    result_digest=run_value_digest("state-result", value),
                ),
                value=value,
            )
        )
    materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))

    exhaust_materials: list[ExhaustRunMaterialV1] = []
    for node in definition.nodes:
        if not isinstance(node, ExhaustTapNodeV3):
            continue
        if exhaust_reader is None:
            raise LineRuntimeRefusal(
                "playbill.line.exhaust_reader_absent",
                "An exhaust_tap node requires an authenticated exhaust reader at admission.",
            )
        reducer = resolve_procedure_pin(
            node.reducer_or_query,
            label=f"exhaust_tap {node.node_id!r}",
            slot_pins=slot_pins,
        )
        read = exhaust_reader.read_exhaust_tap(
            journal_identity=node.journal_identity,
            reducer_or_query=reducer,
        )
        exhaust_materials.append(
            ExhaustRunMaterialV1(
                input=ExhaustRunInputV1(
                    input_name=node.as_,
                    journal_identity=node.journal_identity,
                    first_cursor=read.first_cursor,
                    last_cursor=read.last_cursor,
                    reducer_or_query_digest=reducer.artifact_digest,
                    result_digest=run_value_digest("exhaust-result", read.value),
                ),
                value=read.value,
            )
        )
    exhaust_materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))

    source_aliases = {node.as_ for node in definition.nodes if isinstance(node, SourceNodeV3)}
    landed_materials: list[LandedCaptureRunMaterialV1] = []
    for landed in selection.landed_inputs:
        if landed.input_name not in source_aliases:
            raise LineRuntimeRefusal(
                "playbill.line.landed_capture_unconsumed",
                f"Selected Capture {landed.input_name!r} names no source node in this Procedure.",
            )
        if acquirer is None:
            raise LineRuntimeRefusal(
                "playbill.line.capture_dereference_unavailable",
                "Admitting a landed Capture requires the Capture dereference seam.",
            )
        landed_materials.append(
            LandedCaptureRunMaterialV1(
                input=landed,
                material=acquirer.dereference(landed.capture_digest),
            )
        )
    landed_materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))

    occurrence_digest = line_occurrence_digest(occurrence)
    member = epsilon_membership(
        line_id=line.identity.name,
        occurrence_epoch=line.occurrence_epoch,
        occurrence_digest=occurrence_digest,
        epsilon=line.epsilon,
    )
    calibration = ProcedureCalibrationReadV1(
        accepted_coordinate=accepted_coordinate,
        calibration_pin=calibration_pin,
        epsilon=line.epsilon,
        epsilon_member=member,
    )
    node_pin_sets = procedure_node_pin_sets(accepted_procedure, slot_pins)
    full_pins = closure.exact_pins
    pin_digest = procedure_pin_set_digest(full_pins, node_pin_sets)
    fields: dict[str, object] = {
        "instance_id": deployment.instance_id,
        "run_id": run_id,
        "attempt": attempt,
        "accepted_coordinate": accepted_coordinate,
        "procedure_identity": accepted_procedure.procedure.identity,
        "procedure_path": accepted_procedure.path,
        "procedure_artifact_digest": accepted_procedure.artifact_digest,
        "definition_digest": accepted_procedure.procedure.definition_digest,
        "activation_policy": accepted_procedure.procedure.activation_policy,
        "full_pins": full_pins,
        "node_pin_sets": node_pin_sets,
        "pin_set_digest": pin_digest,
        "invocation_input": normalize_canonical(invocation_input),
        "accepted_state_inputs": tuple(item.input for item in materials),
        "landed_capture_inputs": tuple(item.input for item in landed_materials),
        "exhaust_inputs": tuple(item.input for item in exhaust_materials),
        "budget": line_run_budget(accepted_line),
        "hard_caps": definition.hard_caps,
        "actor_context": actor_context,
        "invocation_origin": "line",
        "journal_stream": deployment.journal_binding.logical_stream,
        "journal_partition_id": deployment.journal_binding.run_partition_id,
        "line_spec_digest": accepted_line.artifact_digest,
        "occurrence_id": occurrence_digest,
        "deployment_snapshot_digest": deployment_binding_snapshot_digest(binding_snapshot),
        "acquisition_policy_digest": acquisition_policy_digest(policy).tagged,
        "selection_receipt_digest": (
            None if selection.receipt is None else selection.receipt.digest
        ),
        "sensitivity_policy_digest": sensitivity_policy_digest(sensitivity_policy),
        "mandate_coordinate_digest": mandate_read_digest(mandate_read),
        "calibration_coordinate_digest": calibration_read_digest(calibration),
        "taint_labels": tuple(sorted(set(taint_labels), key=lambda item: item.encode("utf-8"))),
        "epsilon_member": member,
        "admitted_at": ensure_utc(admitted_at),
    }
    provisional = ProcedureRunAdmissionV1.model_construct(
        _fields_set=None,
        admission_binding_digest="sha256:" + "0" * 64,
        **fields,
    )
    admission = ProcedureRunAdmissionV1.model_validate(
        {**fields, "admission_binding_digest": procedure_admission_digest(provisional)}
    )
    return PreparedProcedureRunV1(
        admission=admission,
        accepted_state_materials=tuple(materials),
        landed_capture_materials=tuple(landed_materials),
        exhaust_materials=tuple(exhaust_materials),
        acquisition_outcomes=selection.outcomes,
    )


def line_run_slot_pins(accepted_line: AcceptedLineSpecV1) -> dict[str, ArtifactPin]:
    """Return the exact slot closure one accepted LineSpec supplies to the executor."""

    return {binding.slot_name: binding.artifact_pin for binding in accepted_line.line.slot_bindings}


__all__ = [
    "CREDENTIAL_NAME_TOKENS",
    "JournalExhaustTapReader",
    "LineDeploymentBindingSnapshotV1",
    "LineSourceSelectionV1",
    "ProcedureCalibrationReadV1",
    "ProcedureExhaustTapReadV1",
    "ProcedureExhaustTapReaderProtocol",
    "ProcedureInputSensitivityV1",
    "ProcedureMandateReadV1",
    "ProcedureSensitivityPolicyV1",
    "ProviderBindingSnapshotV1",
    "admit_line_procedure_run",
    "assert_nonsecret_binding",
    "build_deployment_binding_snapshot",
    "build_sensitivity_policy",
    "calibration_read_digest",
    "deployment_binding_snapshot_digest",
    "epsilon_membership",
    "journal_cursor",
    "line_run_budget",
    "line_run_slot_pins",
    "mandate_read_digest",
    "provider_binding_snapshot",
    "read_mandate_basis",
    "select_line_run_sources",
    "sensitivity_policy_digest",
]
