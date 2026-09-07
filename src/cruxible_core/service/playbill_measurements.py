"""Production measurement resolution and exact-grain Procedure readings.

The declaration, the resolution law, and the reading law already exist as
frozen kernels. This module is the served producer that connects them to real
evidence:

1. derive the ACTIVATION of every declared measurement from the generation that
   accepted the exact Procedure revision (its signed acceptance instant is
   ``activated_at``; nothing restarts that window per run or per poll);
2. decide eligibility at an explicit OBSERVATION instant and coordinate --
   before ``check_at`` the measurement is pending, at or after ``expires_at``
   it is expired, and neither writes anything;
3. inside the window, gather evidence through the shared query, Claim verdict,
   and attestation paths, evaluate the frozen resolution law, and append one
   resolution per activation (the latest non-overturned answer governs; a
   standing answer is returned, never re-derived);
4. when a real run is named, bind that standing resolution to the grain the run
   actually reached -- unit, node, or arm -- as one contract-grade reading,
   keyed so a retry replays the same record and a repeated Line occurrence
   attempt never earns a second credit.

Everything written here is operational exhaust in the Procedure journal. It is
not accepted state, it promotes nothing, and inspection never writes.
"""

from __future__ import annotations

import base64
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.claim_attestation_store import ClaimAttestationEventPayloadV1
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultV1, claim_verdict_v1_compat
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    claim_artifact_digest,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    PlaybillCasError,
    PlaybillError,
    PlaybillExecutionError,
    PlaybillFormatError,
    PlaybillJournalConflictError,
    ProposalIntegrityError,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.measurements import (
    AcceptedQueryProcedureMeasurementV1,
    ClaimAttestationProcedureMeasurementV1,
    ClaimStatementProcedureMeasurementV1,
    ProcedureMeasurementDeclarationV1,
)
from cruxible_client.contracts.procedures.readings import (
    PlaybillProcedureMeasureRequestV1,
    PlaybillProcedureMeasureResultV1,
    PlaybillProcedureReadingsRequestV1,
    PlaybillProcedureReadingsResultV1,
    ProcedureMeasurementContractStatusV1,
    ProcedureMeasurementEligibilityV1,
    ProcedureMeasurementRefusalCodeV1,
    ProcedureMeasurementResolutionSummaryV1,
    ProcedureMeasurementRowV1,
    ProcedureMeasurementStatusV1,
    ProcedureReadingStatusV1,
    ProcedureReadingSummaryV1,
)
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, parse_datetime
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.exhaust.records import (
    CLAIM_VERDICT_OBSERVATION_EVENT_KIND,
    QUERY_RECEIPT_EVENT_KIND,
    QUERY_RECEIPT_JOURNAL_FAMILY,
    JournalPartitionHeadV1,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.material_reservations import ProcedureMaterialReservationStore
from cruxible_core.playbill.procedures.readings import (
    ProcedureReadingV1,
    ReadingReplayKey,
    append_procedure_reading,
    build_procedure_reading,
    procedure_reading_digest,
    procedure_reading_partition_id,
    reading_replay_key,
)
from cruxible_core.playbill.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    ProcedureResolutionV1,
    ProcedureResolutionV2,
    ResolutionContractActivationV1,
    _expectation_holds,
    append_procedure_resolution,
    build_procedure_resolution,
    derive_resolution_activations,
    procedure_resolution_digest,
    resolution_contract_partition_id,
)
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.query.engine import QueryExecutionReceiptV1
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.service.playbill_evidence import (
    _claim_read_history_index,
    current_verified_claim_attestations,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.playbill_procedure_runs import (
    ProcedureNotFound,
    ProcedureRunGrainRecordV1,
    ProcedureSurfaceError,
    load_playbill_procedure_run_grain,
)
from cruxible_core.service.playbill_query import (
    PlaybillQueryReceiptJournal,
    service_run_playbill_query,
)

_PROCEDURE_JOURNAL = "procedure-runs"
_PROCEDURE_STREAM = "procedures"
_QUERY_RECEIPT_JOURNAL = "query-receipts"
_QUERY_RECEIPT_STREAM = "measurements"
_QUERY_RECEIPT_PARTITION = "default"
_WRITER_TOKEN = "playbill-procedure-measurement-v1"
_ACCESS = BodyAccessContext(principal_id="playbill-measurement", can_read_body=True)
_QUERY_BUDGET_KEYS = frozenset(QueryBudgetsV1.model_fields) - {"tag"}
_ACTIVATION_MEMO_CAPACITY = 32
_READING_INDEX_MEMO_CAPACITY = 16
MEASUREMENT_QUERY_EVIDENCE_TAG = "playbill-measurement-query-evidence-v1"
MEASUREMENT_ATTESTATION_EVIDENCE_TAG = "playbill-measurement-attestation-evidence-v1"
MEASUREMENT_CLAIM_VERDICT_OBSERVATION_TAG = "playbill-measurement-claim-verdict-observation-v1"
# A reading append is a compare-and-set on the partition head; a head that
# keeps moving under one request is reported, never spun on.
_APPEND_ATTEMPTS = 3
# Request attribution a fresh authenticated retry legitimately re-mints. The
# principal (actor_type, actor_id, org_id) stays part of the reading's meaning.
_VOLATILE_ACTOR_FIELDS = ("timestamp", "operation_id", "request_id")


class ProcedureMeasurementRefused(ProcedureSurfaceError):
    """A closed, repair-carrying measurement refusal (a request fault, never daemon)."""

    code = "playbill.procedure.measurement.refused"

    def __init__(
        self,
        error_code: ProcedureMeasurementRefusalCodeV1,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.error_code = error_code
        self.details = dict(details or {})
        super().__init__(f"{error_code}: {message}")


def _refuse(
    code: ProcedureMeasurementRefusalCodeV1,
    message: str,
    **details: object,
) -> ProcedureMeasurementRefused:
    return ProcedureMeasurementRefused(code, message, details=details)


# ---------------------------------------------------------------------------
# Journal access
# ---------------------------------------------------------------------------


def _journal(instance: PlaybillInstance) -> tuple[LocalJournalBackend, JournalStreamIdentityV1]:
    root = instance.root / instance.descriptor.storage.exhaust / _PROCEDURE_JOURNAL
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return LocalJournalBackend(root), JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id=_PROCEDURE_STREAM,
    )


def _query_receipt_journal(
    instance: PlaybillInstance,
) -> tuple[LocalJournalBackend, JournalStreamIdentityV1]:
    root = instance.root / instance.descriptor.storage.exhaust / _QUERY_RECEIPT_JOURNAL
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return LocalJournalBackend(root), JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=QUERY_RECEIPT_JOURNAL_FAMILY,
        stream_id=_QUERY_RECEIPT_STREAM,
    )


class _FencedWriter:
    """Hold the measurement writer fence on one partition for one request.

    The fence is taken once per partition and released when the request ends,
    so a request that appends a resolution and several readings does not pay a
    fence round-trip per record, and a request that crashes leaves a fence a
    later request steals exactly as the prediction settlement path does.
    """

    def __init__(self, instance: PlaybillInstance, journal: LocalJournalBackend) -> None:
        self._journal = journal
        self._bodies = instance.body_store()
        self._held: list[tuple[JournalStreamIdentityV1, str]] = []
        self.writer = ProcedureExhaustWriter(
            journal=journal,
            bodies=self._bodies,
            fencing_token=_WRITER_TOKEN,
        )

    def acquire(self, stream: JournalStreamIdentityV1, partition_id: str) -> None:
        if (stream, partition_id) in self._held:
            return
        state = self._journal.writer_state(stream, partition_id)
        if state is not None and state.active and state.fencing_token != _WRITER_TOKEN:
            self._journal.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=state.fencing_token,
            )
            state = self._journal.writer_state(stream, partition_id)
        if state is None or not state.active:
            self._journal.activate_writer(
                stream,
                partition_id,
                fencing_token=_WRITER_TOKEN,
                expected_head=self._journal.read_head(stream, partition_id),
            )
        self._held.append((stream, partition_id))

    def release(self) -> None:
        while self._held:
            stream, partition_id = self._held.pop()
            state = self._journal.writer_state(stream, partition_id)
            if state is not None and state.active and state.fencing_token == _WRITER_TOKEN:
                self._journal.fence_writer(
                    stream,
                    partition_id,
                    expected_fencing_token=_WRITER_TOKEN,
                )


# ---------------------------------------------------------------------------
# Activation derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementActivationBasisV1:
    """The generation that accepted this exact Procedure revision."""

    coordinate: AcceptedCoordinate
    activated_at: datetime
    activations: tuple[ResolutionContractActivationV1, ...]


_activation_memo: OrderedDict[tuple[str, str, str], MeasurementActivationBasisV1] = OrderedDict()


def _accepted_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    coordinate: AcceptedProjectionCoordinate,
) -> AcceptedProcedureV1:
    path = procedure_path(name)
    content = instance.tree_at(coordinate.git_oid).get(path)
    if content is None:
        raise ProcedureNotFound(f"{ProcedureNotFound.code}: {name}")
    procedure = parse_procedure(content, path=path)
    return AcceptedProcedureV1(
        path=path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _accepting_generation(
    instance: PlaybillInstance,
    *,
    accepted: AcceptedProcedureV1,
    observation: AcceptedProjectionCoordinate,
) -> tuple[AcceptedCoordinate, datetime]:
    """Find the generation, at or before the observation, that accepted this revision.

    The rule is the projection's own: the change set whose member carries
    exactly this artifact digest at this path, and its signed candidate
    instant. Accepted history is immutable, so this is a pure function of
    (artifact digest, observation OID).
    """

    observation_seen = False
    found: tuple[AcceptedCoordinate, datetime] | None = None
    for generation in instance.accepted_history():
        record = generation.record
        if record is not None:
            members = getattr(record, "members", ())
            if any(
                getattr(member, "path", None) == accepted.path
                and getattr(member, "candidate_artifact_digest", None) == accepted.artifact_digest
                for member in members
            ):
                found = (
                    AcceptedCoordinate.from_internal(instance.coordinate_for_oid(generation.oid)),
                    instance.accepted_evaluation_time(generation.oid),
                )
        if generation.oid == observation.git_oid:
            observation_seen = True
            break
    if not observation_seen:
        raise PlaybillFormatError("observation coordinate is outside accepted history")
    if found is None:
        raise PlaybillFormatError(
            "accepted Procedure revision has no accepting generation before the observation"
        )
    return found


def measurement_activation_basis(
    instance: PlaybillInstance,
    *,
    accepted: AcceptedProcedureV1,
    observation: AcceptedProjectionCoordinate,
) -> MeasurementActivationBasisV1:
    """Derive every activation once per (instance, revision, observation OID)."""

    key = (str(instance.root), accepted.artifact_digest, observation.git_oid)
    cached = _activation_memo.get(key)
    if cached is not None:
        _activation_memo.move_to_end(key)
        return cached
    coordinate, activated_at = _accepting_generation(
        instance,
        accepted=accepted,
        observation=observation,
    )
    basis = MeasurementActivationBasisV1(
        coordinate=coordinate,
        activated_at=activated_at,
        activations=derive_resolution_activations(
            accepted,
            accepted_coordinate=coordinate,
            activated_at=activated_at,
        ),
    )
    _activation_memo[key] = basis
    while len(_activation_memo) > _ACTIVATION_MEMO_CAPACITY:
        _activation_memo.popitem(last=False)
    return basis


def _window(
    activation: ResolutionContractActivationV1,
    observation_time: datetime,
) -> Literal["before_check", "open", "closed"]:
    if observation_time < activation.check_at:
        return "before_check"
    if observation_time >= activation.expires_at:
        return "closed"
    return "open"


def _eligibility(
    activation: ResolutionContractActivationV1,
    *,
    observation: AcceptedCoordinate,
    observation_time: datetime,
) -> ProcedureMeasurementEligibilityV1:
    return ProcedureMeasurementEligibilityV1(
        activation_coordinate=activation.subject.accepted_coordinate,
        activated_at=activation.activated_at,
        check_at=activation.check_at,
        expires_at=activation.expires_at,
        observation_coordinate=observation,
        observation_time=observation_time,
        window=_window(activation, observation_time),
    )


# ---------------------------------------------------------------------------
# Resolution partition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ContractState:
    activation: ResolutionContractActivationV1
    records: tuple[StoredProcedureJournalRecordV1, ...]
    book: ProcedureResolutionBook
    latest: ProcedureResolutionV1 | ProcedureResolutionV2 | None
    latest_record: StoredProcedureJournalRecordV1 | None


def _contract_state(
    instance: PlaybillInstance,
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    activation: ResolutionContractActivationV1,
) -> _ContractState:
    records = journal.all_records(stream, resolution_contract_partition_id(activation))
    book = ProcedureResolutionBook((activation,))
    book.replay(records, bodies=instance.body_store())
    latest = book.latest_non_overturned(activation.contract_id)
    latest_record = None
    if latest is not None:
        for stored in records:
            if stored.record.event_kind != "resolution":
                continue
            payload = parse_journal_payload(
                instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
            )
            if isinstance(payload, dict) and payload.get("resolution_id") == latest.resolution_id:
                latest_record = stored
                break
    return _ContractState(
        activation=activation,
        records=records,
        book=book,
        latest=latest,
        latest_record=latest_record,
    )


def _resolution_summary(
    state: _ContractState,
    *,
    written_now: bool,
) -> ProcedureMeasurementResolutionSummaryV1 | None:
    if state.latest is None or state.latest_record is None:
        return None
    resolution = state.latest
    return ProcedureMeasurementResolutionSummaryV1(
        resolution_id=resolution.resolution_id,
        contract_id=resolution.contract_id,
        sequence=resolution.sequence,
        verdict=resolution.verdict,
        value=resolution.value,
        note=resolution.note,
        observed_at=resolution.observed_at,
        recorded_at=resolution.recorded_at,
        evidence_refs=tuple(item.model_dump(mode="json") for item in resolution.evidence_refs),
        journal_partition_id=state.latest_record.record.partition_id,
        journal_record_digest=state.latest_record.record_digest,
        resolution_digest=procedure_resolution_digest(resolution),
        written_now=written_now,
    )


# ---------------------------------------------------------------------------
# Evidence evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Evidence:
    verdict: Literal["satisfied", "contradicted", "indeterminate"]
    value: CanonicalValue | None
    evidence_refs: tuple[ProcedureProofReferenceV1, ...]
    note: str | None
    claim_attestation_digests: tuple[str, ...] = ()


def _sorted_proofs(
    proofs: list[ProcedureProofReferenceV1],
) -> tuple[ProcedureProofReferenceV1, ...]:
    unique = {canonical_json(item.model_dump(mode="json")): item for item in proofs}
    return tuple(unique[key] for key in sorted(unique))


def _query_budgets(
    measurement: AcceptedQueryProcedureMeasurementV1,
) -> QueryBudgetsV1 | None:
    options = measurement.execution_options
    if not isinstance(options, dict) or not options:
        return None
    unsupported = sorted(set(options) - _QUERY_BUDGET_KEYS)
    if unsupported:
        raise _refuse(
            "measurement_basis_unsupported",
            "The declared execution_options are not served query budgets.",
            unsupported_options=unsupported,
            supported_options=sorted(_QUERY_BUDGET_KEYS),
        )
    try:
        return QueryBudgetsV1.model_validate(options)
    except ValidationError as exc:
        raise _refuse(
            "measurement_basis_unsupported",
            "The declared execution_options do not form a valid query budget.",
            reason=str(exc),
        ) from exc


def _record_query_receipt(
    instance: PlaybillInstance,
    *,
    fenced_receipts: _FencedWriter,
    actor_context: GovernedActorContext,
) -> PlaybillQueryReceiptJournal:
    _journal_backend, stream = _query_receipt_journal(instance)
    fenced_receipts.acquire(stream, _QUERY_RECEIPT_PARTITION)
    return PlaybillQueryReceiptJournal(
        writer=fenced_receipts.writer,
        instance_id=instance.descriptor.instance_id,
        actor_context=actor_context,
        stream_id=_QUERY_RECEIPT_STREAM,
        partition_id=_QUERY_RECEIPT_PARTITION,
    )


def _evaluate_accepted_query(
    instance: PlaybillInstance,
    *,
    measurement: AcceptedQueryProcedureMeasurementV1,
    observation: AcceptedProjectionCoordinate,
    observation_time: datetime,
    receipt_journal: PlaybillQueryReceiptJournal,
) -> _Evidence:
    name = measurement.query.target.name
    try:
        definition = accepted_query_definition(instance, name=name, coordinate=observation)
    except ClaimNotFoundError as exc:
        raise _refuse(
            "measurement_subject_absent",
            f"QueryDefinition:{name} is absent at the observation coordinate.",
            query=measurement.query.target.qualified,
        ) from exc
    if definition.artifact_digest != measurement.query.artifact_digest:
        raise _refuse(
            "measurement_subject_mismatch",
            f"QueryDefinition:{name} at the observation coordinate is not the declared pin.",
            declared_digest=measurement.query.artifact_digest,
            accepted_digest=definition.artifact_digest,
        )
    parameters = measurement.parameters if isinstance(measurement.parameters, dict) else {}
    run = service_run_playbill_query(
        instance,
        name=name,
        evaluation_time=observation_time,
        parameters=cast(Mapping[str, object], parameters),
        at=PlaybillAcceptedCoordinate.from_internal(observation),
        budgets=_query_budgets(measurement),
        receipt_journal=receipt_journal,
    )
    if run.journal_record_digest is None:  # pragma: no cover - journal supplied above
        raise PlaybillFormatError("query receipt was not retained")
    proofs = (ProcedureProofReferenceV1(kind="query_receipt", digest=run.journal_record_digest),)
    receipt = run.receipt
    if receipt.verdict == "refused":
        return _Evidence(
            verdict="indeterminate",
            value={
                "tag": MEASUREMENT_QUERY_EVIDENCE_TAG,
                "status": "refused",
                "refusal_code": receipt.refusal_code,
                "result_digest": receipt.result_digest,
            },
            evidence_refs=proofs,
            note=f"query refused: {receipt.refusal_code}",
        )
    truncation = receipt.truncation
    truncated = bool(
        truncation.clipped_budgets
        or truncation.truncated_includes
        or truncation.candidate_result_count > truncation.returned_result_count
    )
    if truncated:
        return _Evidence(
            verdict="indeterminate",
            value={
                "tag": MEASUREMENT_QUERY_EVIDENCE_TAG,
                "status": "truncated",
                "candidate_result_count": truncation.candidate_result_count,
                "returned_result_count": truncation.returned_result_count,
                "clipped_budgets": list(truncation.clipped_budgets),
                "result_digest": receipt.result_digest,
            },
            evidence_refs=proofs,
            note="query result was truncated; complete evidence is not established",
        )
    items: list[CanonicalValue] = []
    for row in run.result.rows:
        projected: dict[str, CanonicalValue] = {}
        for item in row.fields:
            if item.state == "present":
                projected[item.name] = normalize_canonical(item.value)
        if row.result_subject_identity is not None:
            projected.setdefault("result_subject_identity", row.result_subject_identity)
        items.append(projected)
    value: CanonicalValue = {
        "tag": MEASUREMENT_QUERY_EVIDENCE_TAG,
        "status": "completed",
        "count": truncation.returned_result_count,
        "items": items,
        "result_digest": receipt.result_digest,
    }
    holds = _expectation_holds(measurement.expect, value=value, evidence_count=1)
    return _Evidence(
        verdict="satisfied" if holds else "contradicted",
        value=value,
        evidence_refs=proofs,
        note=(
            None
            if holds
            else f"query returned {truncation.returned_result_count} rows and the declared "
            "expectation does not hold"
        ),
    )


def _accepted_claim_at(
    instance: PlaybillInstance,
    *,
    tree: Mapping[str, bytes],
    measurement: ClaimStatementProcedureMeasurementV1 | ClaimAttestationProcedureMeasurementV1,
) -> ClaimArtifactAny:
    path = measurement.claim_statement.artifact_path
    content = tree.get(path)
    if content is None:
        raise _refuse(
            "measurement_subject_absent",
            f"Claim statement {path} is absent at the observation coordinate.",
            claim_statement=measurement.claim_statement.model_dump(mode="json"),
        )
    try:
        claim = parse_claim(content, path=path)
    except (PlaybillError, ValueError) as exc:
        raise _refuse(
            "measurement_subject_mismatch",
            f"Claim statement {path} at the observation coordinate is not a readable Claim.",
            reason=str(exc),
        ) from exc
    digest = claim_statement_digest(claim.statement).tagged
    if digest != measurement.claim_statement_digest:
        raise _refuse(
            "measurement_subject_mismatch",
            f"Claim statement {path} at the observation coordinate is not the declared statement.",
            declared_digest=measurement.claim_statement_digest,
            accepted_digest=digest,
        )
    return claim


class ClaimVerdictObservationV1(BaseModel):
    """The retained account of one Claim verdict evaluated as measurement evidence.

    The frozen resolution law pins a Claim-statement resolution's value to the
    verdict string, so the observation that produced it -- which accepted
    state, which Claim artifact, at what instant, with which verdict inputs --
    is retained as its own journal record and cited as a ``journal_record``
    proof, exactly as a query receipt is.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-measurement-claim-verdict-observation-v1"] = (
        "playbill-measurement-claim-verdict-observation-v1"
    )
    observation_coordinate: AcceptedCoordinate
    observation_time: datetime
    claim_identity: str
    claim_artifact_path: str
    claim_artifact_digest: str
    claim_statement_digest: str
    verdict_result: ClaimVerdictResultV1


@dataclass(frozen=True)
class RetainedClaimVerdictObservationV1:
    """One retained Claim verdict observation and the record that retains it."""

    stored: StoredProcedureJournalRecordV1
    observation: ClaimVerdictObservationV1


def _evaluate_claim_statement(
    instance: PlaybillInstance,
    *,
    measurement: ClaimStatementProcedureMeasurementV1,
    observation: AcceptedProjectionCoordinate,
    observation_time: datetime,
    tree: Mapping[str, bytes],
    fenced_receipts: _FencedWriter,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> _Evidence:
    claim = _accepted_claim_at(instance, tree=tree, measurement=measurement)
    verdict_query = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=claim.identity.qualified,
        evaluation_time=observation_time,
        at=PlaybillAcceptedCoordinate.from_internal(observation),
    )
    verdict = claim_verdict_v1_compat(verdict_query.verdict)
    account = ClaimVerdictObservationV1(
        observation_coordinate=AcceptedCoordinate.from_internal(observation),
        observation_time=observation_time,
        claim_identity=claim.identity.qualified,
        claim_artifact_path=measurement.claim_statement.artifact_path,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=measurement.claim_statement_digest,
        verdict_result=verdict,
    )
    _receipt_backend, receipt_stream = _query_receipt_journal(instance)
    fenced_receipts.acquire(receipt_stream, _QUERY_RECEIPT_PARTITION)
    retained = fenced_receipts.writer.append(
        stream=receipt_stream,
        partition_id=_QUERY_RECEIPT_PARTITION,
        event_kind=CLAIM_VERDICT_OBSERVATION_EVENT_KIND,
        accepted_coordinate=account.observation_coordinate,
        definition_digest=account.claim_artifact_digest,
        actor_context=actor_context,
        recorded_at=recorded_at,
        payload=account.model_dump(mode="json"),
    )
    proofs = _sorted_proofs(
        [
            ProcedureProofReferenceV1(
                kind="claim_statement",
                digest=measurement.claim_statement_digest,
                subject=measurement.claim_statement,
            ),
            ProcedureProofReferenceV1(
                kind="journal_record",
                digest=retained.record_digest,
                subject=measurement.claim_statement,
            ),
        ]
    )
    holds = verdict.verdict in measurement.acceptable_verdicts
    return _Evidence(
        verdict="satisfied" if holds else "contradicted",
        value=verdict.verdict,
        evidence_refs=proofs,
        note=(
            None
            if holds
            else f"Claim verdict {verdict.verdict!r} is outside the acceptable verdicts "
            f"{list(measurement.acceptable_verdicts)}"
        ),
    )


def _evaluate_claim_attestation(
    instance: PlaybillInstance,
    *,
    measurement: ClaimAttestationProcedureMeasurementV1,
    observation: AcceptedProjectionCoordinate,
    observation_time: datetime,
    tree: Mapping[str, bytes],
) -> _Evidence:
    claim = _accepted_claim_at(instance, tree=tree, measurement=measurement)
    path = measurement.claim_statement.artifact_path
    history = _claim_read_history_index(instance, coordinate=observation)
    law_evidence = history.law_evidence.get(path)
    if law_evidence is None:
        raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
    # The ClaimType must still resolve at the observation coordinate: an
    # attestation over a predicate that no longer parses is not evidence.
    type_path = claim_type_path(claim.statement.predicate)
    if type_path not in tree:
        raise _refuse(
            "measurement_subject_absent",
            f"ClaimType {type_path} is absent at the observation coordinate.",
        )
    parse_claim_type(tree[type_path], path=type_path)
    stances = frozenset(measurement.stances)
    accepted_attestations = current_verified_claim_attestations(
        tree, claim, law_evidence.verified_attestations
    )
    store = instance.claim_attestation_evidence_store()
    attestation_head = store.head()
    # The COMPLETE event history up to the head, not the fold: the fold keeps
    # each principal's latest word only, and the word that stood at an earlier
    # observation instant may be exactly the one it dropped.
    door_events = store.events(at_head=attestation_head)
    artifact_digest = claim_artifact_digest(claim).tagged
    # Eligibility at the OBSERVATION instant is decided per event, before the
    # latest-per-principal reduction: a principal's later word cannot erase
    # the word that stood at the instant being observed.
    latest_door: dict[str, tuple[int, ClaimAttestationEventPayloadV1]] = {}
    for event, payload in door_events:
        statement = payload.attestation.statement
        if (
            statement.claim_identity != claim.identity
            or statement.claim_artifact_digest != artifact_digest
            or statement.claim_statement_digest != measurement.claim_statement_digest
            or statement.attestation_basis != "examined_existing"
            or not payload.current_at_append
            or statement.attested_at > observation_time
            or (statement.valid_until is not None and observation_time >= statement.valid_until)
        ):
            continue
        previous = latest_door.get(payload.attesting_principal_id)
        if previous is None or event.sequence > previous[0]:
            latest_door[payload.attesting_principal_id] = (event.sequence, payload)
    items: list[dict[str, CanonicalValue]] = []
    digests: set[str] = set()
    principals: set[str] = set()
    for sequence, payload in latest_door.values():
        statement = payload.attestation.statement
        if statement.stance not in stances:
            # The principal's standing word at the observation carries another
            # stance: it stands, it is simply not counted toward this one.
            continue
        digests.add(payload.envelope_digest)
        principals.add(payload.attesting_principal_id)
        items.append(
            {
                "principal": payload.attesting_principal_id,
                "stance": statement.stance,
                "attestation_digest": payload.envelope_digest,
                "source": "evidence_door",
                "event_sequence": sequence,
            }
        )
    for item in accepted_attestations:
        statement_v1 = item.statement
        if (
            not item.current
            or item.attestation_grade != "verified_principal"
            or statement_v1.provider_or_principal.kind != "Principal"
            or statement_v1.claim_statement_digest != measurement.claim_statement_digest
            or statement_v1.stance not in stances
            or statement_v1.provider_or_principal.name in latest_door
            or statement_v1.observed_at > observation_time
            or (
                statement_v1.valid_until is not None
                and observation_time >= statement_v1.valid_until
            )
        ):
            continue
        digests.add(item.attestation_digest)
        principals.add(statement_v1.provider_or_principal.name)
        items.append(
            {
                "principal": statement_v1.provider_or_principal.name,
                "stance": statement_v1.stance,
                "attestation_digest": item.attestation_digest,
                "source": "acceptance",
            }
        )
    ordered_items = sorted(
        items,
        key=lambda entry: (str(entry["principal"]).encode("utf-8"), str(entry["stance"])),
    )
    ordered_digests = tuple(sorted(digests))
    value: CanonicalValue = {
        "tag": MEASUREMENT_ATTESTATION_EVIDENCE_TAG,
        "count": len(principals),
        "items": cast(list[CanonicalValue], ordered_items),
        "attestation_head": attestation_head,
        "observation_coordinate": AcceptedCoordinate.from_internal(observation).model_dump(
            mode="json"
        ),
        "observation_time": format_datetime(observation_time),
        "claim_artifact_digest": artifact_digest,
    }
    proofs = _sorted_proofs(
        [
            ProcedureProofReferenceV1(
                kind="claim_attestation",
                digest=digest,
                subject=measurement.claim_statement,
            )
            for digest in ordered_digests
        ]
    )
    if not proofs:
        # The frozen law demands proof for any verdict, satisfied included: an
        # absence of attestations is an honest indeterminate, never a proof-less
        # satisfaction (a `max_count` of 0 met by nothing) or a fabricated proof.
        return _Evidence(
            verdict="indeterminate",
            value=value,
            evidence_refs=(),
            note="no verified attestation with a declared stance stands at the observation",
        )
    holds = _expectation_holds(measurement.expect, value=value, evidence_count=len(proofs))
    if holds:
        return _Evidence(
            verdict="satisfied",
            value=value,
            evidence_refs=proofs,
            note=None,
            claim_attestation_digests=ordered_digests,
        )
    return _Evidence(
        verdict="contradicted",
        value=value,
        evidence_refs=proofs,
        note=(
            f"{len(principals)} independent principal(s) attested with a declared stance and "
            "the declared expectation does not hold"
        ),
        claim_attestation_digests=ordered_digests,
    )


def _evaluate_evidence(
    instance: PlaybillInstance,
    *,
    declaration: ProcedureMeasurementDeclarationV1,
    observation: AcceptedProjectionCoordinate,
    observation_time: datetime,
    tree: Mapping[str, bytes],
    receipts: PlaybillQueryReceiptJournal | None,
    fenced_receipts: _FencedWriter,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> tuple[_Evidence, PlaybillQueryReceiptJournal | None]:
    measurement = declaration.measurement
    if isinstance(measurement, AcceptedQueryProcedureMeasurementV1):
        if receipts is None:
            receipts = _record_query_receipt(
                instance,
                fenced_receipts=fenced_receipts,
                actor_context=actor_context,
            )
        return (
            _evaluate_accepted_query(
                instance,
                measurement=measurement,
                observation=observation,
                observation_time=observation_time,
                receipt_journal=receipts,
            ),
            receipts,
        )
    if isinstance(measurement, ClaimStatementProcedureMeasurementV1):
        return (
            _evaluate_claim_statement(
                instance,
                measurement=measurement,
                observation=observation,
                observation_time=observation_time,
                tree=tree,
                fenced_receipts=fenced_receipts,
                actor_context=actor_context,
                recorded_at=recorded_at,
            ),
            receipts,
        )
    return (
        _evaluate_claim_attestation(
            instance,
            measurement=measurement,
            observation=observation,
            observation_time=observation_time,
            tree=tree,
        ),
        receipts,
    )


# ---------------------------------------------------------------------------
# Reading partition index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IndexedReading:
    stored: StoredProcedureJournalRecordV1
    reading: ProcedureReadingV1


@dataclass
class _ReadingPartitionIndex:
    """Parsed readings of one partition, extended incrementally by journal prefix.

    Keyed per process on (instance root, partition). A later request re-reads
    the verified record list (the backend's own authority) and parses only the
    records beyond the cached prefix, provided every cached record digest still
    matches; any divergence rebuilds from the authoritative bytes.
    """

    digests: tuple[str, ...]
    entries: tuple[_IndexedReading, ...]
    by_key: dict[ReadingReplayKey, _IndexedReading]
    head: JournalPartitionHeadV1


_reading_index_memo: OrderedDict[tuple[str, str], _ReadingPartitionIndex] = OrderedDict()


def _parse_reading(
    instance: PlaybillInstance,
    stored: StoredProcedureJournalRecordV1,
) -> _IndexedReading:
    payload = parse_journal_payload(
        instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
    )
    try:
        reading = ProcedureReadingV1.model_validate(payload)
    except ValidationError as exc:
        raise PlaybillFormatError("retained Procedure reading does not reproduce") from exc
    return _IndexedReading(stored=stored, reading=reading)


def reading_partition_index(
    instance: PlaybillInstance,
    *,
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    partition_id: str,
) -> _ReadingPartitionIndex:
    # The head is read first and the record list trimmed to it, so the index
    # describes exactly the partition prefix that head commits: an append
    # keyed on this head is a compare-and-set against what was indexed.
    head = journal.read_head(stream, partition_id)
    records = journal.all_records(stream, partition_id)[: head.sequence]
    digests = tuple(stored.record_digest for stored in records)
    key = (str(instance.root), partition_id)
    cached = _reading_index_memo.get(key)
    start = 0
    entries: list[_IndexedReading] = []
    if cached is not None and digests[: len(cached.digests)] == cached.digests:
        start = len(cached.digests)
        entries.extend(cached.entries)
    for stored in records[start:]:
        if stored.record.event_kind != "procedure_reading":
            continue
        entries.append(_parse_reading(instance, stored))
    by_key: dict[ReadingReplayKey, _IndexedReading] = {}
    for entry in entries:
        replay = reading_replay_key(entry.reading)
        if replay is not None:
            by_key.setdefault(replay, entry)
    index = _ReadingPartitionIndex(
        digests=digests, entries=tuple(entries), by_key=by_key, head=head
    )
    _reading_index_memo[key] = index
    _reading_index_memo.move_to_end(key)
    while len(_reading_index_memo) > _READING_INDEX_MEMO_CAPACITY:
        _reading_index_memo.popitem(last=False)
    return index


def _verify_retained(instance: PlaybillInstance, entry: _IndexedReading) -> _IndexedReading:
    """Re-read a reading's body through CAS before serving it from the warm index.

    The journal frame authenticates the record; it does not prove the body
    those bytes address is still present and intact. Every reading this
    service returns or replays against is re-verified against its content
    address, so a warm process answers exactly as a cold one would.
    """

    try:
        instance.body_store().read(entry.stored.record.payload_digest, access=_ACCESS)
    except PlaybillCasError:
        _reading_index_memo.pop((str(instance.root), entry.stored.record.partition_id), None)
        raise
    return entry


def _reading_summary(entry: _IndexedReading) -> ProcedureReadingSummaryV1:
    reading = entry.reading
    return ProcedureReadingSummaryV1(
        reading_id=reading.reading_id,
        reading_digest=procedure_reading_digest(reading),
        journal_partition_id=entry.stored.record.partition_id,
        journal_record_digest=entry.stored.record_digest,
        subject_grain=reading.subject_grain,
        subject=reading.subject,
        accepted_coordinate=reading.accepted_coordinate,
        definition_digest=reading.definition_digest,
        node_id=reading.node_id,
        from_node_id=reading.from_node_id,
        arm_label=reading.arm_label,
        grade=reading.grade,
        measurement_name=reading.measurement_name,
        contract_id=reading.contract_id,
        resolution_id=reading.resolution_id,
        verdict=reading.verdict,
        value=reading.value,
        run_id=reading.run_id,
        run_receipt_digest=reading.run_receipt_digest,
        episode_ref=reading.episode_ref,
        evidence_refs=tuple(item.model_dump(mode="json") for item in reading.evidence_refs),
        claim_attestation_digests=reading.claim_attestation_digests,
        observed_at=reading.observed_at,
        recorded_at=reading.recorded_at,
        actor_id=reading.actor_context.actor_id,
        idempotency_key=reading.idempotency_key,
    )


# ---------------------------------------------------------------------------
# Grain binding
# ---------------------------------------------------------------------------


def _grain_occurred(
    activation: ResolutionContractActivationV1,
    grain: ProcedureRunGrainRecordV1,
) -> tuple[bool, str | None]:
    if activation.subject_grain == "procedure_unit":
        if grain.state.status == "succeeded":
            return True, None
        return False, f"run finalized with status {grain.state.status!r}, not succeeded"
    node_id = cast(str, activation.node_id)
    node_verdict = grain.node_verdicts.get(node_id)
    if activation.subject_grain == "node":
        if node_verdict == "succeeded":
            return True, None
        if node_verdict is None:
            return False, f"node {node_id!r} never fired in this run"
        return False, f"node {node_id!r} fired with verdict {node_verdict!r}"
    from_node_id = cast(str, activation.from_node_id)
    arm_label = cast(str, activation.arm_label)
    selected = grain.selected_arms.get(from_node_id, ())
    if arm_label not in selected:
        if not selected:
            return False, f"guard {from_node_id!r} never evaluated in this run"
        return False, f"guard {from_node_id!r} selected {sorted(set(selected))}, not {arm_label!r}"
    if node_verdict != "succeeded":
        return False, f"arm target {node_id!r} did not fire successfully"
    return True, None


def measurement_reading_idempotency_key(
    activation: ResolutionContractActivationV1,
    grain: ProcedureRunGrainRecordV1,
) -> str:
    """One credit per (activation, grain, real occurrence): attempts collapse."""

    occurrence = grain.occurrence_id if grain.occurrence_id is not None else grain.state.run_id
    return (
        f"measurement:{activation.activation_id}:"
        f"{activation.subject.address.selector.scheme}:"
        f"{activation.subject.address.selector.value}:{occurrence}"
    )


def _semantic_reading_payload(reading: ProcedureReadingV1) -> CanonicalValue:
    """What a retry must agree on: the reading's meaning, not its request.

    ``recorded_at`` and the per-request actor attribution (timestamp,
    operation id, request id) are re-minted by every authenticated call; the
    principal is not. A Line occurrence is credited once, so which attempt
    carried it (``run_id``, ``run_receipt_digest``) is attempt identity, not
    reading identity, when ``episode_ref`` names the occurrence.
    """

    payload = reading.model_dump(mode="json")
    payload.pop("recorded_at", None)
    actor = payload.get("actor_context")
    if isinstance(actor, dict):
        for field in _VOLATILE_ACTOR_FIELDS:
            actor.pop(field, None)
    if payload.get("episode_ref") is not None:
        payload.pop("run_id", None)
        payload.pop("run_receipt_digest", None)
    return normalize_canonical(payload)


# ---------------------------------------------------------------------------
# Served operations
# ---------------------------------------------------------------------------


def _resolve_observation(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _selected_activations(
    basis: MeasurementActivationBasisV1,
    names: tuple[str, ...],
) -> tuple[ResolutionContractActivationV1, ...]:
    if not names:
        return basis.activations
    declared = {item.measurement_name: item for item in basis.activations}
    missing = [name for name in names if name not in declared]
    if missing:
        raise _refuse(
            "measurement_not_declared",
            f"Measurements {missing} are not declared on this accepted Procedure revision.",
            declared=sorted(declared),
            missing=missing,
        )
    return tuple(declared[name] for name in names)


def service_measure_playbill_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    request: PlaybillProcedureMeasureRequestV1,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> PlaybillProcedureMeasureResultV1:
    """Evaluate due measurements, persist resolutions, and credit one real run.

    Idempotent and resumable: a standing resolution is returned rather than
    re-derived; a reading whose key already stands is replayed; a crash between
    the resolution append and the reading append resumes at the reading.
    """

    instance.require_writable()
    recorded_at = ensure_utc(recorded_at)
    observation_time = ensure_utc(
        request.evaluation_time if request.evaluation_time is not None else recorded_at
    )
    if observation_time > recorded_at:
        raise _refuse(
            "measurement_basis_unsupported",
            "The observation instant cannot follow the recording instant.",
            observation_time=observation_time.isoformat(),
            recorded_at=recorded_at.isoformat(),
        )
    observation = _resolve_observation(instance, request.at)
    accepted = _accepted_procedure(instance, name=name, coordinate=observation)
    basis = measurement_activation_basis(instance, accepted=accepted, observation=observation)
    activations = _selected_activations(basis, request.measurement_names)
    public_observation = AcceptedCoordinate.from_internal(observation)
    tree = instance.tree_at(observation.git_oid)

    grain: ProcedureRunGrainRecordV1 | None = None
    if request.run_id is not None:
        grain = load_playbill_procedure_run_grain(instance, run_id=request.run_id)
        if grain.state.procedure_artifact_digest != accepted.artifact_digest:
            raise _refuse(
                "measurement_run_mismatch",
                "The named run executed another Procedure revision.",
                run_procedure_artifact_digest=grain.state.procedure_artifact_digest,
                measured_procedure_artifact_digest=accepted.artifact_digest,
            )

    journal, stream = _journal(instance)
    reading_partition = procedure_reading_partition_id(accepted)
    fenced = _FencedWriter(instance, journal)
    receipt_backend, _receipt_stream = _query_receipt_journal(instance)
    fenced_receipts = _FencedWriter(instance, receipt_backend)
    receipts: PlaybillQueryReceiptJournal | None = None
    rows: list[ProcedureMeasurementRowV1] = []
    try:
        # Recover append-window leases exactly as the run and settlement
        # writers do, over the COMPLETE journal: recovery releases every active
        # lease a scan does not reference, so a partial scan would release
        # another partition's crashed lease on the strength of not looking.
        recovery_records = tuple(
            stored
            for partition_id in journal.partition_ids(stream)
            for stored in journal.all_records(stream, partition_id)
        )
        ProcedureMaterialReservationStore(
            instance.body_store().reservation_root
        ).recover_run_material(recovery_records, bodies=instance.body_store())
        for activation in activations:
            eligibility = _eligibility(
                activation,
                observation=public_observation,
                observation_time=observation_time,
            )
            state = _contract_state(instance, journal, stream, activation)
            written_now = False
            if state.latest is None and eligibility.window == "open":
                evidence, receipts = _evaluate_evidence(
                    instance,
                    declaration=activation.declaration,
                    observation=observation,
                    observation_time=observation_time,
                    tree=tree,
                    receipts=receipts,
                    fenced_receipts=fenced_receipts,
                    actor_context=actor_context,
                    recorded_at=recorded_at,
                )
                resolution = build_procedure_resolution(
                    activation,
                    sequence=len(state.book.resolutions.get(activation.contract_id, ())) + 1,
                    verdict=evidence.verdict,
                    value=evidence.value,
                    evidence_refs=evidence.evidence_refs,
                    observed_at=observation_time,
                    recorded_at=recorded_at,
                    actor_context=actor_context,
                    note=evidence.note,
                )
                partition_id = resolution_contract_partition_id(activation)
                fenced.acquire(stream, partition_id)
                try:
                    if not any(
                        stored.record.event_kind == "resolution_activation"
                        for stored in state.records
                    ):
                        fenced.writer.append(
                            stream=stream,
                            partition_id=partition_id,
                            event_kind="resolution_activation",
                            accepted_coordinate=activation.subject.accepted_coordinate,
                            procedure_artifact_digest=activation.procedure_artifact_digest,
                            definition_digest=activation.definition_digest,
                            actor_context=actor_context,
                            recorded_at=activation.activated_at,
                            payload=activation.model_dump(mode="json"),
                        )
                    append_procedure_resolution(
                        fenced.writer,
                        activation=activation,
                        resolution=resolution,
                        stream=stream,
                    )
                except PlaybillJournalConflictError as exc:
                    raise _refuse(
                        "measurement_resolution_conflict",
                        "Another writer moved this measurement's journal partition; retry.",
                        contract_id=activation.contract_id,
                    ) from exc
                except PlaybillExecutionError as exc:
                    # The kernel refused the sequence or the closed contract:
                    # a concurrent evaluation landed first. Re-read and return
                    # its answer rather than inventing a second one.
                    state = _contract_state(instance, journal, stream, activation)
                    if state.latest is None:
                        raise _refuse(
                            "measurement_resolution_conflict",
                            f"Resolution append was refused: {exc}",
                            contract_id=activation.contract_id,
                        ) from exc
                else:
                    written_now = True
                    state = _contract_state(instance, journal, stream, activation)
            status: ProcedureMeasurementStatusV1 = (
                "resolved"
                if state.latest is not None
                else "pending"
                if eligibility.window == "before_check"
                else "expired"
            )
            detail: str | None = None
            reading_status: ProcedureReadingStatusV1 = "not_requested"
            reading_summary: ProcedureReadingSummaryV1 | None = None
            if grain is not None:
                if state.latest is None:
                    reading_status = "no_resolution"
                    detail = (
                        "the measurement window has not opened"
                        if status == "pending"
                        else "the measurement window closed with no standing resolution"
                    )
                elif not grain.finalized:
                    reading_status = "run_not_final"
                    detail = "the run has not finalized; retry once it has"
                else:
                    occurred, reason = _grain_occurred(activation, grain)
                    if not occurred:
                        reading_status = "grain_not_occurred"
                        detail = reason
                    else:
                        reading_status, reading_summary = _credit_reading(
                            instance,
                            accepted=accepted,
                            activation=activation,
                            resolution=state.latest,
                            book=state.book,
                            grain=grain,
                            actor_context=actor_context,
                            recorded_at=recorded_at,
                            fenced=fenced,
                            journal=journal,
                            stream=stream,
                            partition_id=reading_partition,
                        )
            rows.append(
                ProcedureMeasurementRowV1(
                    measurement_name=activation.measurement_name,
                    measurement_kind=activation.declaration.measurement.kind,
                    contract_id=activation.contract_id,
                    activation_id=activation.activation_id,
                    subject_grain=activation.subject_grain,
                    subject=activation.subject.address,
                    status=status,
                    eligibility=eligibility,
                    resolution=_resolution_summary(state, written_now=written_now),
                    reading_status=reading_status,
                    reading=reading_summary,
                    detail=detail,
                )
            )
    finally:
        fenced_receipts.release()
        fenced.release()
    return PlaybillProcedureMeasureResultV1(
        procedure_identity=accepted.procedure.identity,
        procedure_artifact_digest=accepted.artifact_digest,
        activation_coordinate=basis.coordinate,
        observation_coordinate=public_observation,
        observation_time=observation_time,
        run_id=None if grain is None else grain.state.run_id,
        run_admission_coordinate=(
            None
            if grain is None
            else AcceptedCoordinate.model_validate(
                grain.state.bound_coordinate.model_dump(mode="json")
            )
        ),
        rows=tuple(rows),
    )


def _credit_reading(
    instance: PlaybillInstance,
    *,
    accepted: AcceptedProcedureV1,
    activation: ResolutionContractActivationV1,
    resolution: ProcedureResolutionV1,
    book: ProcedureResolutionBook,
    grain: ProcedureRunGrainRecordV1,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
    fenced: _FencedWriter,
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    partition_id: str,
) -> tuple[ProcedureReadingStatusV1, ProcedureReadingSummaryV1]:
    """Credit one grain exactly once.

    Lookup and append are one compare-and-set: the partition is indexed at a
    head, the keyed reading is looked up in that index, and the append is
    keyed on that same head, so a competing writer that lands the same
    reading in between turns this append into a conflict and this request
    into a replay of what landed -- never a second credit.
    """

    run_id = cast(str, grain.state.run_id)
    if grain.state.receipt_digest is None:
        raise PlaybillFormatError("a finalized run carries no receipt digest")
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=activation.subject.accepted_coordinate,
        subject_grain=activation.subject_grain,
        grade="contract",
        verdict=resolution.verdict,
        observed_at=resolution.observed_at,
        recorded_at=recorded_at,
        actor_context=actor_context,
        node_id=activation.node_id,
        from_node_id=activation.from_node_id,
        arm_label=activation.arm_label,
        activation=activation,
        resolution_id=resolution.resolution_id,
        value=resolution.value,
        run_id=run_id,
        run_receipt_digest=grain.state.receipt_digest,
        episode_ref=grain.occurrence_id,
        situation_shape=(
            None
            if activation.declaration.situation_shape is None
            else activation.declaration.situation_shape.model_dump(mode="json")
        ),
        evidence_refs=resolution.evidence_refs,
        claim_attestation_digests=tuple(
            sorted(
                proof.digest
                for proof in resolution.evidence_refs
                if proof.kind == "claim_attestation"
            )
        ),
        idempotency_key=measurement_reading_idempotency_key(activation, grain),
    )
    replay = reading_replay_key(reading)
    fenced.acquire(stream, partition_id)
    for _attempt in range(_APPEND_ATTEMPTS):
        index = reading_partition_index(
            instance, journal=journal, stream=stream, partition_id=partition_id
        )
        existing = None if replay is None else index.by_key.get(replay)
        if existing is not None:
            _verify_retained(instance, existing)
            if _semantic_reading_payload(existing.reading) != _semantic_reading_payload(reading):
                raise _refuse(
                    "measurement_reading_conflict",
                    "A reading already stands for this key with a different payload.",
                    reading_id=existing.reading.reading_id,
                    existing_resolution_id=existing.reading.resolution_id,
                    requested_resolution_id=reading.resolution_id,
                )
            return "replayed", _reading_summary(existing)
        try:
            stored = append_procedure_reading(
                fenced.writer,
                reading=reading,
                accepted=accepted,
                accepted_coordinate=activation.subject.accepted_coordinate,
                stream=stream,
                bodies=instance.body_store(),
                activations=(activation,),
                resolution_book=book,
                replay_index={
                    key: (entry.stored, entry.reading) for key, entry in index.by_key.items()
                },
                expected_head=index.head,
            )
        except PlaybillJournalConflictError:
            # Another writer moved the partition after it was indexed. Index
            # it again: if the competing record is this very reading, the
            # next pass replays it; otherwise the append is retried on the
            # new head.
            continue
        refreshed = reading_partition_index(
            instance, journal=journal, stream=stream, partition_id=partition_id
        )
        entry = next(
            (
                item
                for item in refreshed.entries
                if item.stored.record_digest == stored.record_digest
            ),
            None,
        )
        if entry is None:  # pragma: no cover - the append just landed
            raise PlaybillFormatError("appended reading is absent from its partition")
        return "recorded", _reading_summary(entry)
    raise _refuse(
        "measurement_resolution_conflict",
        "The reading partition kept moving under this request; retry.",
        reading_id=reading.reading_id,
    )


def _cursor(
    selection: str,
    *,
    observation_time: datetime,
    at: AcceptedCoordinate,
    sequence: int,
) -> str:
    """A page handle that carries the selection it continues.

    The observation instant and coordinate the first page was answered at
    travel inside the cursor, so a client whose clock has moved on (every SDK
    call stamps a fresh instant) continues the SAME selection rather than a
    drifted one, and a cursor never silently re-selects.
    """

    return base64.urlsafe_b64encode(
        canonical_json(
            {
                "selection": selection,
                "observation_time": format_datetime(observation_time),
                "at": at.model_dump(mode="json"),
                "after": sequence,
            }
        ).encode()
    ).decode()


@dataclass(frozen=True)
class _Continuation:
    observation_time: datetime
    at: AcceptedCoordinate
    after: int


def _parse_cursor(cursor: str, *, selection: str) -> _Continuation:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
        if not isinstance(payload, dict) or payload.get("selection") != selection:
            raise ValueError("cursor selection differs")
        after = payload.get("after")
        observation_time = parse_datetime(payload.get("observation_time"))
        if not isinstance(after, int) or observation_time is None:
            raise ValueError("cursor continuation is malformed")
        at = AcceptedCoordinate.model_validate(payload.get("at"))
    except (ValueError, TypeError, UnicodeError, ValidationError) as exc:
        raise PlaybillFormatError("reading cursor does not match this selection") from exc
    return _Continuation(observation_time=ensure_utc(observation_time), at=at, after=after)


def _selection_digest(request: PlaybillProcedureReadingsRequestV1) -> str:
    # The instant and coordinate are the continuation's, carried by the
    # cursor; the rest of the request must not change between pages.
    return canonical_json(
        request.model_dump(mode="json", exclude={"cursor", "limit", "evaluation_time", "at"})
    )


def service_list_playbill_procedure_readings(
    instance: PlaybillInstance,
    *,
    name: str,
    request: PlaybillProcedureReadingsRequestV1,
    evaluation_time: datetime,
) -> PlaybillProcedureReadingsResultV1:
    """Bounded, read-only inspection of contract standing and retained readings."""

    selection = _selection_digest(request)
    continuation = (
        None if not request.cursor else _parse_cursor(request.cursor, selection=selection)
    )
    if continuation is not None:
        observation_time = continuation.observation_time
        observation = _resolve_observation(instance, continuation.at)
    else:
        observation_time = ensure_utc(
            request.evaluation_time if request.evaluation_time is not None else evaluation_time
        )
        observation = _resolve_observation(instance, request.at)
    accepted = _accepted_procedure(instance, name=name, coordinate=observation)
    basis = measurement_activation_basis(instance, accepted=accepted, observation=observation)
    activations = _selected_activations(basis, request.measurement_names)
    public_observation = AcceptedCoordinate.from_internal(observation)
    journal, stream = _journal(instance)
    partition_id = procedure_reading_partition_id(accepted)
    index = reading_partition_index(
        instance, journal=journal, stream=stream, partition_id=partition_id
    )
    wanted_contracts = {item.contract_id for item in activations}
    counts: dict[str, int] = {}
    matching: list[_IndexedReading] = []
    for entry in index.entries:
        reading = entry.reading
        if reading.contract_id is not None:
            counts[reading.contract_id] = counts.get(reading.contract_id, 0) + 1
        if request.measurement_names and reading.contract_id not in wanted_contracts:
            continue
        if reading.definition_digest != accepted.procedure.definition_digest:
            continue
        if request.run_id is not None and reading.run_id != request.run_id:
            continue
        if request.subject_grain is not None and reading.subject_grain != request.subject_grain:
            continue
        matching.append(entry)
    after = 0 if continuation is None else continuation.after
    page = [entry for entry in matching if entry.stored.record.sequence > after]
    truncated = len(page) > request.limit
    page = page[: request.limit]
    # Only what this page serves is re-read through CAS: bounded by the page,
    # and enough that a warm index never vouches for a body it cannot show.
    for entry in page:
        _verify_retained(instance, entry)
    cursor = (
        _cursor(
            selection,
            observation_time=observation_time,
            at=public_observation,
            sequence=page[-1].stored.record.sequence,
        )
        if truncated and page
        else None
    )
    contracts: list[ProcedureMeasurementContractStatusV1] = []
    for activation in activations:
        state = _contract_state(instance, journal, stream, activation)
        eligibility = _eligibility(
            activation,
            observation=public_observation,
            observation_time=observation_time,
        )
        contracts.append(
            ProcedureMeasurementContractStatusV1(
                measurement_name=activation.measurement_name,
                measurement_kind=activation.declaration.measurement.kind,
                contract_id=activation.contract_id,
                activation_id=activation.activation_id,
                subject_grain=activation.subject_grain,
                subject=activation.subject.address,
                status=(
                    "resolved"
                    if state.latest is not None
                    else "pending"
                    if eligibility.window == "before_check"
                    else "expired"
                ),
                eligibility=eligibility,
                resolution=_resolution_summary(state, written_now=False),
                reading_count=counts.get(activation.contract_id, 0),
            )
        )
    return PlaybillProcedureReadingsResultV1(
        procedure_identity=accepted.procedure.identity,
        procedure_artifact_digest=accepted.artifact_digest,
        activation_coordinate=basis.coordinate,
        observation_coordinate=public_observation,
        observation_time=observation_time,
        contracts=tuple(contracts),
        readings=tuple(_reading_summary(entry) for entry in page),
        truncated=truncated,
        cursor=cursor,
    )


# ---------------------------------------------------------------------------
# Evidence closure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetainedQueryEvidenceV1:
    """One retained query receipt and the record that retains it."""

    stored: StoredProcedureJournalRecordV1
    receipt: QueryExecutionReceiptV1


def load_retained_query_receipt(
    instance: PlaybillInstance,
    *,
    record_digest: str,
) -> RetainedQueryEvidenceV1:
    """Resolve a ``query_receipt`` proof reference to its retained receipt bytes.

    A proof reference retains nothing by itself; the receipt lives in the
    query-receipt journal this producer appends to, and its CAS payload is the
    material the reference names. Absence or a payload that does not parse is
    surfaced as missing evidence, never papered over.
    """

    journal, stream = _query_receipt_journal(instance)
    for stored in journal.all_records(stream, _QUERY_RECEIPT_PARTITION):
        if stored.record_digest != record_digest:
            continue
        if stored.record.event_kind != QUERY_RECEIPT_EVENT_KIND:
            break
        payload = parse_journal_payload(
            instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
        )
        try:
            receipt = QueryExecutionReceiptV1.model_validate(payload)
        except ValidationError as exc:
            raise PlaybillFormatError("retained query receipt does not reproduce") from exc
        return RetainedQueryEvidenceV1(stored=stored, receipt=receipt)
    raise PlaybillFormatError("query receipt evidence is not retained under that digest")


def load_retained_claim_verdict_observation(
    instance: PlaybillInstance,
    *,
    record_digest: str,
) -> RetainedClaimVerdictObservationV1:
    """Resolve a Claim-statement resolution's ``journal_record`` proof to its account."""

    journal, stream = _query_receipt_journal(instance)
    for stored in journal.all_records(stream, _QUERY_RECEIPT_PARTITION):
        if stored.record_digest != record_digest:
            continue
        if stored.record.event_kind != CLAIM_VERDICT_OBSERVATION_EVENT_KIND:
            break
        payload = parse_journal_payload(
            instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
        )
        try:
            account = ClaimVerdictObservationV1.model_validate(payload)
        except ValidationError as exc:
            raise PlaybillFormatError(
                "retained Claim verdict observation does not reproduce"
            ) from exc
        return RetainedClaimVerdictObservationV1(stored=stored, observation=account)
    raise PlaybillFormatError("Claim verdict observation is not retained under that digest")


def reconstruct_query_evidence(
    instance: PlaybillInstance,
    *,
    evidence: RetainedQueryEvidenceV1,
    parameters: Mapping[str, object] | None = None,
) -> bool:
    """Re-run the retained receipt's exact coordinates and compare result digests.

    The receipt names the definition digest, coordinate, instant, budgets, and
    parameter digest; the declaration supplies the parameters themselves. A
    reproduced result digest proves the retained material still says what the
    resolution cited.
    """

    receipt = evidence.receipt
    definition_name = receipt.definition_path.removeprefix("query-definitions/").removesuffix(
        ".json"
    )
    run = service_run_playbill_query(
        instance,
        name=definition_name,
        evaluation_time=receipt.evaluation_time,
        at=PlaybillAcceptedCoordinate.from_internal(receipt.coordinate),
        budgets=receipt.budgets,
        parameters=parameters,
    )
    return (
        run.definition_digest == receipt.definition_digest
        and run.receipt.parameter_digest == receipt.parameter_digest
        and run.receipt.result_digest == receipt.result_digest
    )


__all__ = [
    "MEASUREMENT_ATTESTATION_EVIDENCE_TAG",
    "MEASUREMENT_QUERY_EVIDENCE_TAG",
    "MeasurementActivationBasisV1",
    "ProcedureMeasurementRefused",
    "RetainedQueryEvidenceV1",
    "load_retained_query_receipt",
    "measurement_activation_basis",
    "measurement_reading_idempotency_key",
    "reading_partition_index",
    "reconstruct_query_evidence",
    "service_list_playbill_procedure_readings",
    "service_measure_playbill_procedure",
]
