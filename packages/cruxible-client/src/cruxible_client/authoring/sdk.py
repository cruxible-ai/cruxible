"""Synchronous, agent-oriented authoring facade over the Cruxible wire ISA."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from pydantic import SecretStr, TypeAdapter

import cruxible_client.compatibility as client_compatibility
from cruxible_client import contracts as api
from cruxible_client.authoring.approval import ReviewedProposal, approve_reviewed, review_proposal
from cruxible_client.authoring.attestations import (
    ClaimAttestationSigner,
    append_prepared_claim_attestation,
    prepare_claim_attestation,
)
from cruxible_client.authoring.blocks import (
    assert_independent_projection_evidence,
    repin_projection_block,
    sync_projection_blocks,
)
from cruxible_client.authoring.compact_query import QueryResult, filters_from_mappings
from cruxible_client.authoring.context import (
    ContextResolutionError,
    resolve_context,
)
from cruxible_client.authoring.procedures import ProviderBinding, procedure_record_constructor
from cruxible_client.authoring.procedures import Sequence as ProcedureSequence
from cruxible_client.authoring.sdk_types import (
    AccessProfile,
    CallSite,
    CapabilityNotServed,
    CaptureRef,
    CaptureView,
    Cardinality,
    ClaimObjectKind,
    ClaimRef,
    ClaimRole,
    ClaimRoleNotPermittedError,
    ClaimTypeRef,
    Diagnostic,
    Disposition,
    Duration,
    EffectivePeriod,
    ExactContent,
    ExactContentTypeError,
    LiteralValue,
    LiteralValueTypeError,
    PendingClaimTypeRef,
    PendingSubjectRef,
    ProcedureRef,
    ProcedureSlotRef,
    QueryRef,
    ReferenceKindError,
    ReferentSensitivity,
    RefKind,
    SourceMapEntry,
    SourceRef,
    SourceSelectionError,
    SubjectRef,
    TypedRef,
)
from cruxible_client.authoring.selectors import (
    EvidenceSelection,
    FileSelector,
    WorkspaceSources,
)
from cruxible_client.authoring.signing import ApprovalSigner
from cruxible_client.authoring.source import ProcedureBlueprint
from cruxible_client.authoring.source_map import (
    DiagnosticSourceMap,
    capture_keyword_sites,
    entries_for_keywords,
)
from cruxible_client.authoring.workspace import (
    observe_next_workspace,
    observe_next_workspace_with_coverage,
    refresh_workspace_floor,
    workspace_floor_freshness,
)
from cruxible_client.authoring.write_evidence import observe_changes
from cruxible_client.contracts.acquisition_policies import (
    SourceAcquisitionPolicy,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.authoring.inputs import (
    ProcedureInput,
    ProcedureMandateInput,
    QueryDefinitionInput,
    lower_authoring_input,
)
from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AttestationAuthoringPayload,
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExactContentObject,
    AuthoringExistingClaimDisposition,
    AuthoringIntentView,
    AuthoringProgramOperation,
    AuthoringProgramStamp,
    AuthoringReferenceExpectation,
    CaptureContractAuthoringPayload,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    ClaimRetirementMember,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionDependent,
    ClaimTypeSuccessionMember,
    ExistingCaptureCitationSource,
    LineAuthoringPayload,
    ProcedureAuthoringPayload,
    ProcedureAuthoringPayloadV1,
    ProcedureMandateAuthoringPayload,
    QueryDefinitionAuthoringPayload,
    ResolutionContractAuthoringPayload,
    SelfSourceBody,
    SourceAcquisitionPolicyAuthoringPayload,
    SubjectAuthoringPayload,
    TriggerAuthoringPayload,
    authoring_member_identity,
    authoring_program_digest,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    normalize_canonical,
)
from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.captures import (
    CaptureContract,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationAppendResult,
    ClaimStance,
    PreparedClaimAttestationRequest,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole as ClaimRoleValue
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
    EvidenceRequirement,
    RevisionEvidence,
    effective_evidence_requirement,
    effective_revision_evidence,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimRetireDependent,
    ClaimRetirementReason,
    ClaimUnsupportedFormatError,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.compact_query import (
    QueryClaimStatus,
    QueryFilter,
    QueryFollow,
    QueryFollowDirection,
    QueryReceiptDetail,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStamp,
    ProjectionCurrencyPolicy,
)
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.get_reads import (
    GET_BATCH_MAX_REFS,
    ByteRange,
    ExactContentRef,
    GetBatchRequest,
    GetDetail,
    GetProcedureCard,
    GetProcedureTrackRecord,
    GetRequest,
    GetResult,
)
from cruxible_client.contracts.line_dispatch import (
    LineTriggerCheckRequest,
    LineTriggerCheckResult,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidence,
    PredictRequest,
    ResolutionContractInput,
    TerminalSettlementEvidence,
)
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.results import ProcedureTerminalEgress
from cruxible_client.contracts.procedures.windows import (
    TriggerEventReference,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_client.contracts.records import Record, RecordConstructor
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReference,
    ResolutionContract,
    ResolutionContractReference,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import format_datetime
from cruxible_client.contracts.triggers import InternalActionName, TriggerSchedule
from cruxible_client.contracts.write import (
    AddChange,
    Change,
    ClaimValue,
    Evidence,
    ExpectedValue,
    RetireChange,
    RetireRequest,
    SetChange,
    SetRequest,
    SlotRef,
    WriteAccept,
    WriteOutcome,
    WriteRequest,
    WriteRetireReason,
    WriteRole,
)
from cruxible_client.errors import CoreError
from cruxible_client.transport.http import CruxibleClient, configured_principal_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.world import World

SDK_CONTRACT_SNAPSHOT_DIGEST = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST

_SUBJECT_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/"
    r"(?P<identifier>[a-z][a-z0-9_.-]{0,255})$"
)
# Object kinds this client understands from an accepted ClaimType envelope.
# Anything outside the set is skew, not caller error; see _claim_type_object_kind.
_CLAIM_TYPE_OBJECT_KINDS = frozenset({"literal", "subject", "exact_content"})
_CLAIM_ADAPTER: TypeAdapter[ClaimArtifactAny] = TypeAdapter(ClaimArtifactAny)


def _coordinate(value: api.AcceptedCoordinate | Mapping[str, object]) -> AcceptedCoordinate:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
    return AcceptedCoordinate.model_validate(payload)


def _get_coordinate(result: GetResult) -> AcceptedCoordinate:
    """The full accepted coordinate a ``get`` answered at (requested by the SDK)."""

    if result.accepted_coordinate is None:  # pragma: no cover - the SDK always asks for it
        raise ValueError("get answered without the full accepted coordinate the SDK requested")
    return _coordinate(result.accepted_coordinate)


def _api_coordinate(value: AcceptedCoordinate) -> api.AcceptedCoordinate:
    return api.AcceptedCoordinate.model_validate(value.model_dump(mode="json"))


def _subject_parts(value: str) -> tuple[str, str]:
    match = _SUBJECT_RE.fullmatch(value)
    if match is None:
        raise ValueError("subject must use canonical <subject-kind>/<subject-id> shorthand")
    return match["kind"], match["identifier"]


def _subject_address(value: str) -> SemanticAddress:
    kind, identifier = _subject_parts(value)
    return SemanticAddress.whole_artifact(f"subjects/{kind}/{identifier}.json")


def _address(value: str | TypedRef, expected: RefKind) -> str:
    if isinstance(value, str):
        if (
            expected is RefKind.SUBJECT
            and value.startswith("subjects/")
            and value.endswith(".json")
        ):
            shorthand = value[len("subjects/") : -len(".json")]
            _subject_parts(shorthand)
            return shorthand
        return value
    if value.kind is not expected:
        raise ReferenceKindError(
            f"expected {expected.value} reference, received {value.kind.value}"
        )
    return value.address


@dataclass(frozen=True)
class ClaimView:
    """The few Claim fields a caller reads, lifted out of the fact array."""

    claim_id: str
    revision: int
    subject: str
    predicate: str
    qualifier: str | None
    role: str
    object_kind: str
    # The object's value: a literal, a Subject path, or an exact-content
    # Claim's text (a ExactContentRef marker when it is not text).
    value: object
    lifecycle_state: str
    verdict: str
    captures: tuple[CaptureRef, ...]
    # An exact-content Claim's digest, the proof its text is ``value``.
    content_digest: str | None = None


def _address_path(value: object) -> str:
    if isinstance(value, Mapping):
        return str(value.get("artifact_path", ""))
    return ""


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum(value: _EnumT | str, kind: type[_EnumT], *, label: str) -> _EnumT:
    """Accept the enum or its exact string value.

    Every vocabulary here is a `str, Enum`, so a plain string reads as correct
    and only fails deep in the call as an AttributeError on `.value` -- at
    runtime, not at typecheck. Coerce at the boundary instead, and name the
    admissible values when the string is not one of them.
    """

    if isinstance(value, kind):
        return value
    if isinstance(value, str):
        try:
            return kind(value)
        except ValueError:
            admissible = ", ".join(sorted(item.value for item in kind))
            raise ValueError(f"{label} must be one of: {admissible}") from None
    raise TypeError(f"{label} must be a {kind.__name__} or one of its string values")


# The kind a daemon-resolved ``get`` reference names, as the SDK's RefKind.
_GET_REF_KINDS: Mapping[str, RefKind] = {
    "claim": RefKind.CLAIM,
    "subject": RefKind.SUBJECT,
    "claim_type": RefKind.CLAIM_TYPE,
    "procedure": RefKind.PROCEDURE,
    "query": RefKind.QUERY,
    "document": RefKind.DOCUMENT,
    "capture_contract": RefKind.CAPTURE_CONTRACT,
    "proposal": RefKind.PROPOSAL,
    "line": RefKind.LINE,
    "capture": RefKind.CAPTURE,
    "resolution_contract": RefKind.RESOLUTION_CONTRACT,
    "mandate": RefKind.MANDATE,
    "procedure_run": RefKind.PROCEDURE_RUN,
    "trigger": RefKind.TRIGGER,
    "principal": RefKind.PRINCIPAL,
    "approval_policy": RefKind.APPROVAL_POLICY,
    "procedure_runtime_policy": RefKind.PROCEDURE_RUNTIME_POLICY,
    "source_acquisition_policy": RefKind.SOURCE_ACQUISITION_POLICY,
    "provider_interface": RefKind.PROVIDER_INTERFACE,
}

_REFERENCE_KINDS: Mapping[RefKind, str] = {
    RefKind.SUBJECT: "Subject",
    RefKind.CLAIM_TYPE: "ClaimType",
    RefKind.CLAIM: "Claim",
    RefKind.PROCEDURE: "Procedure",
    RefKind.QUERY: "QueryDefinition",
    RefKind.SOURCE: "Source",
}


def _expectation(
    value: str | TypedRef,
    *,
    expected: RefKind,
    payload_path: str,
) -> AuthoringReferenceExpectation | None:
    if isinstance(value, str):
        return None
    _address(value, expected)
    if expected is RefKind.SLOT:
        return None
    if isinstance(value, (PendingSubjectRef, PendingClaimTypeRef)):
        # A same-set definition did not exist at the coordinate this ref names,
        # so asserting it there would refuse in preflight against the base tree.
        # The set lowers definitions before the members that read them.
        return None
    return AuthoringReferenceExpectation(
        payload_path=payload_path,
        artifact_kind=cast(Any, _REFERENCE_KINDS[expected]),
        address=_claim_id(cast(ClaimRef, value)) if expected is RefKind.CLAIM else value.address,
        minted_coordinate=value.coordinate,
    )


def _sorted_expectations(
    values: Sequence[AuthoringReferenceExpectation | None],
) -> tuple[AuthoringReferenceExpectation, ...]:
    return tuple(
        sorted(
            (value for value in values if value is not None),
            key=lambda item: (
                item.payload_path.encode("utf-8"),
                item.artifact_kind.encode("ascii"),
                item.address.encode("utf-8"),
            ),
        )
    )


def _program_stamp(operation: str, decisions: Mapping[str, object]) -> AuthoringProgramStamp:
    operation_value = AuthoringProgramOperation(operation=operation, decisions=dict(decisions))
    return AuthoringProgramStamp(
        program_digest=authoring_program_digest(
            sdk_contract_snapshot_digest=SDK_CONTRACT_SNAPSHOT_DIGEST,
            operations=(operation_value,),
        ),
        sdk_version=AUTHORING_SDK_VERSION,
        sdk_contract_snapshot_digest=SDK_CONTRACT_SNAPSHOT_DIGEST,
    )


def _claim_from_public_view(view: api.ClaimViewRecord) -> ClaimArtifactAny:
    """Reconstruct the exact Claim from its pure projection envelope and facts."""

    statement = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "cruxible.claim.statement"
        ),
        None,
    )
    backing = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "cruxible.claim.backing"
        ),
        None,
    )
    lifecycle = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "cruxible.claim.lifecycle"
        ),
        None,
    )
    identity = view.envelope.get("identity")
    artifact_format = view.envelope.get("format_tag")
    if not (
        isinstance(identity, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
        and isinstance(artifact_format, str)
    ):
        raise ValueError("Claim read lacks its complete canonical artifact")
    if artifact_format == "playbill-claim-v2":
        model: type[ClaimArtifactV2] | type[ClaimArtifact] = ClaimArtifactV2
    elif artifact_format == "playbill-claim-v3":
        model = ClaimArtifact
    else:
        raise ClaimUnsupportedFormatError(
            f"{ClaimUnsupportedFormatError.error_code}: {artifact_format!r}"
        )
    return _CLAIM_ADAPTER.validate_python(
        model.model_validate(
            {
                "artifact_format": artifact_format,
                "identity": {
                    "kind": "Claim",
                    "name": identity.removeprefix("Claim:"),
                },
                "statement": statement,
                "backing": backing,
                "pins": lifecycle.get("pins"),
                "lifecycle": lifecycle.get("lifecycle"),
                **(
                    {"retirement": lifecycle.get("retirement")}
                    if artifact_format == "playbill-claim-v3"
                    else {}
                ),
            }
        )
    )


@dataclass(frozen=True)
class KnowledgeCard:
    """What ``cx.get(ref)`` answered: the kind, identity and coordinate, and the value.

    ``value`` is a ``ClaimView`` for a Claim summary, the values-first card for any other
    summary, and the detail's payload otherwise. Next: ``card.ref`` to pass the thing on as a
    typed ref, or ``cx.get(card.identity, detail=...)`` for another detail.
    """

    kind: RefKind
    identity: str
    coordinate: AcceptedCoordinate
    value: object

    @property
    def ref(self) -> TypedRef:
        """This thing as a typed ref at the coordinate it was read at.

        Raises ``ReferenceKindError`` for a kind that mints no ref (an operational card).
        Next: pass it as ``subject=``, ``predicate=`` or ``value=``, or back to ``cx.get``.
        """

        constructors = {
            RefKind.SUBJECT: SubjectRef,
            RefKind.CLAIM_TYPE: ClaimTypeRef,
            RefKind.CLAIM: ClaimRef,
            RefKind.PROCEDURE: ProcedureRef,
            RefKind.QUERY: QueryRef,
            RefKind.SOURCE: SourceRef,
        }
        constructor = constructors.get(self.kind)
        if constructor is None:
            raise ReferenceKindError(f"{self.kind.value} cards do not mint references")
        return cast(TypedRef, constructor(address=self.identity, coordinate=self.coordinate))


@dataclass(frozen=True)
class NextPage:
    """The whole ``cx.next(...)`` queue for this caller: iterate it for the items.

    ``status`` is the environment the queue was read in. A row whose repair this caller cannot
    run stays with ``repair_requires`` set. Next: run an item's ``repair.command``, or
    ``cx.get(item.subject_identity)`` to look first.
    """

    coordinate: AcceptedCoordinate
    evaluation_time: str
    items: tuple[api.NextItem, ...]
    result_digest: str
    observed_domains: tuple[str, ...]
    unobserved_domains: tuple[str, ...]
    # The environment the queue was read in. A row whose repair this caller
    # cannot perform stays in `items` with `repair_requires` set.
    status: api.NextStatus
    attestation_head_digest: str | None = None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.items)


@dataclass(frozen=True)
class ClaimTypeDraft:
    """A ClaimType definition built by ``cx.claim_type(...)``, not yet proposed.

    Next: ``draft.propose(proposal_name=...)``, or
    ``cx.changes(rationale=...).claim_type(draft)`` to define it beside Claims that use it.
    """

    _playbill: Cruxible = field(repr=False, compare=False)
    definition: ClaimType

    @property
    def predicate(self) -> str:
        """The predicate this ClaimType defines. Next: ``draft.propose(proposal_name=...)``."""

        return self.definition.predicate

    def propose(self, *, proposal_name: str) -> ClaimTypeProposal:
        """Propose this ClaimType as its own proposal, with its evidence-policy lint.

        A ClaimType proposed on its own is linted the way `claim-type propose`
        lints it; the result carries that lint beside the plain proposal handle.

        Next: ``result.lint.warnings``, then ``result.proposal.review()``,
        ``.approve(...)`` and ``.activate()``.
        """

        result = self._playbill._client.propose_claim_type(
            self._playbill._instance_id,
            claim_type=self.definition.model_dump(mode="json"),
            proposal_name=proposal_name,
            base=_api_coordinate(self._playbill.coordinate),
        )
        admission = result.proposal.get("admission")
        proposal_id = admission.get("proposal_id") if isinstance(admission, Mapping) else None
        if not isinstance(proposal_id, str):
            raise ValueError("claim-type proposal omitted its proposal_id")
        return ClaimTypeProposal(
            proposal=Proposal(self._playbill, proposal_id),
            lint=result.lint or api.ClaimTypeProposalLint(warnings=()),
        )


@dataclass(frozen=True)
class _IntentDraft:
    _playbill: Cruxible = field(repr=False, compare=False)
    payload: (
        ClaimAuthoringPayloadV1
        | ClaimAuthoringPayloadV2
        | ClaimAuthoringPayload
        | ProcedureAuthoringPayloadV1
        | ProcedureAuthoringPayload
        | SubjectAuthoringPayload
        | ChangeSetAuthoringPayload
        | QueryDefinitionAuthoringPayload
    )
    reference_expectations: tuple[AuthoringReferenceExpectation, ...]
    program_stamp: AuthoringProgramStamp
    source_map: DiagnosticSourceMap

    def prepare(self) -> Intent:
        """Compile and preflight this draft as an intent, without submitting it.

        Next: ``intent.refused`` and ``intent.diagnostics`` (each names its call site), then
        ``intent.submit()``.
        """

        result = self._playbill._client.compile_authoring(
            self._playbill._instance_id,
            payload=self.payload.model_dump(mode="json"),
            reference_expectations=[
                item.model_dump(mode="json") for item in self.reference_expectations
            ],
            program_stamp=self.program_stamp.model_dump(mode="json"),
        )
        return Intent.from_preflight(self._playbill, self, result)

    def submit(self) -> Intent:
        """Compile and submit in one request; the daemon preflights once.

        Equivalent to ``prepare().submit()`` without the separate preflight and
        its round trips. A refused preflight returns an unsubmitted intent whose
        ``refused`` and ``diagnostics`` report it, exactly as ``prepare()`` does.

        Next: ``intent.proposal`` for the proposal, then ``proposal.review()``;
        ``intent.diagnostics`` if it was refused.
        """

        result = self._playbill._client.submit_authoring(
            self._playbill._instance_id,
            payload=self.payload.model_dump(mode="json"),
            reference_expectations=[
                item.model_dump(mode="json") for item in self.reference_expectations
            ],
            program_stamp=self.program_stamp.model_dump(mode="json"),
        )
        return Intent(
            self._playbill,
            self,
            result.intent,
            preflight=result.preflight,
            candidate_status=result.status,
        )


@dataclass(frozen=True)
class ClaimDraft(_IntentDraft):
    """One Claim drafted by ``cx.claim(...)``.

    Next: ``draft.submit()``, or ``draft.prepare()`` first.
    """

    def derived_by(self, derivation: object) -> ClaimDraft:
        """Not served: derivation carry needs its own approved contract.

        Raises ``CapabilityNotServed``. Next: ``draft.submit()`` without it.
        """

        del derivation
        raise CapabilityNotServed(
            code="cruxible.sdk.derivation_carry_not_served",
            capability="derivation_carry",
            repair=("Remove derived_by() or use a separately approved derivation-carry contract."),
        )


@dataclass(frozen=True)
class Prediction:
    """A proposed governed test; its hypothesis already exists in accepted state.

    Next: ``prediction.proposal.review()``; once accepted, ``cx.settle(...)`` settles it.
    """

    _playbill: Cruxible = field(repr=False, compare=False)
    contract_identity: str
    contract_digest: str
    intent_id: str
    proposal_id: str

    @property
    def proposal(self) -> Proposal:
        """The proposal that carries this prediction's ResolutionContract.

        Next: ``prediction.proposal.review()``, then approve and ``activate()``.
        """

        return Proposal(self._playbill, self.proposal_id)


@dataclass(frozen=True)
class PredictionSettlement:
    """How one prediction settled: its outcome and the relation that decided it.

    Next: ``cx.get(prediction_id)`` for the settled window.
    """

    prediction_id: str
    outcome: bool
    relation: dict[str, object]


@dataclass(frozen=True)
class ProcedureDraft(_IntentDraft):
    """One Procedure drafted by ``cx.procedure(definition=...)``.

    Next: ``draft.submit()``; once accepted, ``cx.accepted_procedure(name).run(...)``.
    """

    pass


@dataclass(frozen=True)
class QueryDraft(_IntentDraft):
    """One named query drafted by ``cx.query_definition(definition=...)``.

    Next: ``draft.submit()``; once accepted, ``cx.query(name=..., params={...})``.
    """

    pass


def carry(claim: str | ClaimRef) -> ClaimTypeSuccessionDependent:
    """Carry one dependent to the successor by re-pinning it, unchanged.

    Available when the dependent still says something true under the successor.
    A successor that changes `object_kind` refuses this for a live Claim: its
    object no longer says what the ClaimType now means.
    """

    return ClaimTypeSuccessionDependent(
        identity=_claim_identity(claim),
        disposition="successor",
    )


def rescind(claim: str | ClaimRef) -> ClaimTypeSuccessionDependent:
    """Tombstone one dependent because it should never have been stated.

    The tombstone keeps the exact statement it was accepted with, under the
    vocabulary it was accepted under -- that is what makes the record readable
    after the vocabulary moves, rather than silently rewritten.
    """

    return ClaimTypeSuccessionDependent(
        identity=_claim_identity(claim),
        disposition="retire",
        claim_retirement_reason="was-rescinded",
    )


def retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
) -> ClaimTypeSuccessionDependent:
    """Retire one dependent with an attributed reason as the succession lands."""

    return ClaimTypeSuccessionDependent(
        identity=_claim_identity(claim),
        disposition="retire",
        claim_retirement_reason=reason,
        claim_effective_until=effective_until,
    )


def re_author(
    claim: str | ClaimRef,
    *,
    with_: str | ClaimRef | None = None,
) -> ClaimTypeSuccessionDependent:
    """Say this dependent again, under the successor, as a sibling Claim member.

    The sibling revises this same Claim -- a re-authoring keeps the identity,
    the subject, the predicate and the exact predecessor digest of what it
    re-states -- so `with_` is only ever an explicit spelling of what `claim`
    already says, and `re_author(claim)` alone is complete.
    """

    return ClaimTypeSuccessionDependent(
        identity=_claim_identity(claim),
        disposition="re_author",
        successor_claim_id=_claim_id(claim if with_ is None else with_),
    )


def _claim_id(claim: str | ClaimRef) -> str:
    return _address(claim, RefKind.CLAIM).removeprefix("Claim:")


def _claim_identity(claim: str | ClaimRef) -> ArtifactIdentity:
    return ArtifactIdentity(kind="Claim", name=_claim_id(claim))


@dataclass(frozen=True)
class _ChangeSetMember:
    payload: AuthoringChangeSetMember
    expectations: tuple[AuthoringReferenceExpectation, ...]
    source_map: DiagnosticSourceMap
    decisions: dict[str, object]


@dataclass
class ChangeSetDraft:
    """One authoring intent under construction, carrying any mix of members.

    Authoring surfaces and changesets are one-to-one: everything added here
    lowers once, proposes once and generates once, and the whole intent admits
    or refuses together. There is no member ceiling -- how many members one
    daemon will receive in a single submission is an operator admission knob.

    Next: add members (``.claim``, ``.subject``, ``.claim_type``, ``.retire``, ...), then
    ``.submit()``.
    """

    _playbill: Cruxible = field(repr=False, compare=False)
    rationale: str | None = None
    _members: list[_ChangeSetMember] = field(default_factory=list, repr=False)

    def claim(
        self,
        *,
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None = None,
        copied_from: EvidenceSelection | CaptureRef | None = None,
        self_source: str | None = None,
        qualifier: str | None = None,
        effective_period: EffectivePeriod | None = None,
        revises: str | ClaimRef | None = None,
        dispositions: Mapping[str | ClaimRef, Disposition | str] | None = None,
        subject_definition: SubjectDraft | None = None,
        claim_type_definition: ClaimTypeDraft | None = None,
    ) -> ChangeSetDraft:
        """Add one Claim to this changeset; the signature is `Cruxible.claim`'s.

        Next: more members, then ``.submit()``.
        """

        draft = self._playbill._claim_draft(
            sites=capture_keyword_sites("claim", stacklevel=1),
            subject=subject,
            predicate=predicate,
            value=value,
            role=role,
            rationale=rationale,
            supported_by=supported_by,
            copied_from=copied_from,
            self_source=self_source,
            qualifier=qualifier,
            effective_period=effective_period,
            revises=revises,
            dispositions={} if dispositions is None else dispositions,
            subject_definition=subject_definition,
            claim_type_definition=claim_type_definition,
            staged_claim_types=self._staged_claim_types(),
        )
        assert isinstance(draft.payload, ClaimAuthoringPayloadV1)
        self._members.append(
            _ChangeSetMember(
                payload=draft.payload,
                expectations=draft.reference_expectations,
                source_map=draft.source_map,
                decisions={"kind": "claim", "predicate": _address(predicate, RefKind.CLAIM_TYPE)},
            )
        )
        return self

    def signed_attestation(self, attestation: ClaimAttestation) -> ChangeSetDraft:
        """Add an already signed statement, without changing its bytes or Claim.

        It becomes accepted only when this changeset passes ordinary approval
        and activation. The authenticated submitter need not be its signer.

        Next: ``.submit()``.
        """
        self._members.append(
            _ChangeSetMember(
                payload=AttestationAuthoringPayload(attestation=attestation),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={
                    "kind": "attestation",
                    "claim": attestation.statement.claim_identity.qualified,
                },
            )
        )
        return self

    def attestation(
        self,
        claim: ClaimRef | str,
        *,
        stance: ClaimStance,
        signer: ClaimAttestationSigner,
        valid_until: datetime | None = None,
    ) -> ChangeSetDraft:
        """Sign an exact Claim and stage it in this governed batch.

        Next: ``.submit()``.
        """
        identity = claim.address if isinstance(claim, ClaimRef) else claim
        prepared = PreparedClaimAttestationRequest(
            claim_id=identity.removeprefix("Claim:"),
            attestation_basis="examined_existing",
            stance=stance,
            valid_until=valid_until,
            referent_coordinate=claim.coordinate
            if isinstance(claim, ClaimRef)
            else self._playbill.coordinate
            if self._playbill._pinned
            else None,
            attested_at=datetime.fromisoformat(self._playbill._evaluation_time()),
        )
        return self.signed_attestation(
            prepare_claim_attestation(
                self._playbill._client,
                self._playbill._instance_id,
                prepared=prepared,
                signer=signer,
            )
        )

    def _staged_claim_types(self) -> dict[str, ClaimType]:
        """The ClaimType definitions this set stages, by predicate.

        A ref returned by `.claim_type(...)` already carries its object kind, so
        a caller who keeps the ref needs nothing here. A caller who names the
        predicate as a string does: the object-kind lookup would otherwise skip
        the definition sitting in this very set and read the accepted
        coordinate, where in a first generation there is no coordinate at all.
        Same set, same answer, whichever way the predicate is spelled.
        """

        return {
            member.payload.claim_type.predicate: member.payload.claim_type
            for member in self._members
            if isinstance(member.payload, ClaimTypeAuthoringPayload)
        }

    def subject(self, definition: SubjectDraft | SubjectShell) -> PendingSubjectRef:
        """Define one Subject inside this changeset, and return a ref to it.

        A Claim member may still carry its Subject as a dependency draft; this
        is for the Subjects a set defines that no single Claim owns.

        The ref it returns is usable as `subject=` or `value=` in the same set,
        which is what lets one changeset define a Subject and say something
        about it without the caller retyping the address as a string.

        Next: ``.claim(subject=ref, ...)`` about it, then ``.submit()``.
        """

        shell = definition.shell if isinstance(definition, SubjectDraft) else definition
        self._members.append(
            _ChangeSetMember(
                payload=SubjectAuthoringPayload(subject=shell),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "subject", "subject": shell.identity.name},
            )
        )
        return PendingSubjectRef(
            address=shell.identity.name,
            coordinate=self._playbill.coordinate,
        )

    def capture_contract(self, contract: CaptureContract) -> ChangeSetDraft:
        """Define one CaptureContract inside this changeset.

        Next: more members, then ``.submit()``.
        """

        self._members.append(
            _ChangeSetMember(
                payload=CaptureContractAuthoringPayload(capture_contract=contract),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "capture_contract", "name": contract.identity.name},
            )
        )
        return self

    def resolution_contract(self, contract: ResolutionContract) -> ChangeSetDraft:
        """Define one ResolutionContract inside this changeset.

        Next: more members, then ``.submit()``.
        """

        self._members.append(
            _ChangeSetMember(
                payload=ResolutionContractAuthoringPayload(resolution_contract=contract),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "resolution_contract", "name": contract.identity.name},
            )
        )
        return self

    def acquisition_policy(self, policy: SourceAcquisitionPolicy) -> ChangeSetDraft:
        """Define one SourceAcquisitionPolicy inside this changeset.

        Next: ``.line(..., acquisition_policy=name)``, then ``.submit()``.
        """

        self._members.append(
            _ChangeSetMember(
                payload=SourceAcquisitionPolicyAuthoringPayload(acquisition_policy=policy),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "acquisition_policy", "name": policy.identity.name},
            )
        )
        return self

    def procedure(
        self, *, definition: ProcedureInput | ProcedureSequence | ProcedureBlueprint
    ) -> ChangeSetDraft:
        """Compose a Procedure with its Line and mandate in one existing changeset.

        Next: ``.line(name=..., procedure=...)`` to run it, then ``.submit()``.
        """
        draft = self._playbill.procedure(definition=definition)
        assert isinstance(draft.payload, (ProcedureAuthoringPayloadV1, ProcedureAuthoringPayload))
        self._members.append(
            _ChangeSetMember(
                payload=draft.payload,
                expectations=draft.reference_expectations,
                source_map=draft.source_map,
                decisions={"kind": "procedure", "payload": draft.payload.model_dump(mode="json")},
            )
        )
        return self

    def procedure_mandate(self, definition: ProcedureMandateInput) -> ChangeSetDraft:
        """Stage a typed mandate through the shared authoring-input lowering.

        Next: more members, then ``.submit()``.
        """
        payload = lower_authoring_input(definition)
        assert isinstance(payload, ProcedureMandateAuthoringPayload)
        self._members.append(
            _ChangeSetMember(
                payload=payload,
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions=definition.model_dump(mode="json"),
            )
        )
        return self

    def line(
        self,
        *,
        name: str,
        procedure: str,
        acquisition_policy: str | None = None,
        max_authority: Literal["observe", "propose", "settle"] | None = None,
        trigger_input: str | None = None,
        parameters: CanonicalValue | None = None,
        budgets: Mapping[str, int] | None = None,
        occurrence_epoch: int = 1,
        retire: bool = False,
    ) -> ChangeSetDraft:
        """Define one Line inside this changeset, naming its Procedure and policy.

        Lowering resolves both names -- accepted at the base or defined earlier
        in this same set -- into the exact pins the LineSpec carries. A Line
        runs when run explicitly, or when a Trigger aimed at it fires
        (:meth:`trigger`), and inherits the Procedure's hard caps as its budget
        unless one is given. ``trigger_input`` binds the triggering Capture to a
        named Source alias; the Line then accepts only that Source's exact
        CaptureContract event, and every Trigger aimed at it must fire on it.
        Missing or ineligible trigger material refuses admission, without a re-fetch.

        Lowering refuses a Procedure that is not graph-v4/v5/v6 and one whose
        Source nodes leave a Provider slot open: the Line pins exactly what the
        Procedure names, and an open slot is nothing to pin.
        ``acquisition_policy`` is required only when the Procedure has Source
        nodes. ``parameters`` is the Procedure's input record; lowering checks it
        against the Procedure's input contract. ``max_authority`` (observe,
        propose or settle) caps this Line below its Procedure's own capability
        and defaults to it. A Line that proposes or settles also needs a live
        ProcedureMandate covering its Procedure before it can run or be armed;
        an observe-only Line needs none.

        Next: ``.submit()``; once accepted, ``cx.arm_line(name)`` or ``cx.run_line(name)``.
        """

        self._members.append(
            _ChangeSetMember(
                payload=LineAuthoringPayload(
                    name=name,
                    procedure_name=procedure,
                    acquisition_policy_name=acquisition_policy,
                    max_authority=max_authority,
                    trigger_input=trigger_input,
                    parameters={} if parameters is None else parameters,
                    budgets=None if budgets is None else dict(budgets),
                    occurrence_epoch=occurrence_epoch,
                    retire=retire,
                ),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "line", "name": name, "procedure": procedure},
            )
        )
        return self

    def trigger(
        self,
        *,
        name: str,
        schedule: TriggerSchedule,
        line: str | None = None,
        action: InternalActionName | None = None,
        retire: bool = False,
    ) -> ChangeSetDraft:
        """Define one Trigger inside this changeset: a schedule aimed at one target.

        Name exactly one of ``line`` (an accepted Line, or one defined in this
        same set) or ``action`` (a registered internal action such as
        ``floor.refresh``), which takes cadence, cron or generation_accepted;
        a Line takes any schedule that supplies its input. A
        Line can have several
        Triggers; retiring a Line needs its live Triggers retired or retargeted
        in the same set.

        Next: ``.submit()`` to propose the changeset for review and acceptance.
        """

        payload = TriggerAuthoringPayload(
            name=name, schedule=schedule, line_name=line, action=action, retire=retire
        )
        self._members.append(
            _ChangeSetMember(
                payload=payload,
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "trigger", "name": name},
            )
        )
        return self

    def query_definition(
        self,
        definition: QueryDefinitionInput,
        *,
        vocabulary: Sequence[ClaimTypeRef] = (),
    ) -> ChangeSetDraft:
        """Add a named query after its vocabulary definitions in this changeset.

        Next: ``.submit()``; once accepted, ``cx.query(name=..., params={...})``.
        """
        draft = self._playbill.query_definition(definition=definition, vocabulary=vocabulary)
        assert isinstance(draft.payload, QueryDefinitionAuthoringPayload)
        self._members.append(
            _ChangeSetMember(
                payload=draft.payload,
                expectations=draft.reference_expectations,
                source_map=draft.source_map,
                decisions=definition.model_dump(mode="json"),
            )
        )
        return self

    def claim_type(self, definition: ClaimTypeDraft | ClaimType) -> PendingClaimTypeRef:
        """Define one whole ClaimType inside this changeset, and return a ref.

        Succeeding an accepted ClaimType stays on `/claim-types/proposals`,
        where the migration a succession demands is decided.

        The ref it returns is usable as `predicate=` in the same set, and
        carries the object kind the definition declares so a Claim under it
        lowers without reading a ClaimType that is not accepted yet.

        Next: ``.claim(predicate=ref, ...)`` under it, then ``.submit()``.
        """

        value = definition.definition if isinstance(definition, ClaimTypeDraft) else definition
        self._members.append(
            _ChangeSetMember(
                payload=ClaimTypeAuthoringPayload(claim_type=value),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "claim_type", "predicate": value.predicate},
            )
        )
        return PendingClaimTypeRef(
            address=value.predicate,
            coordinate=self._playbill.coordinate,
            object_kind=value.object_kind,
        )

    def retire(
        self,
        claim: str | ClaimRef,
        *,
        reason: ClaimRetirementReason,
        effective_until: datetime | None = None,
        dependents: Sequence[ClaimRetireDependent] = (),
    ) -> ChangeSetDraft:
        """Retire one accepted Claim, and its live closure, inside this changeset.

        Takes a Claim ID in either spelling the SDK's rows and refs use
        (`CLM-...` or `Claim:CLM-...`). The member carries the one canonical bare
        spelling as `retires`, which is what keeps two spellings of one
        retirement on one member identity and one digest. `Cruxible.retire` is
        the typed write verb for the common case: it computes the closure.

        Next: more members, then ``.submit()``.
        """

        address = _address(claim, RefKind.CLAIM).removeprefix("Claim:")
        self._members.append(
            _ChangeSetMember(
                payload=ClaimRetirementMember(
                    retires=address,
                    reason=reason,
                    effective_until=effective_until,
                    dependents=tuple(dependents),
                ),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "retire", "claim": address, "reason": reason},
            )
        )
        return self

    def succeed_claim_type(
        self,
        successor: ClaimTypeDraft | ClaimType,
        *,
        dependents: Sequence[ClaimTypeSuccessionDependent] = (),
    ) -> ChangeSetDraft:
        """Succeed one accepted ClaimType, and settle its closure, in this set.

        Vocabulary evolution is one epistemic move -- "I need this distinction,
        and here is everything it changes" -- so it lands in the same signed
        generation as the Claims that speak the new vocabulary. Write the
        dependents with `carry`, `rescind`, `retire` and `re_author`; the
        closure must be exact, and preflight names every member of it that is
        still missing.

        Next: ``.prepare()``; its diagnostics name any dependent still missing.
        """

        value = successor.definition if isinstance(successor, ClaimTypeDraft) else successor
        self._members.append(
            _ChangeSetMember(
                payload=ClaimTypeSuccessionMember(
                    successor=value,
                    dependents=tuple(
                        sorted(
                            dependents,
                            key=lambda item: item.identity.qualified.encode("utf-8"),
                        )
                    ),
                ),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={
                    "kind": "claim_type_succession",
                    "predicate": value.predicate,
                    "dependents": [
                        {"claim": item.identity.qualified, "disposition": item.disposition}
                        for item in sorted(
                            dependents,
                            key=lambda item: item.identity.qualified.encode("utf-8"),
                        )
                    ],
                },
            )
        )
        return self

    def prepare(self) -> Intent:
        """Compile and preflight the whole changeset as one intent.

        Next: ``intent.diagnostics`` if refused, else ``intent.submit()``.
        """

        return self._compiled().prepare()

    def submit(self) -> Intent:
        """Compile and submit the whole changeset as one intent in one request.

        Next: ``intent.proposal.review()``, then approve and accept it.
        """

        return self._compiled().submit()

    def _compiled(self) -> _IntentDraft:
        """Fold every member into exactly one intent draft."""

        if not self._members:
            raise ValueError("a changeset needs at least one member")
        payload = ChangeSetAuthoringPayload(
            members=tuple(
                sorted(
                    (member.payload for member in self._members),
                    key=lambda item: authoring_member_identity(item).encode("utf-8"),
                )
            ),
            # The prose travels now, instead of only being hashed into the
            # program digest: the daemon writes it as the candidate commit's
            # subject, which is the one place a reviewer reading Git looks for
            # why a change set exists.
            rationale=self.rationale,
        )
        index_by_identity = {
            authoring_member_identity(member): index for index, member in enumerate(payload.members)
        }
        expectations: list[AuthoringReferenceExpectation | None] = []
        entries: list[SourceMapEntry] = []
        decisions: list[dict[str, object]] = []
        for member in sorted(
            self._members,
            key=lambda item: index_by_identity[authoring_member_identity(item.payload)],
        ):
            prefix = f"members[{index_by_identity[authoring_member_identity(member.payload)]}]."
            expectations.extend(
                expectation.model_copy(update={"payload_path": prefix + expectation.payload_path})
                for expectation in member.expectations
            )
            entries.extend(
                SourceMapEntry(
                    builder_path=entry.builder_path,
                    emitted_paths=tuple(prefix + path for path in entry.emitted_paths),
                    call_site=entry.call_site,
                )
                for entry in member.source_map.entries
            )
            decisions.append(dict(member.decisions))
        return _IntentDraft(
            self._playbill,
            payload,
            _sorted_expectations(expectations),
            _program_stamp("changes", {"members": decisions, "rationale": self.rationale}),
            DiagnosticSourceMap(tuple(entries)),
        )


class _Unset(Enum):
    TOKEN = "unset"


_UNSET = _Unset.TOKEN
WriteAt = AcceptedCoordinate | api.AcceptedCoordinate | str | None


def _write_subject(subject: str | SubjectRef) -> str:
    return subject.address if isinstance(subject, SubjectRef) else subject


def _write_field(field_name: str | ClaimTypeRef) -> str:
    return field_name.address if isinstance(field_name, ClaimTypeRef) else field_name


def _write_value(value: ClaimValue | SubjectRef | LiteralValue) -> ClaimValue:
    if isinstance(value, SubjectRef):
        return value.address
    if isinstance(value, LiteralValue):
        if not isinstance(value.value, bool | int | float | str):
            raise ValueError("the write verbs take a scalar value; this literal is structured")
        return value.value
    return value


WriteExpect = (
    ClaimValue | SubjectRef | LiteralValue | Sequence[ClaimValue | SubjectRef | LiteralValue]
)


def _write_expect(expect: WriteExpect | None) -> ExpectedValue | None:
    """``expect`` on the wire: one value, or every live value as a tuple."""

    if expect is None:
        return None
    if isinstance(expect, bool | int | float | str | SubjectRef | LiteralValue):
        return _write_value(expect)
    return tuple(_write_value(item) for item in expect)


def _write_target(target: str | ClaimRef | SlotRef) -> str | SlotRef:
    if isinstance(target, ClaimRef):
        return target.address
    return target


_WriteSubject = str | SubjectRef
_WriteField = str | ClaimTypeRef
_WriteValue = ClaimValue | SubjectRef | LiteralValue


def _batch_operands(
    verb: str, operands: tuple[Any, ...], subject: _WriteSubject | None
) -> tuple[str | None, str, ClaimValue]:
    """``(subject, field, value)`` positionally, or ``(field, value)`` with a default subject."""

    if len(operands) == 3:
        if subject is not None:
            raise TypeError(f"{verb}() names its subject once: positionally or as subject=")
        subject, field_name, value = operands
    elif len(operands) == 2:
        field_name, value = operands
    else:
        raise TypeError(
            f"{verb}() takes (subject, field, value), or (field, value) under a default subject"
        )
    return (
        None if subject is None else _write_subject(subject),
        _write_field(field_name),
        _write_value(value),
    )


class WriteBatch:
    """Changes that are written together, as one change set: ``cx.changes(because=...)``.

    ``set`` replaces a single-value field, ``add`` puts one more value in a
    many-valued field, and ``retire`` ends one live Claim; ``write()`` sends them
    all and returns the outcome, raising ``WriteRefusalError`` on a refusal.

    ``cx.changes(because=..., subject="kind/id")`` names the Subject once:
    ``.set(field, value)`` and ``.add(field, value)`` are about it, and
    ``.retire(SlotRef(field=...))`` ends one of its fields. A change that names
    its own subject overrides the default.
    """

    def __init__(self, cx: Cruxible, *, because: str, subject: _WriteSubject | None = None) -> None:
        self._playbill = cx
        self.because = because
        self.subject = None if subject is None else _write_subject(subject)
        self.changes: list[Change] = []

    @overload
    def set(
        self,
        subject: _WriteSubject,
        field: _WriteField,
        value: _WriteValue,
        /,
        *,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        contend: bool = False,
        expect: WriteExpect | None = None,
    ) -> WriteBatch: ...

    @overload
    def set(
        self,
        field: _WriteField,
        value: _WriteValue,
        /,
        *,
        subject: _WriteSubject | None = None,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        contend: bool = False,
        expect: WriteExpect | None = None,
    ) -> WriteBatch: ...

    def set(
        self,
        *operands: Any,
        subject: _WriteSubject | None = None,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        contend: bool = False,
        expect: WriteExpect | None = None,
    ) -> WriteBatch:
        """Replace the live value of one single-value field.

        Spelled ``set(field, value)`` or ``set(subject, field, value)``. ``expect`` refuses
        unless the field holds that value now. Next: more changes, then ``.write()``.
        """

        named, field_name, value = _batch_operands("set", operands, subject)
        self.changes.append(
            SetChange(
                subject=named,
                field=field_name,
                value=value,
                evidence=evidence,
                role=role,
                contend=contend,
                expect=_write_expect(expect),
            )
        )
        return self

    @overload
    def add(
        self,
        subject: _WriteSubject,
        field: _WriteField,
        value: _WriteValue,
        /,
        *,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        expect_absent: bool = False,
    ) -> WriteBatch: ...

    @overload
    def add(
        self,
        field: _WriteField,
        value: _WriteValue,
        /,
        *,
        subject: _WriteSubject | None = None,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        expect_absent: bool = False,
    ) -> WriteBatch: ...

    def add(
        self,
        *operands: Any,
        subject: _WriteSubject | None = None,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        expect_absent: bool = False,
    ) -> WriteBatch:
        """Add one more value to a many-valued field.

        Spelled ``add(field, value)`` or ``add(subject, field, value)``.
        ``expect_absent=True`` refuses a value already there. Next: more changes, then
        ``.write()``.
        """

        named, field_name, value = _batch_operands("add", operands, subject)
        self.changes.append(
            AddChange(
                subject=named,
                field=field_name,
                value=value,
                evidence=evidence,
                role=role,
                expect_absent=expect_absent,
            )
        )
        return self

    def retire(
        self,
        target: str | ClaimRef | SlotRef,
        *,
        because: str | None = None,
        reason: WriteRetireReason = "was-rescinded",
        expect: WriteExpect | None = None,
    ) -> WriteBatch:
        """End one live Claim, named by ID or ``SlotRef(subject=..., field=...)``.

        Its dependents retire with it. Next: more changes, then ``.write()``.
        """

        self.changes.append(
            RetireChange(
                target=_write_target(target),
                because=because,
                reason=reason,
                expect=_write_expect(expect),
            )
        )
        return self

    def write(
        self,
        *,
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
        at: WriteAt | _Unset = _UNSET,
    ) -> WriteOutcome:
        """Send every change as one change set and return the outcome.

        ``dry_run=True`` checks and writes nothing; ``accept="never"`` only proposes; ``at``
        refuses if a field moved since that coordinate. A refusal raises
        ``WriteRefusalError``. Next: ``outcome.next`` for what is still needed,
        ``outcome.warnings`` for verdicts that are not supported.
        """

        if not self.changes:
            raise ValueError("a write needs at least one change")
        return self._playbill._write(
            WriteRequest(
                because=self.because,
                subject=self.subject,
                changes=tuple(self.changes),
                dry_run=dry_run,
                accept=accept,
                at=self._playbill._write_at(at),
                surface="sdk",
                full_coordinate=True,
            )
        )

    def __repr__(self) -> str:
        spelled = ", ".join(
            f"{item.op} {item.subject or self.subject or ''} {item.field}".replace("  ", " ")
            if not isinstance(item, RetireChange)
            else f"retire {item.target}"
            for item in self.changes
        )
        about = "" if self.subject is None else f", subject={self.subject!r}"
        return f"WriteBatch(because={self.because!r}{about}, changes=[{spelled}])"


@dataclass(frozen=True)
class SubjectDraft(_IntentDraft):
    """One Subject definition drafted by ``cx.subject(...)`` or ``w.<kind>.define(id)``.

    Next: ``draft.submit()``, or ``cx.changes(rationale=...).subject(draft)`` with other
    members.
    """

    shell: SubjectShell

    @property
    def address(self) -> str:
        """The Subject's ``kind/id``. Next: ``draft.submit()``, then ``cx.get(draft.address)``."""

        return self.shell.identity.name


class Intent:
    """One authoring intent: a draft compiled by the daemon, then preflighted and submitted.

    An intent is revised in place (``reprepare``, ``rebase``) and owns the proposal its
    submission makes. Next: ``intent.submit()``, then ``intent.proposal.review()``.
    """

    def __init__(
        self,
        cx: Cruxible,
        draft: _IntentDraft | None,
        raw: Mapping[str, object],
        *,
        preflight: api.AuthoringPreflightResult | None = None,
        candidate_status: api.CandidateStatusRecord | None = None,
    ) -> None:
        self._playbill = cx
        self._draft = draft
        self._raw = dict(raw)
        self._preflight = preflight
        self._candidate_status = candidate_status

    @classmethod
    def from_preflight(
        cls,
        cx: Cruxible,
        draft: _IntentDraft,
        result: api.AuthoringPreflightResult,
    ) -> Intent:
        """Build the handle for an intent a preflight just named. Next: ``intent.submit()``."""

        raw = cx._client.get_authoring_intent(cx._instance_id, result.certificate.intent_id).intent
        return cls(cx, draft, raw, preflight=result)

    def __repr__(self) -> str:
        # No I/O: only what this handle last observed.
        status = self._candidate_status
        if self.refused:
            state = f"refused, {len(self.diagnostics)} diagnostics"
        elif status is not None:
            state = status.state
        elif self._preflight is not None:
            state = "prepared"
        else:
            state = "not observed"
        proposal = (
            ""
            if status is None or status.proposal_id is None
            else f", proposal={status.proposal_id!r}"
        )
        return (
            f"Intent({self._raw.get('intent_id')!r}, "
            f"revision={self._raw.get('intent_revision')}, {state}{proposal})"
        )

    @property
    def intent_id(self) -> str:
        """The intent's ID. Next: ``cx.resume_intent(intent_id)`` from another process."""

        value = self._raw.get("intent_id")
        if not isinstance(value, str):
            raise ValueError("authoring intent response omitted intent_id")
        return value

    @property
    def revision(self) -> int:
        """The intent revision last read. Next: ``intent.status()`` for its candidate state."""

        value = self._raw.get("intent_revision")
        if not isinstance(value, int):
            raise ValueError("authoring intent response omitted intent_revision")
        return value

    @property
    def refused(self) -> bool:
        """Whether the last preflight refused. Next: ``intent.diagnostics`` for why."""

        return self._preflight is not None and self._preflight.verdict == "refused"

    @property
    def lint(self) -> api.ClaimTypeProposalLint | None:
        """The ClaimType lint the last preflight returned, if any. Next: ``intent.warnings``."""

        return None if self._preflight is None else self._preflight.lint

    @property
    def warnings(self) -> tuple[api.ClaimTypeLintWarning, ...]:
        """Lint warnings from the last preflight.

        Next: fix them, then ``intent.reprepare(draft=...)``.
        """

        lint = self.lint
        return () if lint is None else tuple(lint.warnings)

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """Why the last preflight refused, each with its repair and Python call site.

        Next: fix the draft and ``intent.reprepare(draft=...)``.
        """

        if self._preflight is None:
            return ()
        return tuple(
            Diagnostic(
                code=item.code,
                stage=item.stage,
                offending_element=item.offending_element,
                message=item.message,
                repair=tuple(repair.model_dump(mode="json") for repair in item.repairs),
                owner=item.owner,
                disposition=item.disposition,
                call_site=(
                    None
                    if self._draft is None
                    else self._draft.source_map.locate(item.offending_element)
                ),
            )
            for item in self._preflight.frontier.diagnostics
        )

    @property
    def path_to_acceptance(self) -> tuple[dict[str, object], ...]:
        """The remaining steps to acceptance, read fresh from the daemon.

        Next: the first step's operation, often ``proposal.review()`` and approval.
        """

        status = self.status()
        return tuple(cast(dict[str, object], item) for item in status.path_to_acceptance)

    @property
    def proposal(self) -> Proposal | None:
        """Last observed proposal identity, without a server read.

        Populated by resume_intent(), submit() or status(); None means no proposal was observed
        for this local intent revision. Call status() for fresh server state.
        This handle is not review, approval, or proof of activation eligibility.

        Next: ``proposal.review()``.
        """

        status = self._candidate_status
        if status is None or status.proposal_id is None:
            return None
        return self._playbill.proposal(status.proposal_id)

    def prepare(self) -> Intent:
        """Preflight this intent again at current head, without changing its draft.

        Next: ``intent.refused`` and ``intent.diagnostics``, then ``intent.submit()``.
        """

        self._candidate_status = None
        result = self._playbill._client.preflight_authoring_intent(
            self._playbill._instance_id, self.intent_id
        )
        self._preflight = result
        self._raw = self._playbill._client.get_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        return self

    def reprepare(self, *, draft: ClaimDraft | ProcedureDraft | SubjectDraft) -> Intent:
        """Replace this intent's draft and preflight the new revision.

        Next: ``intent.diagnostics`` if refused, else ``intent.submit()``.
        """

        if draft._playbill is not self._playbill:
            raise ValueError("replacement draft belongs to another Cruxible connection")
        self._candidate_status = None
        result = self._playbill._client.compile_authoring(
            self._playbill._instance_id,
            payload=draft.payload.model_dump(mode="json"),
            intent_id=self.intent_id,
            reference_expectations=[
                item.model_dump(mode="json") for item in draft.reference_expectations
            ],
            program_stamp=draft.program_stamp.model_dump(mode="json"),
        )
        self._draft = draft
        self._preflight = result
        self._raw = self._playbill._client.get_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        return self

    def submit(self) -> Intent:
        """Submit this intent: the daemon admits it as a proposal.

        Next: ``intent.proposal.review()``, then approve and ``activate()``;
        ``intent.status()`` for its state.
        """

        self._candidate_status = None
        result = self._playbill._client.submit_authoring_intent(
            self._playbill._instance_id, self.intent_id
        )
        self._raw = result.intent
        self._candidate_status = result.status
        return self

    def status(self) -> api.CandidateStatusRecord:
        """Read this intent's candidate state fresh from the daemon.

        Next: ``intent.path_to_acceptance`` for what remains, or ``intent.rebase()`` if head
        moved past it.
        """

        status = self._playbill._client.authoring_intent_status(
            self._playbill._instance_id, self.intent_id
        )
        self._candidate_status = status
        return status

    def rebase(self) -> Intent:
        """Rebase this intent onto current head, dropping its last preflight.

        Next: ``intent.prepare()``, then ``intent.submit()``.
        """

        self._candidate_status = None
        self._raw = self._playbill._client.rebase_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        self._preflight = None
        self._candidate_status = None
        return self

    def wait_for_acceptance(
        self,
        *,
        timeout: Duration,
        poll_interval: Duration,
    ) -> api.CandidateStatusRecord:
        """Poll ``status()`` until accepted, terminal or superseded, or until ``timeout``.

        Next: ``cx.get(ref)`` to read what was accepted.
        """

        return cast(
            api.CandidateStatusRecord,
            _wait_for_status(self.status, timeout=timeout, poll_interval=poll_interval),
        )


class Proposal:
    """A handle on one admitted proposal, by ID; nothing is read until asked.

    Read it with ``cx.get(proposal_id)``: status, verdict, changes, and for a refused
    proposal every refusal with its repair. Next: ``proposal.review()``, then
    ``proposal.approve(signer=..., reviewed=...)`` and ``proposal.activate()``.
    """

    def __init__(self, cx: Cruxible, proposal_id: str) -> None:
        self._playbill = cx
        self.proposal_id = proposal_id

    def review(self) -> ReviewedProposal:
        """Fetch an immutable full review; inspect its details before approving.

        Next: ``proposal.approve(signer=..., reviewed=review)``.
        """
        return review_proposal(self._playbill, self.proposal_id)

    def approve(self, *, signer: ApprovalSigner, reviewed: ReviewedProposal) -> api.ApprovalReceipt:
        """Sign this exact review with caller-configured custody; never activate.

        Next: ``proposal.activate()`` once enough approvals are in.
        """
        return approve_reviewed(self._playbill, self.proposal_id, signer=signer, reviewed=reviewed)

    def activate(self) -> api.ActivationReceipt:
        """Activate this proposal once its approvals are in: ``Cruxible.activate`` by handle.

        Next: ``cx.at(receipt.accepted_coordinate)`` to read exactly what was accepted.
        """
        return self._playbill.activate(self.proposal_id)

    def readmit(
        self, *, dry_run: bool | None = None, at: str | None = None
    ) -> api.ProposalReadmitResult:
        """Re-admit this stale proposal at the current head, under a new proposal ID.

        Approvals are signed over the exact evaluation, so a stale proposal is
        readmitted explicitly and reviewed again. Next: ``cx.proposal(result's new
        proposal ID).review()``.
        """
        return self._playbill._client.readmit_proposal(
            self._playbill._instance_id, self.proposal_id, dry_run=dry_run, at=at
        )

    def __repr__(self) -> str:
        return f"Proposal({self.proposal_id!r})"


@dataclass(frozen=True)
class ClaimTypeProposal:
    """What ``ClaimTypeDraft.propose`` returns: the proposal handle and its lint.

    ``lint`` is the same typed evidence-policy lint `claim-type propose` serves;
    an empty ``warnings`` means the policy drew none.

    Next: ``result.proposal.review()``.
    """

    proposal: Proposal
    lint: api.ClaimTypeProposalLint


def _wait_for_status(call: Any, *, timeout: Duration, poll_interval: Duration) -> Any:
    deadline = time.monotonic_ns() + timeout.value * 1_000
    while True:
        status = call()
        if status.state in {"accepted", "terminal", "superseded"}:
            return status
        if time.monotonic_ns() >= deadline:
            return status
        time.sleep(poll_interval.value / 1_000_000)


class Cruxible:
    """One connection to one Cruxible instance: read, write, see what needs doing.

    Open one with ``Cruxible.connect()``. Reading: ``cx.orient()`` maps what exists,
    ``cx.query(kind, ...)`` answers questions over it, ``cx.get(ref)`` opens one thing, and the
    exported floor under ``.cruxible/floor/current/`` is plain files to grep. ``cx.world()``
    hands back the vocabulary as objects; ``cx.world().describe()`` lists every verb and field.
    Writing: ``cx.set``, ``cx.retire`` and ``cx.changes(because=...)``; authoring anything else:
    ``cx.changes(rationale=...)``. Next: ``cx.next(expiring_within=...)`` for what needs
    attention.
    """

    def __init__(
        self,
        *,
        client: CruxibleClient,
        instance_id: str,
        workspace: Path | None,
        access_profile: AccessProfile,
        clock: Any,
    ) -> None:
        self._client = client
        self._instance_id = instance_id
        # None: a connection with no workspace. Reads serve; orient reports no
        # floor; the members that read or write workspace files refuse.
        self._workspace = None if workspace is None else workspace.expanduser().resolve()
        self._workspace_sources: WorkspaceSources | None = None
        self._access_profile = access_profile
        self._clock = clock
        self._coordinate: AcceptedCoordinate | None = None
        self._pinned = False
        self._owns_client = True
        # An accepted ClaimType at an exact coordinate never changes, so one read
        # answers every Claim this connection drafts under that predicate there.
        self._claim_type_envelopes: dict[tuple[str, str], Mapping[str, object]] = {}

    @classmethod
    def connect(
        cls,
        *,
        context: str | Path | None = None,
        target: str | None = None,
        instance: str | None = None,
        token: SecretStr | None = None,
        principal_id: str | None = None,
        workspace: Path | None = None,
        access_profile: AccessProfile | None = None,
        at: AcceptedCoordinate | api.AcceptedCoordinate | None = None,
    ) -> Cruxible:
        """Open a connection to one instance on a daemon.

        The context file fills whatever is not passed. ``target`` is an ``http(s)://`` URL
        or ``unix:<socket>``; ``instance`` the instance ID; ``token`` the bearer credential
        (default ``CRUXIBLE_SERVER_BEARER_TOKEN``); ``principal_id`` the principal of an
        auth-off daemon. ``at`` pins every read to one accepted coordinate; without it the
        connection is live and reads current head. One identity read names the head; nothing
        else is read. Use it as a context manager to close it. Next: ``cx.orient()`` to see
        what exists.
        """

        context_path = (
            Path(context).expanduser().resolve()
            if context is not None
            else Path(
                os.environ.get(
                    "CRUXIBLE_CLI_CONTEXT_PATH",
                    str(Path.home() / ".cruxible" / "client-context.json"),
                )
            )
            .expanduser()
            .resolve()
        )
        remembered: dict[str, object] = {}
        if context_path.is_file():
            loaded = json.loads(context_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Cruxible context must contain a JSON object")
            remembered = loaded
        explicit_url = target
        explicit_socket = None
        if target is not None and target.startswith("unix:"):
            explicit_url = None
            explicit_socket = target.removeprefix("unix:")
        resolved = resolve_context(
            server_url=explicit_url,
            server_socket=explicit_socket,
            instance_id=instance,
            workspace=workspace,
            remembered=remembered,
        )
        if resolved.server_url is None and resolved.server_socket is None:
            raise ValueError("Cruxible connection requires a server target")
        if not resolved.instance_id:
            if resolved.instance_transport_mismatch:
                raise ContextResolutionError(resolved.instance_transport_mismatch)
            raise ValueError("Cruxible connection requires an instance")
        raw_token = (
            token.get_secret_value()
            if token is not None
            else os.environ.get("CRUXIBLE_SERVER_BEARER_TOKEN")
        )
        client = CruxibleClient(
            base_url=resolved.server_url,
            socket_path=resolved.server_socket,
            token=raw_token,
            principal_id=(principal_id if principal_id is not None else configured_principal_id()),
        )
        try:
            client_compatibility.check_daemon_compatibility(client)
            result = cls(
                client=client,
                instance_id=resolved.instance_id,
                workspace=resolved.workspace,
                access_profile=access_profile
                or AccessProfile(
                    profile_id="sdk-default",
                    permitted_access_classes=("instance", "public"),
                    disclose_restricted_existence=True,
                ),
                clock=lambda: datetime.now(UTC),
            )
            # A session needs only the current head, not an orientation of the
            # whole accepted world: one identity read names it. Orient
            # explicitly (``orient()`` / ``refresh()``) when the overview is wanted.
            if at is None:
                result._coordinate = AcceptedCoordinate.model_validate(
                    client.whoami(resolved.instance_id).coordinate.model_dump(mode="json")
                )
            else:
                result._coordinate = AcceptedCoordinate.model_validate(at.model_dump(mode="json"))
                result._pinned = True
        except BaseException:
            client.close()
            raise
        return result

    @classmethod
    def _from_client(
        cls,
        client: CruxibleClient,
        *,
        instance_id: str,
        workspace: Path | None,
        access_profile: AccessProfile | None = None,
        clock: Any = None,
    ) -> Cruxible:
        result = cls(
            client=client,
            instance_id=instance_id,
            workspace=workspace,
            access_profile=access_profile
            or AccessProfile(
                profile_id="sdk-default",
                permitted_access_classes=("instance", "public"),
                disclose_restricted_existence=True,
            ),
            clock=clock or (lambda: datetime.now(UTC)),
        )
        result.refresh()
        return result

    def __repr__(self) -> str:
        # Safe on a half-built connection, and no I/O: what this handle holds.
        coordinate = getattr(self, "_coordinate", None)
        at = "no coordinate yet" if coordinate is None else f"at {coordinate.git_oid[:12]}"
        mode = "pinned" if getattr(self, "_pinned", False) else "live"
        return f"Cruxible({getattr(self, '_instance_id', '?')!r}, {at}, {mode})"

    def close(self) -> None:
        """Close the transport this connection owns.

        A borrowed ``at()`` context leaves it open. Next: ``Cruxible.connect()`` to open
        another.
        """

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Cruxible:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def _workspace_root(self) -> Path:
        """This connection's workspace, or a typed refusal when it was opened without one."""

        if self._workspace is None:
            raise SourceSelectionError(
                f"{SourceSelectionError.code}: this connection has no workspace; "
                "connect with workspace= to read or write workspace files"
            )
        return self._workspace

    @property
    def _sources(self) -> WorkspaceSources:
        """Resolve the workspace source catalog the first time one is needed.

        Reads -- orientation, search, `world()` -- touch no working tree, so
        demanding a catalog at connect made every read depend on a writer's
        setup. The refusal is unchanged; it now lands on the first surface that
        actually selects from the workspace.
        """

        if self._workspace_sources is None:
            self._workspace_sources = WorkspaceSources(self._workspace_root)
        return self._workspace_sources

    @property
    def coordinate(self) -> AcceptedCoordinate:
        """The pinned coordinate, or the live client's last observed coordinate.

        This property performs no I/O. Live reads resolve current head in their
        own request, so this value is not a freshness check.

        Next: ``cx.at(cx.coordinate)`` to pin later reads to it, or ``cx.orient()`` to read
        current head.
        """
        if self._coordinate is None:
            raise ValueError("Cruxible has not installed an orientation coordinate")
        return self._coordinate

    def at(self, coordinate: AcceptedCoordinate | api.AcceptedCoordinate) -> Cruxible:
        """Borrow an explicitly pinned reading/authoring context, without I/O.

        Accepted reads, including World construction, stay at this coordinate.
        Writes still undergo current daemon admission and may reject stale input.
        The owning connection must remain open; closing this borrowed context
        does not close its parent's transport. Operational queues remain live.

        Next: any read on the returned context, e.g. ``cx.at(coordinate).get(ref)``.
        """
        result = Cruxible(
            client=self._client,
            instance_id=self._instance_id,
            workspace=self._workspace,
            access_profile=self._access_profile,
            clock=self._clock,
        )
        result._coordinate = AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json"))
        result._pinned = True
        result._owns_client = False
        return result

    def _read_at(
        self, coordinate: AcceptedCoordinate | None = None
    ) -> api.AcceptedCoordinate | None:
        if coordinate is not None:
            if self._pinned:
                self._assert_coordinate(coordinate)
            return _api_coordinate(coordinate)
        return _api_coordinate(self.coordinate) if self._pinned else None

    def _observe_read(
        self,
        coordinate: AcceptedCoordinate,
        *,
        expected: api.AcceptedCoordinate | None,
    ) -> None:
        if expected is not None and coordinate != _coordinate(expected):
            raise ValueError("accepted read returned a different requested coordinate")
        if not self._pinned:
            self._coordinate = coordinate

    @property
    def block(self) -> ProjectionBlocks:
        """Client-only declaration stamps; prose remains wholly agent-owned.

        Next: ``cx.block.sync()`` to check every declared block.
        """

        return ProjectionBlocks(self)

    def _with_exact_text(self, view: ClaimView, coordinate: AcceptedCoordinate) -> ClaimView:
        """An exact-content view with the daemon's text for its value, as get shows it.

        The Claim envelope carries only the content digest. ``get`` with
        ``detail="evidence"`` reads the whole value through the daemon's
        exact-content reader at the same coordinate: the text, or the marker
        that says why it is not text. The digest moves to ``content_digest``.
        """

        if view.object_kind != "exact_content":
            return view
        evidence = self._get(view.claim_id, "evidence", None, coordinate).evidence
        if evidence is None:  # pragma: no cover - evidence always answers a Claim
            return view
        value: object = evidence.value
        if isinstance(value, Mapping) and "exact_content" in value:
            value = ExactContentRef.model_validate(value)
        digest = evidence.content_digest
        return replace(
            view,
            value=value,
            content_digest=digest if digest is not None else cast(str | None, view.value),
        )

    @staticmethod
    def _typed_claim_view(view: api.ClaimViewRecord, identity: str = "") -> ClaimView:
        facts = {
            str(fact.get("schema_id")): fact.get("value")
            for fact in view.facts
            if isinstance(fact, Mapping)
        }
        statement = facts.get("cruxible.claim.statement")
        lifecycle = facts.get("cruxible.claim.lifecycle")
        verdict = facts.get("cruxible.claim.current_verdict")
        if not isinstance(statement, Mapping):
            raise ValueError(f"accepted Claim {identity} carries no statement fact")
        item = statement.get("object")
        object_value: object = None
        object_kind = ""
        if isinstance(item, Mapping):
            object_kind = str(item.get("kind", ""))
            if object_kind == "literal":
                object_value = item.get("value")
            elif object_kind == "subject":
                address = item.get("address")
                object_value = (
                    address.get("artifact_path") if isinstance(address, Mapping) else None
                )
            else:
                object_value = item.get("content_digest")
        state = ""
        if isinstance(lifecycle, Mapping):
            inner = lifecycle.get("lifecycle")
            if isinstance(inner, Mapping):
                state = str(inner.get("state", ""))
        return ClaimView(
            claim_id=str(view.envelope.get("identity", identity)),
            revision=int(view.envelope.get("revision", 0)),
            subject=_address_path(statement.get("subject")),
            predicate=str(statement.get("predicate", "")),
            qualifier=cast(str | None, statement.get("qualifier")),
            role=str(statement.get("role", "")),
            object_kind=object_kind,
            value=object_value,
            lifecycle_state=state,
            verdict=(str(verdict.get("verdict", "")) if isinstance(verdict, Mapping) else ""),
            captures=tuple(
                CaptureRef(
                    capture_digest=account.capture_digest,
                    contract_address=capture_contract_path(
                        account.capture_contract_identity.removeprefix("CaptureContract:")
                    ),
                    coordinate=_coordinate(view.coordinate),
                    citation_role=account.citation_role,
                )
                for account in view.admission_accounts
            ),
        )

    def capture(
        self, capture: str | CaptureRef, *, max_bytes: int = 4 * 1024 * 1024
    ) -> CaptureView:
        """Open retained evidence at this SDK coordinate, without refetching it.

        ``capture`` is a digest, a ``CAP-<12+ hex>`` handle or a ``CaptureRef``. Next:
        ``view.text()`` or ``view.json()`` for the material, or pass ``view.ref`` as
        ``supported_by=``.
        """
        if isinstance(capture, CaptureRef):
            self._assert_coordinate(capture.coordinate)
        result = self._client.read_capture(
            self._instance_id,
            CaptureReadRequest(
                capture_digest=capture.capture_digest
                if isinstance(capture, CaptureRef)
                else capture,
                at=self.coordinate,
                max_bytes=max_bytes,
            ),
        )
        return CaptureView(result=result)

    def predictions(self, claim: str | ClaimVersionReference) -> api.ResolutionContractsResult:
        """List the accepted predictions that test one Claim version, retired ones included.

        ``claim`` is a Claim ID (``CLM-...``); the daemon resolves its version at
        this connection's coordinate. An exact ``ClaimVersionReference`` names one
        version directly. A prediction is stored as a resolution contract.

        Next: ``cx.settle(contract, observation=...)`` once an observation is accepted.
        """
        return self._client.list_predictions(
            self._instance_id,
            request=api.ResolutionContractsRequest(hypothesis=claim, at=self.coordinate),
        )

    def predict(self, contract: ResolutionContract | ResolutionContractInput) -> Prediction:
        """Propose a governed test of an accepted Claim.

        The contract's ``hypothesis`` may be a Claim ID (``ResolutionContractInput``);
        the daemon pins the exact accepted version it resolves to.

        Next: ``prediction.proposal.review()``, then approve and ``activate()`` it.
        """
        result = self._client.predict(self._instance_id, request=PredictRequest(contract=contract))
        view = AuthoringIntentView.model_validate(result.intent)
        return Prediction(
            self,
            contract_identity=result.contract_identity,
            contract_digest=result.contract_digest,
            intent_id=view.intent.intent_id,
            proposal_id=result.proposal_id,
        )

    def settle(
        self,
        prediction: str | ResolutionContractReference,
        *,
        observation: str | ClaimVersionReference,
        trigger_event: TriggerEventReference | None = None,
        terminal_run_id: str | None = None,
        terminal_record_digest: str | None = None,
    ) -> PredictionSettlement:
        """Settle a prediction from an accepted observation, both named by ID.

        ``prediction`` is the contract name or a bound window's ``RSC-...`` id
        (as ``next`` names it); ``observation`` is the settling Claim's ID. The
        daemon resolves the exact contract, window and Claim version. Exact
        references are accepted as the advanced form.

        Next: ``cx.next(expiring_within=...)``; a settled window leaves the queue.
        """
        if (terminal_run_id is None) != (terminal_record_digest is None):
            raise ValueError("terminal settlement requires its run and record digest")
        contract = None if isinstance(prediction, str) else prediction
        route = prediction if isinstance(prediction, str) else prediction.identity.name
        if terminal_run_id is None and isinstance(observation, str):
            request = api.SettleRequest(
                observation=observation, contract=contract, trigger_event=trigger_event
            )
        else:
            evidence = (
                ObservationSettlementEvidence(claim=observation)
                if terminal_run_id is None
                else TerminalSettlementEvidence(
                    claim=observation,
                    run_id=terminal_run_id,
                    terminal_record_digest=cast(str, terminal_record_digest),
                )
            )
            request = api.SettleRequest(
                contract=contract, trigger_event=trigger_event, evidence=evidence
            )
        result = self._client.settle_prediction(self._instance_id, route, request=request)
        outcome = result.resolution.get("settlement_outcome")
        if not isinstance(outcome, bool):
            raise ValueError("settlement response omitted its mechanical outcome")
        return PredictionSettlement(
            prediction_id=result.prediction_id, outcome=outcome, relation=result.relation
        )

    def resume_intent(self, intent_id: str) -> Intent:
        """Read an existing intent after interruption, without submitting it.

        Restores the daemon's latest revision, preflight and observed proposal.
        Python call-site locations are process-local and are not reconstructed.
        Review, approval and activation remain explicit.

        Next: ``intent.status()``, then ``intent.submit()`` if it was never submitted.
        """
        raw = self._client.get_authoring_intent(self._instance_id, intent_id).intent
        preflight = raw.get("last_preflight")
        return Intent(
            self,
            None,
            raw,
            preflight=(
                None
                if preflight is None
                else api.AuthoringPreflightResult.model_validate(preflight)
            ),
            candidate_status=api.CandidateStatusRecord.model_validate(raw["candidate_status"]),
        )

    def proposal(self, proposal_id: str) -> Proposal:
        """Return a handle for an existing proposal without creating or approving it.

        Next: ``proposal.review()``, then ``proposal.approve(...)`` and
        ``proposal.activate()``.
        """
        return Proposal(self, proposal_id)

    def refresh_workspace(
        self,
        *,
        at: AcceptedCoordinate | api.AcceptedCoordinate | None = None,
    ) -> api.FloorRefreshResult:
        """Pull the configured floor, at the accepted head unless ``at`` pins one.

        The pull for setups the daemon does not deliver to (a remote daemon,
        delivery off, MCP library mode). A daemon that delivers this workspace's
        floor is its only writer: over the local socket it writes now (head only);
        over TCP the refresh reports failed rather than writing a second copy.
        Reports the coordinate written, or a failed/not_configured status. Does
        not advance this connection's read coordinate or check projection blocks;
        use block.sync() for that.

        Next: grep ``.cruxible/floor/current/`` for what it wrote.
        """

        coordinate = (
            None
            if at is None
            else api.AcceptedCoordinate.model_validate(at.model_dump(mode="json"))
        )
        return refresh_workspace_floor(
            self._client, self._instance_id, workspace=self._workspace_root, at=coordinate
        )

    def activate(self, proposal_id: str) -> api.ActivationReceipt:
        """Activate one proposal: the daemon settles it, nothing local is written.

        The daemon's floor-refresh trigger delivers the floor to a local
        workspace it serves; elsewhere ``cx.refresh_workspace()`` pulls it.
        The receipt's coordinate becomes this live connection's last observation.
        Subsequent live reads select current head; use at(receipt.accepted_coordinate)
        for exact readback. Explicitly pinned contexts and World snapshots stay fixed.

        Next: ``cx.at(receipt.accepted_coordinate)`` to read exactly what was accepted.
        """

        receipt = self._client.activate_proposal(self._instance_id, proposal_id)
        if receipt.status == "accepted" and receipt.accepted_coordinate is not None:
            self._observe_read(_coordinate(receipt.accepted_coordinate), expected=None)
        return receipt

    def proposals(
        self,
        *,
        status: Literal["open", "settled", "incomplete"] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> api.ProposalList:
        """One page of proposals, newest first; follow ``next_cursor`` while truncated.

        Next: ``cx.get(entry.proposal_id)`` to read one, or ``cx.proposal(id).review()``.
        """

        return self._client.list_proposals(
            self._instance_id, status=status, limit=limit, cursor=cursor
        )

    def upgrade_claim_types(
        self,
        *claim_types: str | ClaimTypeRef,
        revision_evidence: Literal["replace", "accumulate"] = "replace",
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> ClaimTypeUpgradeResult:
        """Propose moving live ClaimTypes to v7 as one reviewed change set.

        Names no ClaimType to move every live one before v7. v7 states what a
        statement-changing revision keeps (``revision_evidence``, default
        ``replace``); every Claim is carried with its backing intact. The change
        set carries every dependent Claim, so it previews by default and
        proposes nothing; commit that preview with ``dry_run=False,
        at=result.coordinate.git_oid``. Approve as usual.

        Next: ``cx.proposal(result.proposal_id).review()`` and approve it.
        """

        for item in claim_types:
            if isinstance(item, ClaimTypeRef):
                self._assert_coordinate(item.coordinate)
        request = ClaimTypeUpgradeRequest(
            claim_types=tuple(_address(item, RefKind.CLAIM_TYPE) for item in claim_types),
            revision_evidence=revision_evidence,
            dry_run=dry_run,
            at=at,
        )
        return self._client.upgrade_claim_types(self._instance_id, request)

    def refresh(self) -> api.Head:
        """Re-read the accepted head (a pinned context re-reads its own coordinate).

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """

        requested = self._read_at() if self._pinned else None
        head = self._client.head(self._instance_id, at=requested)
        self._observe_read(_coordinate(head.coordinate.model_dump(mode="json")), expected=requested)
        self._coordinate = _coordinate(head.coordinate.model_dump(mode="json"))
        return head

    def file(self, path: str | Path) -> FileSelector:
        """Select text from a catalogued workspace file, to cite as evidence.

        Next: ``cx.file(path).anchor("text found once")`` and pass it as ``supported_by=``.
        """

        return self._sources.select(path)

    def subject(
        self,
        *,
        subject: str | SubjectRef,
        pins: Sequence[ArtifactPin],
        lifecycle: ArtifactLifecycle,
    ) -> SubjectDraft:
        """Draft one Subject definition (``kind/id``) with its pins and lifecycle.

        A write that names a missing Subject of a known kind adds it for you; this is for
        defining one explicitly, or alongside other members of a change set. Next:
        ``draft.submit()``, or ``cx.changes(rationale=...).subject(draft)`` to define it
        with other members.
        """

        address = _address(subject, RefKind.SUBJECT)
        if isinstance(subject, SubjectRef):
            self._assert_coordinate(subject.coordinate)
        kind, identifier = _subject_parts(address)
        shell = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=address),
            subject_kind=kind,
            subject_id=identifier,
            pins=tuple(pins),
            lifecycle=lifecycle,
        )
        return SubjectDraft(
            self,
            SubjectAuthoringPayload(subject=shell),
            (),
            _program_stamp(
                "subject",
                {"subject": shell.model_dump(mode="json")},
            ),
            DiagnosticSourceMap(()),
            shell,
        )

    def claim_type(
        self,
        *,
        predicate: str | ClaimTypeRef,
        subject_kinds: Sequence[str],
        object_kind: ClaimObjectKind | str,
        value_schema: dict[str, object] | None,
        object_subject_kinds: Sequence[str],
        cardinality: Cardinality | str,
        permitted_roles: Sequence[ClaimRole | str],
        referent_sensitivity: ReferentSensitivity | str,
        sources: Sequence[str | SourceRef],
        admission_policy: ClaimAdmissionPolicy,
        resolution_policy: ClaimResolutionPolicy,
        pins: Sequence[ArtifactPin],
        evidence_freshness: Duration | None,
        attestation_consequence_policy: ClaimAttestationConsequencePolicy | None = None,
    ) -> ClaimTypeDraft:
        """Draft a ClaimType, reading its accepted definition through get.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """

        kind = _enum(object_kind, ClaimObjectKind, label="claim-type object kind")
        arity = _enum(cardinality, Cardinality, label="claim-type cardinality")
        sensitivity = _enum(
            referent_sensitivity, ReferentSensitivity, label="claim-type referent sensitivity"
        )
        roles = tuple(
            _enum(item, ClaimRole, label="claim-type permitted role") for item in permitted_roles
        )
        name = _address(predicate, RefKind.CLAIM_TYPE)
        if isinstance(predicate, ClaimTypeRef):
            self._assert_coordinate(predicate.coordinate)
        for source in sources:
            if isinstance(source, SourceRef):
                self._assert_coordinate(source.coordinate)
        source_ids = tuple(sorted({_address(item, RefKind.SOURCE) for item in sources}))
        role_values: tuple[ClaimRoleValue, ...] = tuple(role.value for role in roles)
        modes: tuple[tuple[Literal["derivational", "direct"], tuple[ClaimRoleValue, ...]], ...] = (
            ("derivational", tuple(r for r in role_values if r == "derivation")),
            ("direct", tuple(sorted({r for r in role_values if r != "derivation"}))),
        )
        rules = tuple(
            ClaimEvidenceAdmissionRule(
                rule_id=f"source-{source_id}-{admission}",
                claim_roles=rule_roles,
                capture_contracts=(
                    ArtifactRef(
                        role=CAPTURE_CONTRACT_REF_ROLE,
                        target=foreign_source_capture_contract(source_id).identity,
                    ),
                ),
                evidence_kinds=("self_asserted",),
                admission=admission,
                subject_binding="exact_claim_subject",
            )
            for source_id in source_ids
            for admission, rule_roles in modes
            if rule_roles
        )
        lifecycle = ArtifactLifecycle()
        # A new ClaimType replaces evidence on revision; a successor keeps what
        # its predecessor means, exactly as the daemon's lowering does.
        evidence_requirement: EvidenceRequirement = "self"
        revision_evidence: RevisionEvidence = "replace"
        if isinstance(predicate, ClaimTypeRef):
            predecessor = self._get(f"ClaimType:{name}", "proof", None, predicate.coordinate)
            assert predecessor.proof is not None
            lifecycle = ArtifactLifecycle(
                predecessor_digest=str(predecessor.proof["artifact_digest"])
            )
            envelope = predecessor.proof.get("envelope")
            if isinstance(envelope, Mapping):
                accepted = ClaimType.model_validate(envelope)
                evidence_requirement = effective_evidence_requirement(accepted)
                revision_evidence = effective_revision_evidence(accepted)
        definition = ClaimType(
            artifact_format="playbill-claim-type-v7",
            identity=ArtifactIdentity(kind="ClaimType", name=name),
            predicate=name,
            allowed_subject_kinds=tuple(subject_kinds),
            object_kind=kind.value,
            literal_schema=value_schema,
            allowed_object_subject_kinds=tuple(object_subject_kinds),
            cardinality=arity.value,
            permitted_roles=tuple(role.value for role in roles),
            referent_sensitivity=sensitivity.value,
            evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=rules),
            admission_policy=admission_policy,
            resolution_policy=resolution_policy,
            pins=tuple(pins),
            lifecycle=lifecycle,
            evidence_freshness=(
                None
                if evidence_freshness is None
                else ClaimEvidenceFreshness(
                    stale_after=ClaimFreshnessDuration(microseconds=evidence_freshness.value)
                )
            ),
            attestation_consequence_policy=attestation_consequence_policy,
            evidence_requirement=evidence_requirement,
            revision_evidence=revision_evidence,
        )
        return ClaimTypeDraft(self, definition)

    def world(self) -> World:
        """Read this instance's accepted vocabulary as typed objects.

        Strings are the one place the SDK gave away what it knows. The daemon
        already publishes the accepted ClaimTypes, so this reads them once and
        hands back a tree of kinds, predicates and admissible values, every ref
        stamped with the coordinate selected for this World.

        A live client's vocabulary listing selects current head. An explicitly
        pinned context uses its coordinate. The returned World owns a separate
        pinned context and remains readable when the live client moves, without
        fetching an orientation page. No Subject is read here. The first Subject access of any kind
        reads every Subject of every kind in one list, because the served verb
        takes neither a kind filter nor a cursor; a world with a thousand
        Subjects therefore costs the vocabulary at `world()` and that one list
        the first time any Subject is named.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """

        from cruxible_client.authoring.world import build_world

        requested = self._read_at()
        names: list[str] = []
        cursor: str | None = None
        coordinate: AcceptedCoordinate | None = None
        while True:
            page = self._client.orient(
                self._instance_id,
                section="claim_types",
                limit=api.ORIENT_MAX_LIMIT,
                cursor=cursor,
                at=requested if coordinate is None else _api_coordinate(coordinate),
                surface="sdk",
            )
            coordinate = _coordinate(page.coordinate.model_dump(mode="json"))
            names.extend(f"ClaimType:{item.predicate}" for item in page.claim_types or ())
            if not page.truncated or page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert coordinate is not None
        self._observe_read(coordinate, expected=requested)
        envelopes: list[Mapping[str, object]] = []
        for start in range(0, len(names), GET_BATCH_MAX_REFS):
            batch = self._client.get_batch(
                self._instance_id,
                request=GetBatchRequest(
                    refs=tuple(names[start : start + GET_BATCH_MAX_REFS]),
                    at=_api_coordinate(coordinate),
                    evaluation_time=datetime.fromisoformat(self._evaluation_time()),
                ),
            )
            if _coordinate(batch.coordinate) != coordinate:
                raise ValueError("the ClaimType read returned a different accepted coordinate")
            envelopes.extend(
                cast(Mapping[str, object], (result.proof or {})["envelope"])
                for result in batch.results
            )
        return build_world(
            self.at(coordinate),
            coordinate=coordinate,
            claim_type_envelopes=tuple(envelopes),
        )

    @overload
    def changes(self, *, because: str, subject: str | SubjectRef | None = None) -> WriteBatch: ...

    @overload
    def changes(self, *, rationale: str | None = None) -> ChangeSetDraft: ...

    def changes(
        self,
        *,
        rationale: str | None = None,
        because: str | None = None,
        subject: str | SubjectRef | None = None,
    ) -> ChangeSetDraft | WriteBatch:
        """Open one changeset that any mix of members can be authored into.

        ``changes(because=...)`` opens the typed write batch instead:
        ``.set(...)``, ``.add(...)`` and ``.retire(...)`` changes, sent together by
        ``.write()``; ``subject=`` names the Subject of every change that names
        none. ``changes(rationale=...)`` is the full authoring changeset.

        `cx.claim(...)` still authors exactly one Claim. This is the same
        authoring surface for an intent that carries more than one: it lowers
        once, proposes once, and admits or refuses whole. The draft retains the
        last observed coordinate for its vocabulary lookups and typed references;
        current daemon admission still checks whether its inputs are stale.

        Next: ``.set(...)``/``.add(...)`` then ``.write()`` on a write batch;
        ``.claim(...)`` and friends then ``.submit()`` on a changeset.
        """

        if because is not None:
            if rationale is not None:
                raise ValueError(
                    "pass because (a write batch) or rationale (a changeset), not both"
                )
            return WriteBatch(self, because=because, subject=subject)
        if subject is not None:
            raise ValueError("subject= names the default Subject of a write batch (because=)")
        return ChangeSetDraft(self.at(self.coordinate), rationale)

    # -- the write verbs -------------------------------------------------------

    def _write_at(self, at: WriteAt | _Unset) -> api.AcceptedCoordinate | str | None:
        """The read coordinate a write names: by default this context's own."""

        if isinstance(at, _Unset):
            return None if self._coordinate is None else _api_coordinate(self.coordinate)
        if at is None or isinstance(at, str):
            return at
        return api.AcceptedCoordinate.model_validate(at.model_dump(mode="json"))

    def _write(self, request: WriteRequest) -> WriteOutcome:
        """Send one write and answer its outcome, or raise its refusal."""

        request = request.model_copy(
            update={"changes": observe_changes(request.changes, workspace=self._workspace_root)}
        )
        outcome = self._client.write(self._instance_id, request=request)
        return self._written(outcome)

    def _written(self, outcome: WriteOutcome) -> WriteOutcome:
        pinned = outcome.accepted_coordinate
        if outcome.status == "accepted" and pinned is not None:
            self._observe_read(_coordinate(pinned), expected=None)
        if not outcome.refused:
            return outcome
        refusal = outcome.refusal
        if (
            refusal is not None
            and normalize_code(refusal.code) == "cruxible.write.slot_changed"
            and pinned
        ):
            # The refusal showed the value the slot holds now; setting again
            # replaces that value, which is its repair.
            self._observe_read(_coordinate(pinned), expected=None)
        raise WriteRefusalError(
            "cruxible.write.refused" if refusal is None else refusal.code,
            "the write refused" if refusal is None else refusal.message,
            change=None if refusal is None else refusal.change,
            candidates=() if refusal is None else refusal.candidates,
            repair_line=None if refusal is None else refusal.repair,
            field_path=None if refusal is None else refusal.field_path,
            outcome=outcome,
        )

    def set(
        self,
        subject: str | SubjectRef,
        field: str | ClaimTypeRef,
        value: ClaimValue | SubjectRef | LiteralValue,
        *,
        because: str,
        evidence: Evidence | None = None,
        role: WriteRole | None = None,
        contend: bool = False,
        expect: WriteExpect | None = None,
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
        at: WriteAt | _Unset = _UNSET,
    ) -> WriteOutcome:
        """Put one value in one field of one Subject, replacing the live value.

        The Claim it replaces is found for you, and a missing Subject of a known
        kind is added. It accepts in the same call when policy lets you
        (``accept="never"`` only proposes); ``dry_run`` checks everything and
        writes nothing. By default it refuses when the field changed since this
        context's coordinate; ``expect`` compares by value instead: it refuses
        unless the field holds that value now (a list for several, ``[]`` for
        none). A refusal raises ``WriteRefusalError``; check
        ``outcome.warnings`` for a verdict that is not supported.

        Next: ``outcome.next`` names what is still needed; ``cx.get(f"{subject}")`` reads
        the value back with its verdict.
        """

        request = SetRequest(
            subject=_write_subject(subject),
            field=_write_field(field),
            value=_write_value(value),
            because=because,
            evidence=evidence,
            role=role,
            contend=contend,
            expect=_write_expect(expect),
            dry_run=dry_run,
            accept=accept,
            at=self._write_at(at),
            surface="sdk",
            full_coordinate=True,
        )
        if request.evidence is not None:
            (change,) = observe_changes((request.change(),), workspace=self._workspace_root)
            request = request.model_copy(update={"evidence": cast(SetChange, change).evidence})
        return self._written(self._client.set(self._instance_id, request=request))

    def retire(
        self,
        target: str | ClaimRef | SlotRef,
        *,
        because: str,
        reason: WriteRetireReason = "was-rescinded",
        expect: WriteExpect | None = None,
        dry_run: bool = False,
        accept: WriteAccept = "if_allowed",
        at: WriteAt | _Unset = _UNSET,
    ) -> WriteOutcome:
        """End one live Claim, named by ID or ``SlotRef(subject=..., field=...)``.

        Claims that depend on it retire with it, in one change set. ``expect``
        refuses unless its field holds that value now (every live value, as a
        list, for a many-valued field).

        Next: ``outcome.next`` names what is still needed; ``cx.get(ref, detail="history")``
        shows the retirement.
        """

        request = RetireRequest(
            target=_write_target(target),
            because=because,
            reason=reason,
            expect=_write_expect(expect),
            dry_run=dry_run,
            accept=accept,
            at=self._write_at(at),
            surface="sdk",
            full_coordinate=True,
        )
        return self._written(self._client.retire(self._instance_id, request=request))

    def claim(
        self,
        *,
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None = None,
        copied_from: EvidenceSelection | CaptureRef | None = None,
        self_source: str | None = None,
        qualifier: str | None = None,
        effective_period: EffectivePeriod | None = None,
        revises: str | ClaimRef | None = None,
        dispositions: Mapping[str | ClaimRef, Disposition | str] | None = None,
        subject_definition: SubjectDraft | None = None,
        claim_type_definition: ClaimTypeDraft | None = None,
    ) -> ClaimDraft:
        """Author exactly one Claim, as a draft to submit.

        The write verbs (``cx.set``, ``cx.changes(because=...)``) cover the common case and
        infer the role, revision and evidence; this is the full authoring surface: role,
        rationale, qualifiers, effective periods, explicit revisions, dispositions and
        Subject or ClaimType definitions in the same intent. Evidence is ``supported_by=``
        (a ``cx.file(...).anchor(...)`` selection or a ``CaptureRef``), ``copied_from=``, or
        ``self_source=`` text. Next: ``draft.submit()`` (or ``draft.prepare()`` to preflight
        first), then ``intent.proposal.review()``.
        """

        return self._claim_draft(
            sites=capture_keyword_sites("claim", stacklevel=1),
            subject=subject,
            predicate=predicate,
            value=value,
            role=role,
            rationale=rationale,
            supported_by=supported_by,
            copied_from=copied_from,
            self_source=self_source,
            qualifier=qualifier,
            effective_period=effective_period,
            revises=revises,
            dispositions={} if dispositions is None else dispositions,
            subject_definition=subject_definition,
            claim_type_definition=claim_type_definition,
        )

    def _claim_draft(
        self,
        *,
        sites: dict[str, CallSite],
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None,
        copied_from: EvidenceSelection | CaptureRef | None,
        self_source: str | None,
        qualifier: str | None,
        effective_period: EffectivePeriod | None,
        revises: str | ClaimRef | None,
        dispositions: Mapping[str | ClaimRef, Disposition | str],
        subject_definition: SubjectDraft | None,
        claim_type_definition: ClaimTypeDraft | None,
        staged_claim_types: Mapping[str, ClaimType] | None = None,
    ) -> ClaimDraft:
        """Build one authored Claim from decisions plus its caller's call sites.

        The call sites arrive as an argument so a Claim authored inside
        `cx.changes(...)` still points its diagnostics at the author's own
        keyword, not at the builder that forwarded it.
        """

        claim_role = _enum(role, ClaimRole, label="claim role")
        resolved_dispositions: dict[str, Disposition] = {}
        original_dispositions: dict[str, str | ClaimRef] = {}
        for key, disposition_value in dispositions.items():
            claim_id = _claim_id(key)
            if claim_id in resolved_dispositions:
                raise ValueError(f"duplicate normalized Claim disposition: {claim_id}")
            resolved_dispositions[claim_id] = _enum(
                disposition_value, Disposition, label="claim disposition"
            )
            original_dispositions[claim_id] = key
        branches = tuple(item is not None for item in (supported_by, copied_from, self_source))
        if sum(branches) != 1:
            raise ValueError("exactly one of supported_by, copied_from, or self_source is required")
        subject_name = _address(subject, RefKind.SUBJECT)
        if isinstance(subject, str):
            # PC-HR moved accepted artifacts to .json without retiring the
            # pre-PC-HR .yaml authoring shorthand.
            if subject_name.endswith(".json"):
                subject_name = subject_name.removesuffix(".json")
            elif subject_name.endswith(".yaml"):
                subject_name = subject_name.removesuffix(".yaml")
        predicate_name = _address(predicate, RefKind.CLAIM_TYPE)
        statement_object: LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObject
        if isinstance(value, ExactContent):
            # Exact bytes name their own kind, so the only question left is
            # whether the predicate states one. Asking here turns a shape the
            # daemon would refuse into a refusal that names the predicate and
            # the kind it does state, before anything is written.
            object_kind = self._claim_type_object_kind(
                predicate_name=predicate_name,
                predicate=predicate,
                claim_type_definition=claim_type_definition,
                staged_claim_types=staged_claim_types,
            )
            if object_kind != "exact_content":
                raise ExactContentTypeError(
                    predicate=predicate_name,
                    object_kind=object_kind,
                )
            statement_object = AuthoringExactContentObject(
                content_base64=base64.b64encode(value.content).decode("ascii")
            )
        elif isinstance(value, LiteralValue):
            # A typed literal already names the ClaimType that admits it, so it
            # answers the object kind without a read and refuses the wrong
            # predicate here rather than in the daemon's preflight.
            if value.predicate != predicate_name:
                raise LiteralValueTypeError(
                    minted_under=value.predicate,
                    passed_to=predicate_name,
                )
            self._assert_coordinate(value.coordinate)
            statement_object = LiteralClaimObject(value=normalize_canonical(value.value))
        elif isinstance(value, SubjectRef):
            self._assert_coordinate(value.coordinate)
            statement_object = SubjectClaimObject(address=_subject_address(value.address))
        elif isinstance(value, str) and _SUBJECT_RE.fullmatch(value):
            object_kind = self._claim_type_object_kind(
                predicate_name=predicate_name,
                predicate=predicate,
                claim_type_definition=claim_type_definition,
                staged_claim_types=staged_claim_types,
            )
            statement_object = (
                SubjectClaimObject(address=_subject_address(value))
                if object_kind == "subject"
                else LiteralClaimObject(value=value)
            )
        else:
            statement_object = LiteralClaimObject(value=normalize_canonical(value))
        # A role the ClaimType does not permit is refused here, at the keyword that
        # set it, instead of as a proposal-evaluation diagnostic after a round trip.
        permitted_roles = self._claim_type_permitted_roles(
            predicate_name=predicate_name,
            predicate=predicate,
            claim_type_definition=claim_type_definition,
            staged_claim_types=staged_claim_types,
        )
        if permitted_roles is not None and claim_role.value not in permitted_roles:
            raise ClaimRoleNotPermittedError(
                predicate=predicate_name,
                role=claim_role.value,
                permitted_roles=permitted_roles,
                call_site=sites.get("role"),
            )
        source: Any
        if supported_by is not None:
            if isinstance(supported_by, CaptureRef):
                self._assert_coordinate(supported_by.coordinate)
                if supported_by.citation_role != "evidence":
                    raise ValueError(
                        "a CaptureRef minted from a copy or legacy citation cannot be "
                        "promoted to independent evidence; reuse it with copied_from"
                    )
                source = ExistingCaptureCitationSource(capture_digest=supported_by.capture_digest)
            else:
                assert_independent_projection_evidence(
                    source_id=supported_by.source_id,
                    content=supported_by.content,
                    start_byte=supported_by.start_byte,
                    end_byte=supported_by.end_byte,
                )
                source = supported_by.observation()
            citation_role: Literal["evidence", "copy"] | None = "evidence"
        elif copied_from is not None:
            if isinstance(copied_from, CaptureRef):
                self._assert_coordinate(copied_from.coordinate)
                source = ExistingCaptureCitationSource(capture_digest=copied_from.capture_digest)
            else:
                # A copy of projection bytes attests them into concrete exactly
                # as evidence would; the role changes nothing about the law.
                assert_independent_projection_evidence(
                    source_id=copied_from.source_id,
                    content=copied_from.content,
                    start_byte=copied_from.start_byte,
                    end_byte=copied_from.end_byte,
                )
                source = copied_from.observation()
            citation_role = "copy"
        else:
            assert self_source is not None
            source = SelfSourceBody(
                content_base64=base64.b64encode(self_source.encode("utf-8")).decode("ascii")
            )
            citation_role = None
        sorted_dispositions = tuple(
            sorted(
                resolved_dispositions.items(),
                key=lambda item: item[0].encode("ascii"),
            )
        )
        payload_values = dict(
            statement=AuthoringClaimStatement(
                subject=_subject_address(subject_name),
                predicate=predicate_name,
                qualifier=qualifier,
                object=statement_object,
                role=claim_role.value,
                effective_from=(None if effective_period is None else effective_period.starts_at),
                effective_until=(None if effective_period is None else effective_period.ends_at),
            ),
            rationale=rationale,
            source=source,
            citation_role=citation_role,
            revises=(None if revises is None else _claim_id(revises)),
            existing_claim_dispositions=tuple(
                AuthoringExistingClaimDisposition(claim_id=claim_id, disposition=disposition.value)
                for claim_id, disposition in sorted_dispositions
            ),
            dependency_drafts=ClaimDependencyDrafts(
                subject=None if subject_definition is None else subject_definition.shell,
                claim_type=(
                    None if claim_type_definition is None else claim_type_definition.definition
                ),
            ),
        )
        payload = (
            ClaimAuthoringPayload(**payload_values)
            if isinstance(source, ExistingCaptureCitationSource)
            else ClaimAuthoringPayloadV2(**payload_values)
        )
        expectations: list[AuthoringReferenceExpectation | None] = [
            _expectation(
                subject,
                expected=RefKind.SUBJECT,
                payload_path="statement.subject",
            ),
            _expectation(
                predicate,
                expected=RefKind.CLAIM_TYPE,
                payload_path="statement.predicate",
            ),
        ]
        if isinstance(value, SubjectRef):
            expectations.append(
                _expectation(
                    value,
                    expected=RefKind.SUBJECT,
                    payload_path="statement.object.address",
                )
            )
        if revises is not None:
            expectations.append(
                _expectation(revises, expected=RefKind.CLAIM, payload_path="revises")
            )
        capture_ref = (
            supported_by
            if isinstance(supported_by, CaptureRef)
            else copied_from
            if isinstance(copied_from, CaptureRef)
            else None
        )
        if capture_ref is not None:
            expectations.append(
                AuthoringReferenceExpectation(
                    payload_path="source",
                    artifact_kind="Source",
                    address=capture_ref.contract_address,
                    minted_coordinate=capture_ref.coordinate,
                )
            )
        for index, (raw_key, _value) in enumerate(sorted_dispositions):
            original = original_dispositions[raw_key]
            expectations.append(
                _expectation(
                    original,
                    expected=RefKind.CLAIM,
                    payload_path=f"existing_claim_dispositions[{index}].claim_id",
                )
            )
        emitted = {
            "subject": ("statement.subject",),
            "predicate": ("statement.predicate",),
            "value": (
                "statement.object",
                (
                    "statement.object.address"
                    if isinstance(statement_object, SubjectClaimObject)
                    else "statement.object.content_base64"
                    if isinstance(statement_object, AuthoringExactContentObject)
                    else "statement.object.value"
                ),
            ),
            "role": ("statement.role",),
            "rationale": ("rationale",),
            "supported_by": ("source",),
            "copied_from": ("source",),
            "self_source": ("source",),
            "qualifier": ("statement.qualifier",),
            "effective_period": ("statement.effective_from", "statement.effective_until"),
            "revises": ("revises",),
            "dispositions": ("existing_claim_dispositions",),
            "subject_definition": ("dependency_drafts.subject",),
            "claim_type_definition": ("dependency_drafts.claim_type",),
        }
        decisions = {
            "subject": subject_name,
            "predicate": predicate_name,
            "value": (
                statement_object.address.model_dump(mode="json")
                if isinstance(statement_object, SubjectClaimObject)
                # The program stamp records the decision, not the body: exact
                # content is already stored and digested by the daemon, and a
                # second copy of it here would put the same bytes in the stamp.
                else statement_object.content_base64
                if isinstance(statement_object, AuthoringExactContentObject)
                else statement_object.value
            ),
            "role": claim_role.value,
            "rationale": rationale,
            "source_branch": (
                "supported_by"
                if supported_by is not None
                else "copied_from"
                if copied_from is not None
                else "self_source"
            ),
            "source_id": (
                supported_by.source_id
                if isinstance(supported_by, EvidenceSelection)
                else copied_from.source_id
                if isinstance(copied_from, EvidenceSelection)
                else None
            ),
            "capture_digest": None if capture_ref is None else capture_ref.capture_digest,
            "self_source": self_source,
            "qualifier": qualifier,
            "effective_period": (
                None
                if effective_period is None
                else {
                    "starts_at": format_datetime(effective_period.starts_at),
                    "ends_at": format_datetime(effective_period.ends_at),
                }
            ),
            "revises": None if revises is None else _claim_id(revises),
            "dispositions": {
                identity: disposition.value for identity, disposition in sorted_dispositions
            },
            "dependency_drafts": payload.dependency_drafts.model_dump(mode="json"),
        }
        return ClaimDraft(
            self,
            payload,
            _sorted_expectations(expectations),
            _program_stamp("claim", decisions),
            DiagnosticSourceMap(
                entries_for_keywords(builder="claim", emitted=emitted, sites=sites)
            ),
        )

    def _accepted_claim_type_envelope(
        self, predicate_name: str, predicate: str | ClaimTypeRef
    ) -> Mapping[str, object]:
        """Read one accepted ClaimType at the draft's coordinate, once per connection."""

        coordinate = (
            predicate.coordinate if isinstance(predicate, ClaimTypeRef) else self.coordinate
        )
        if isinstance(predicate, ClaimTypeRef):
            self._assert_coordinate(coordinate)
        key = (predicate_name, coordinate.git_oid)
        envelope = self._claim_type_envelopes.get(key)
        if envelope is None:
            proof = self._get(f"ClaimType:{predicate_name}", "proof", None, coordinate).proof
            assert proof is not None
            envelope = proof["envelope"]
            self._claim_type_envelopes[key] = envelope
        return envelope

    def _claim_type_permitted_roles(
        self,
        *,
        predicate_name: str,
        predicate: str | ClaimTypeRef,
        claim_type_definition: ClaimTypeDraft | None,
        staged_claim_types: Mapping[str, ClaimType] | None,
    ) -> tuple[str, ...] | None:
        """The roles the exact ClaimType permits, or None when only the daemon can say.

        A definition this Claim carries, a World ref, or a definition its set
        stages answers without a read.
        Otherwise the accepted ClaimType is read; a predicate that does not
        resolve is left to preflight, whose typed refusal names it.
        """

        if claim_type_definition is not None:
            return tuple(claim_type_definition.definition.permitted_roles)
        # A World ClaimType ref already carries the structure it was read with.
        world_roles = getattr(predicate, "permitted_roles", None)
        if isinstance(predicate, ClaimTypeRef) and isinstance(world_roles, tuple):
            return tuple(str(getattr(role, "value", role)) for role in world_roles)
        staged = (staged_claim_types or {}).get(predicate_name)
        if staged is not None:
            return tuple(staged.permitted_roles)
        try:
            roles = self._accepted_claim_type_envelope(predicate_name, predicate).get(
                "permitted_roles"
            )
        except (CoreError, ValueError):
            return None
        if not isinstance(roles, list | tuple) or not all(isinstance(r, str) for r in roles):
            return None
        return tuple(roles)

    def _claim_type_object_kind(
        self,
        *,
        predicate_name: str,
        predicate: str | ClaimTypeRef,
        claim_type_definition: ClaimTypeDraft | None,
        staged_claim_types: Mapping[str, ClaimType] | None = None,
    ) -> Literal["literal", "subject", "exact_content"]:
        """Resolve the exact ClaimType before interpreting an untyped object.

        Three answers a change set already holds, tried before the accepted
        coordinate is read: the definition this Claim carries as a dependency
        draft, the ref a same-set definition returned, and the definitions the
        same set staged under other names. Only then is there anything to ask
        the daemon, and in a first generation there is no coordinate to ask at.
        """

        if claim_type_definition is not None:
            return claim_type_definition.definition.object_kind
        if isinstance(predicate, PendingClaimTypeRef):
            if predicate.object_kind not in _CLAIM_TYPE_OBJECT_KINDS:
                return "literal"
            return cast(
                Literal["literal", "subject", "exact_content"],
                predicate.object_kind,
            )
        staged = (staged_claim_types or {}).get(predicate_name)
        if staged is not None and staged.object_kind in _CLAIM_TYPE_OBJECT_KINDS:
            return staged.object_kind
        object_kind = self._accepted_claim_type_envelope(predicate_name, predicate).get(
            "object_kind"
        )
        if object_kind not in _CLAIM_TYPE_OBJECT_KINDS:
            # An envelope kind this client does not know is daemon/client skew,
            # not a caller mistake. Falling back to the literal shape keeps the
            # daemon's preflight the single authority: it answers with the typed
            # `playbill.claim.object_kind_mismatch` refusal and its repair,
            # instead of the SDK raising an untyped, repair-less ValueError.
            return "literal"
        return cast(Literal["literal", "subject", "exact_content"], object_kind)

    def query_definition(
        self,
        *,
        definition: QueryDefinitionInput,
        vocabulary: Sequence[ClaimTypeRef] = (),
    ) -> QueryDraft:
        """Draft a named read through the shared authoring coordinator.

        No exact pins are needed. Optional World/vocabulary references assert
        that their versions still match the intent base; same-set refs resolve
        after sibling definitions. Both ontology and relationship queries use
        this path, including declarative CLI/MCP inputs.

        Next: ``draft.submit()``; once accepted, ``cx.query(name=..., params={...})`` runs
        it.
        """
        payload = lower_authoring_input(definition)
        assert isinstance(payload, QueryDefinitionAuthoringPayload)
        expectations = []

        def visit(value: object, path: str, ref: ClaimTypeRef) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    next_path = f"{path}.{key}"
                    if key == "predicate" and child == ref.address:
                        expectations.append(
                            _expectation(ref, expected=RefKind.CLAIM_TYPE, payload_path=next_path)
                        )
                    else:
                        visit(child, next_path, ref)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]", ref)

        for ref in vocabulary:
            if ref.address not in definition.query_definition.referenced_predicates:
                raise ValueError("vocabulary reference is not used by this query")
            visit(definition.query_definition.model_dump(mode="json"), "query_definition", ref)
        return QueryDraft(
            self,
            payload,
            _sorted_expectations(expectations),
            _program_stamp("query_definition", definition.model_dump(mode="json")),
            DiagnosticSourceMap(()),
        )

    def provider_binding(self, interface: str, *, provider: str | None = None) -> ProviderBinding:
        """Select a registered interface through existing accepted-state discovery.

        Discovery never installs a provider or authorizes its execution. Multiple
        implementations require an explicit selection rather than an arbitrary default.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """
        identity = (
            interface
            if interface.startswith("ProviderInterface:")
            else "ProviderInterface:" + interface
        )
        read = self._get(identity, "proof", None, None)
        if read.proof is None or read.accepted_coordinate is None:
            raise ValueError(f"No accepted provider interface {identity!r}")
        entry = api.ProviderInterfaceEntry.model_validate(read.proof["entry"])
        return ProviderBinding.from_interface(entry, provider=provider).model_copy(
            update={
                "coordinate": AcceptedCoordinate.model_validate(
                    read.accepted_coordinate.model_dump(mode="json")
                )
            },
            deep=True,
        )

    def procedure(
        self,
        *,
        definition: ProcedureInput | ProcedureSequence | ProcedureBlueprint,
    ) -> ProcedureDraft:
        """Author a Procedure from a Sequence or the input shared by CLI and HTTP.

        A Sequence first performs its local structural checks and builds that
        same input. Its preview never runs providers or replaces daemon preflight.

        Declare owned input/output schemas in ``definition.contracts`` and use
        ``carried_contract`` references in its graph. ``accepted`` references
        resolve at the intent base; ``slot`` references remain deferred. Exact
        pins belong to accepted graphs and are never silently converted into
        references to a potentially different version.

        Activation, retirement, and the optional acquisition-policy name are
        also carried by this input. The daemon resolves the policy at the same
        base as the graph's other dependencies.

        Next: ``draft.submit()``, then ``cx.accepted_procedure(name).run(...)`` once
        accepted.
        """

        sites = capture_keyword_sites("procedure", stacklevel=1)
        if isinstance(definition, ProcedureBlueprint):
            definition = definition.build(world=self.world())
        if isinstance(definition, ProcedureSequence):
            definition = definition.build()
        if not isinstance(definition, ProcedureInput):
            raise TypeError("procedure definition must be a ProcedureInput or authoring Sequence")
        payload = lower_authoring_input(definition)
        assert isinstance(payload, (ProcedureAuthoringPayloadV1, ProcedureAuthoringPayload))
        # `source` is served by the graph-v4/v5 observation path: a v3 Source
        # node names no interface or implementation, so nothing can plan its
        # Provider occurrence. Keep it out of the v3 allow-list rather than
        # letting authoring succeed on a graph no run lane can admit.
        allowed = {"state_tap", "transform", "project", "guard", "repeat", "halt"}
        if definition.definition.get("graph_format") in {4, 5}:
            # Effectful terminals are served on the Line lane: direct runs
            # refuse them at admission. The shared compiler enforces that each
            # terminal ends its path; the SDK must allow authoring that path.
            allowed = allowed | {
                "source",
                "emit_capture",
                "propose_change_set",
                "settle_change_set",
            }
        if definition.definition.get("graph_format") == 5:
            allowed = allowed | {"call"}
        nodes = definition.definition.get("nodes")
        if "source_request" in definition.definition:
            from cruxible_client.contracts.procedures.source_requests import (
                ProcedureSourceRequest,
            )

            ProcedureSourceRequest.model_validate(definition.definition["source_request"])
            nodes = ()
        if not isinstance(nodes, list | tuple):
            raise ValueError("Procedure input must declare its nodes")
        unsupported = tuple(
            node.get("node_id")
            for node in nodes
            if isinstance(node, Mapping) and node.get("kind") not in allowed
        )
        if unsupported:
            raise CapabilityNotServed(
                code="cruxible.sdk.procedure_capability_not_served",
                capability=f"procedure nodes {unsupported}",
                repair=(
                    "Use only state_tap, transform, project, guard, repeat, and halt nodes "
                    "on the served SDK lane, plus source, emit_capture, propose_change_set, "
                    "and settle_change_set on a graph-v4/v5 definition, and call on a graph-v5 "
                    "definition."
                ),
            )
        return ProcedureDraft(
            self,
            payload,
            (),
            _program_stamp(
                "procedure",
                definition.model_dump(mode="json"),
            ),
            DiagnosticSourceMap(
                entries_for_keywords(
                    builder="procedure",
                    emitted={
                        "definition": (
                            "definition",
                            "owned_contracts",
                            "activation_policy",
                            "acquisition_policy",
                            "retire",
                        ),
                    },
                    sites=sites,
                )
            ),
        )

    def query(
        self,
        kind: str | None = None,
        *,
        where: Sequence[QueryFilter | Mapping[str, object]] | None = None,
        contains: str | None = None,
        select: Sequence[str] | None = None,
        follow: Sequence[
            QueryFollow
            | Mapping[str, str]
            | tuple[str, str]
            | tuple[str, str, QueryFollowDirection]
        ]
        | None = None,
        order_by: Sequence[str] | None = None,
        limit: int = api.QUERY_DEFAULT_LIMIT,
        cursor: str | None = None,
        spec: QueryDefinitionSpec | None = None,
        name: str | QueryRef | None = None,
        params: Mapping[str, object] | None = None,
        at: AcceptedCoordinate | str | None = None,
        evaluation_time: datetime | str | None = None,
        status: Sequence[QueryClaimStatus] = ("live",),
        claims: bool = False,
        budgets: QueryBudgets | None = None,
        receipt: QueryReceiptDetail = "compact",
    ) -> QueryResult:
        """Answer any question over accepted state: one page of values with flags.

        Exactly one mode: ``kind`` and/or ``contains`` (compact, with ``where``
        filters such as ``{"field": "adoption_state", "eq": "adopted"}``,
        ``select``, ``follow`` and ``order_by``), a ``spec``, or a query ``name``
        with ``params``. ``next_page()`` continues a truncated answer.

        A follow is ``(field, alias)`` forward along the kind's own predicate, or
        ``(field, alias, "reverse")`` backwards along another kind's predicate
        that points at this kind, e.g. ``("dev.batch.delivers", "batch",
        "reverse")`` from ``dev.roadmap_item``; a mapping or ``QueryFollow``
        with ``direction`` works too.

        Next: ``result.next_page()`` while truncated; ``cx.get(ref)`` on any row's ref or
        Claim ID for its evidence and history.
        """

        follows = [
            dict(zip(("field", "as", "direction"), item, strict=False))
            if isinstance(item, tuple)
            else item
            for item in follow or ()
        ]
        request = api.QueryRequest.model_validate(
            {
                "kind": kind,
                "where": filters_from_mappings(where or ()),
                "contains": contains,
                "select": tuple(select or ()),
                "follow": [
                    item.model_dump(mode="json", by_alias=True)
                    if isinstance(item, QueryFollow)
                    else dict(item)
                    for item in follows
                ],
                "order_by": tuple(order_by or ()),
                "limit": limit,
                "cursor": cursor,
                "spec": spec,
                "name": None if name is None else _address(name, RefKind.QUERY),
                "params": None if params is None else dict(params),
                "status": tuple(status),
                "claims": claims,
                "budgets": budgets,
                "receipt": receipt,
            }
        )
        return self._run_query_request(request, at=at, evaluation_time=evaluation_time)

    def _run_query_request(
        self,
        request: api.QueryRequest,
        *,
        at: AcceptedCoordinate | str | None = None,
        evaluation_time: datetime | str | None = None,
    ) -> QueryResult:
        expected = None if isinstance(at, str) else self._read_at(at)
        # A continuation keeps the instant its first page pinned; only a new
        # query takes the connection clock.
        when = (
            evaluation_time
            if evaluation_time is not None or request.cursor is not None
            else self._evaluation_time()
        )
        prepared = request.model_copy(
            update={
                "at": (
                    at
                    if isinstance(at, str)
                    else None
                    if expected is None
                    else AcceptedCoordinate.model_validate(expected.model_dump(mode="json"))
                ),
                "evaluation_time": (
                    datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if isinstance(when, str)
                    else when
                ),
            }
        )
        page = self._client.query(self._instance_id, request=prepared)
        coordinate = AcceptedCoordinate.model_validate(
            page.receipt.coordinate.model_dump(mode="json")
        )
        self._observe_read(coordinate, expected=expected)

        def fetch(next_cursor: str) -> QueryResult:
            return self._run_query_request(
                prepared.model_copy(update={"cursor": next_cursor}),
                at=coordinate,
                evaluation_time=prepared.evaluation_time,
            )

        return QueryResult(page, fetch=fetch)

    def accepted_procedure(self, procedure: str | ProcedureRef) -> Procedure:
        """A handle on one accepted Procedure, read lazily.

        Next: ``procedure.run(input=procedure.input(...))``, or ``cx.get(procedure.ref)``
        for its readiness and track record.
        """

        name = _address(procedure, RefKind.PROCEDURE)
        requested = self._read_at(
            procedure.coordinate if isinstance(procedure, ProcedureRef) else None
        )
        return Procedure(self, name, None if requested is None else _coordinate(requested))

    def check_line(
        self,
        line: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> LineTriggerCheckResult:
        """Inspect trigger eligibility and retained admissions without starting work.

        Next: ``cx.evaluate_line(...)`` for a missed range, or ``cx.dispatch_line(line)``
        for pending work.
        """
        return self._client.check_line(
            self._instance_id,
            line,
            request=LineTriggerCheckRequest(since=since, until=until, limit=limit, cursor=cursor),
        )

    def arm_line(
        self, line: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> api.LineArm:
        """Arm a Line forward-only: the daemon admits what it matches from now on.

        Runs use this connection's credential, rechecked before each admission,
        and the Line version current now. Work already pending stays for
        `dispatch_line`. Arming it again unchanged returns `outcome="already_armed"`.
        `dry_run=True` previews it (`would_arm`) and records nothing; commit
        exactly that with `at=` the preview's `coordinate.git_oid`.

        Next: ``cx.line_status(line)``, or ``cx.get(f"Line:{line}")`` for its occurrences
        and runs.
        """
        return self._client.arm_line(self._instance_id, line, dry_run=dry_run, at=at)

    def disarm_line(
        self, line: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> api.LineArm:
        """Stop a Line admitting work on its own; admitted runs are not cancelled.

        A Line whose arm already stopped returns `outcome="already_disarmed"`.
        `dry_run=True` previews it (`would_disarm`); `at` pins the commit.

        Next: ``cx.arm_line(line)`` to resume it.
        """
        return self._client.disarm_line(self._instance_id, line, dry_run=dry_run, at=at)

    def line_status(self, line: str) -> api.LineArm:
        """The Line's current arm, or its last one and why it stopped.

        Next: ``cx.arm_line(line)`` if it stopped, or ``cx.get(f"Line:{line}")`` for its
        runs.
        """
        return self._client.line_status(self._instance_id, line)

    def evaluate_line(
        self,
        line: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 100,
        cursor: str | None = None,
    ) -> api.LineTriggerCheckResult:
        """Explicitly turn a missed range into pending occurrences.

        Next: ``cx.dispatch_line(line)`` to admit what it found.
        """
        return self._client.evaluate_line(
            self._instance_id,
            line,
            request=api.LineEvaluateRequest(since=since, until=until, limit=limit, cursor=cursor),
        )

    def dispatch_line(
        self, line: str, *, occurrence_id: str | None = None, limit: int = 1, retry: bool = False
    ) -> api.LineDispatchResult:
        """Admit pending work using this connection's current actor and authority.

        Next: ``cx.get(f"ProcedureRun:{item.run_id}")`` for each admitted run.
        """
        return self._client.dispatch_line(
            self._instance_id,
            line,
            request=api.LineDispatchRequest(occurrence_id=occurrence_id, limit=limit, retry=retry),
        )

    def run_line(
        self,
        line: str,
        *,
        trigger: str | None = None,
        occurrence_id: str | None = None,
        resolution_contract: ResolutionContractReference | None = None,
        trigger_event: TriggerEventReference | None = None,
    ) -> ProcedureRun:
        """Trigger a named accepted Line; the daemon resolves its exact identity.

        ``trigger`` names the Trigger this occurrence fires on; omit it only for
        a Line no live Trigger aims at, which runs when run explicitly.

        Next: ``run.succeeded`` and ``run.result``, or
        ``cx.get(f"ProcedureRun:{run.run_id}")``.
        """

        result = self._client.run_line(
            self._instance_id,
            line,
            trigger=trigger,
            occurrence_id=occurrence_id,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            evaluation_time=self._evaluation_time(),
        )
        return ProcedureRun(self, result)

    def get(
        self,
        ref: str | TypedRef,
        *,
        detail: GetDetail = "summary",
        range: tuple[int, int] | str | None = None,
    ) -> KnowledgeCard:
        """Read one governed thing by reference, resolved directly by the daemon.

        ``ref`` is a typed ref or any string form an agent sees: ``CLM-…`` or a
        unique prefix, ``kind/id``, a predicate, ``ClaimType:``/``Document:``/
        ``Procedure:``/``query:``/``CaptureContract:<name>``, an artifact path,
        a proposal id, or an operational reference: ``Line:<name>`` (or the
        Line identity digest ``next`` names), ``CAP-<12+ hex>`` /
        ``Capture:<digest>``, ``ResolutionContract:<name>``, ``Mandate:<name>``
        and ``ProcedureRun:<run_id>`` (or ``RUN-<12+ hex>``). A wrong or
        ambiguous name refuses with the nearest names. A Claim summary is a
        ``ClaimView``; other summaries are the values-first card; other
        details carry that detail's payload.

        Next: ``detail="evidence"`` for what backs a Claim, ``detail="history"`` for its
        revisions, ``detail="proof"`` for the full envelope; ``card.ref`` passes the thing
        on as a typed ref.
        """

        if isinstance(ref, SourceRef):
            return self._source_card(ref)
        coordinate: AcceptedCoordinate | None = None
        if isinstance(ref, str):
            text = ref
        else:
            coordinate = ref.coordinate
            prefixes = {
                RefKind.SUBJECT: "",
                RefKind.CLAIM: "",
                RefKind.CLAIM_TYPE: "ClaimType:",
                RefKind.PROCEDURE: "Procedure:",
                RefKind.QUERY: "query:",
            }
            if ref.kind not in prefixes:
                raise ReferenceKindError(f"get does not read {ref.kind.value} references")
            text = prefixes[ref.kind] + ref.address
        window: ByteRange | None = None
        if isinstance(range, str):
            window = ByteRange.parse(range)
        elif range is not None:
            window = ByteRange(start=range[0], end=range[1])
        claim_summary = detail == "summary" and (
            (not isinstance(ref, str) and ref.kind is RefKind.CLAIM)
            or (isinstance(ref, str) and ref.removeprefix("Claim:").startswith("CLM-"))
        )
        result = self._get(text, "proof" if claim_summary else detail, window, coordinate)
        if detail == "summary" and result.kind == "claim" and not claim_summary:
            # The reference resolved to a Claim only on the daemon; read its
            # envelope at the same coordinate for the typed view.
            result = self._get(result.ref, "proof", None, _get_coordinate(result))
        kind = _GET_REF_KINDS[result.kind]
        identity = (
            result.ref
            if result.kind in {"claim", "subject", "proposal"}
            else result.ref.split(":", 1)[1]
        )
        if result.kind == "proposal":
            identity = result.ref.removeprefix("Proposal:")
        value: object
        if result.kind == "claim" and result.detail == "proof":
            view = api.ClaimViewRecord.model_validate(result.proof)
            value = (
                self._with_exact_text(
                    self._typed_claim_view(view, identity), _get_coordinate(result)
                )
                if detail == "summary"
                else view
            )
        elif result.card is not None:
            value = result.card
        else:
            value = next(
                item
                for item in (result.evidence, result.history, result.body, result.why, result.proof)
                if item is not None
            )
        return KnowledgeCard(kind, identity, _get_coordinate(result), value)

    def _get(
        self,
        ref: str,
        detail: GetDetail,
        window: ByteRange | None,
        coordinate: AcceptedCoordinate | None,
    ) -> GetResult:
        requested = self._read_at(coordinate)
        request = GetRequest(
            ref=ref,
            detail=detail,
            range=window,
            at=requested,
            evaluation_time=datetime.fromisoformat(self._evaluation_time()),
            surface="sdk",
            # The SDK pins every read, so it asks for the full coordinate
            # that summary answers otherwise leave out.
            full_coordinate=True,
        )
        result = self._client.get(self._instance_id, request=request)
        self._observe_read(_get_coordinate(result), expected=requested)
        if result.history is not None:
            # History pages newest first; the SDK reads every page, pinned to
            # the first page's coordinate by the cursor, so it never answers
            # a silently cut history.
            revisions = list(result.history.revisions)
            page = result
            while page.truncated and page.next_cursor is not None:
                page = self._client.get(
                    self._instance_id,
                    request=request.model_copy(update={"at": None, "cursor": page.next_cursor}),
                )
                assert page.history is not None
                revisions.extend(page.history.revisions)
            result = result.model_copy(
                update={
                    "history": result.history.model_copy(update={"revisions": tuple(revisions)}),
                    "truncated": False,
                    "next_cursor": None,
                }
            )
        return result

    def _source_card(self, ref: SourceRef) -> KnowledgeCard:
        """A catalog source: its Document's card, or the catalog entry when it has none.

        The catalog name lives only in the workspace's source catalog, so the
        name resolves there; an entry that declares a Document reads that
        Document at the ref's coordinate, and an evidence-only entry answers
        the entry itself.
        """

        entry = self._sources.entry_for_source(ref.address)
        if entry.document_id is not None:
            result = self._get(f"Document:{entry.document_id}", "summary", None, ref.coordinate)
            return KnowledgeCard(
                RefKind.SOURCE,
                ref.address,
                _get_coordinate(result),
                result.card if result.card is not None else result.proof,
            )
        return KnowledgeCard(
            RefKind.SOURCE, ref.address, ref.coordinate, entry.model_dump(mode="json")
        )

    def orient(
        self,
        *,
        kind: str | None = None,
        section: api.OrientSection | None = None,
        limit: int = api.ORIENT_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> api.OrientResult:
        """Map accepted state in one call, at this context's coordinate.

        With no arguments: each Subject kind with its count and predicates,
        artifact counts, named queries, whether this caller can author, what
        needs attention, and ``next`` suggestions written as SDK calls.
        ``kind`` reads one kind in full with sample Subject IDs and the
        predicates that point at it (``incoming``: follow one with
        ``query(kind, follow=[(predicate, alias, "reverse")])``); ``section``
        pages documents, procedures, claim_types, queries or interfaces (the
        provider interfaces a Procedure can call), or an operational family:
        runs, lines, captures, capture_contracts, predictions or mandates. Follow
        ``next_cursor`` while ``truncated``. When this workspace holds an
        exported floor, ``floor`` says the coordinate it is at and how many
        generations it is behind this answer.

        Next: the answer's own ``next`` calls; ``cx.query(kind, ...)`` for values,
        ``cx.get(ref)`` for one thing.
        """

        requested = self._read_at()
        result = self._client.orient(
            self._instance_id,
            kind=kind,
            section=section,
            limit=limit,
            cursor=cursor,
            at=requested,
            evaluation_time=self._evaluation_time(),
            surface="sdk",
        )
        self._observe_read(
            result.coordinate,
            expected=requested if cursor is None else None,
        )
        if self._workspace is None:
            # No workspace, no floor to compare: the floor is unknown, not stale.
            return result
        return workspace_floor_freshness(self._workspace, result)

    def _append_attestation(
        self,
        *,
        prepared: PreparedClaimAttestationRequest,
        signer: ClaimAttestationSigner,
    ) -> ClaimAttestationAppendResult:
        return append_prepared_claim_attestation(
            self._client,
            self._instance_id,
            prepared=prepared,
            signer=signer,
        )

    def attest(
        self,
        claim: ClaimRef | str,
        *,
        stance: ClaimStance,
        signer: ClaimAttestationSigner,
        note: str | None = None,
        valid_until: datetime | None = None,
    ) -> ClaimAttestationAppendResult:
        """Sign that the caller examined the current exact Claim and append it once.

        Next: ``cx.get(claim)`` shows the attestation among the Claim's flags;
        ``detail="evidence"`` lists it.
        """

        identity = claim.address if isinstance(claim, ClaimRef) else claim
        return self._append_attestation(
            prepared=PreparedClaimAttestationRequest(
                claim_id=identity.removeprefix("Claim:"),
                attestation_basis="examined_existing",
                stance=stance,
                referent_coordinate=(
                    claim.coordinate
                    if isinstance(claim, ClaimRef)
                    else self.coordinate
                    if self._pinned
                    else None
                ),
                attested_at=datetime.fromisoformat(self._evaluation_time()),
                valid_until=valid_until,
                note=note,
            ),
            signer=signer,
        )

    def attest_new_capture(
        self,
        request: PreparedClaimAttestationRequest,
        *,
        signer: ClaimAttestationSigner,
    ) -> ClaimAttestationAppendResult:
        """Append a pre-staged new-Capture observation after exact client signing.

        Next: ``cx.get(claim, detail="evidence")`` to see it among the Claim's attestations.
        """

        if request.attestation_basis != "new_capture":
            raise ValueError("attest_new_capture requires attestation_basis='new_capture'")
        return self._append_attestation(prepared=request, signer=signer)

    def next(self, *, expiring_within: Duration) -> NextPage:
        """Read the whole queue of work that needs this caller.

        The queue covers accepted state and this workspace. Every page is read, pinned to
        the first page's instant, coordinate and attestation head; ``expiring_within`` says
        how far ahead expiring evidence counts. Each item names its ``reason``, the thing it
        is about and its ``repair``, rendered as an SDK call this caller can run
        (``repair_requires`` names what a withheld one needs). ``page.status`` reports the
        environment: the floor, the ledger mirror, providers, Line dispatch. Next: run an
        item's ``repair.command``, or ``cx.get(item.subject_identity)`` to look first.
        """

        requested_coordinate = self._read_at()
        access_profile = self._access_profile.model_dump()
        observation, scanned_coordinate = observe_next_workspace_with_coverage(
            self._client,
            self._instance_id,
            self._workspace_root,
            observation=observe_next_workspace(self._workspace_root),
            coordinate=requested_coordinate,
            access_profile=access_profile,
            # Only procedure-projection-only workspaces need a separate head
            # binding. Resolve metadata, not a whole-world orientation page.
            resolve_coordinate=lambda: self._client.whoami(self._instance_id).coordinate,
        )
        evaluation_time = self._evaluation_time()

        def page(cursor: str | None) -> api.NextResult:
            return self._client.next(
                self._instance_id,
                evaluation_time=evaluation_time,
                access_profile=access_profile,
                at=scanned_coordinate or requested_coordinate,
                expiring_within=expiring_within.model_dump(),
                workspace_observation=observation,
                limit=api.NEXT_MAX_LIMIT,
                cursor=cursor,
                # Repairs render as SDK calls, not CLI commands.
                caller_surface="sdk",
            )

        # A NextPage is the whole queue. Each cursor pins its first page's
        # instant, coordinate and head, so every later page reads that queue.
        result = page(None)
        items = list(result.items)
        while result.next_cursor is not None:
            result = page(result.next_cursor)
            items.extend(result.items)
        self._observe_read(
            _coordinate(result.coordinate), expected=scanned_coordinate or requested_coordinate
        )
        return NextPage(
            coordinate=_coordinate(result.coordinate),
            evaluation_time=result.evaluation_time,
            items=tuple(items),
            result_digest=result.result_digest,
            observed_domains=tuple(result.observed_domains),
            unobserved_domains=tuple(result.unobserved_domains),
            status=result.status,
            attestation_head_digest=result.attestation_head_digest,
        )

    def since(
        self,
        generation: int,
        *,
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: api.SinceCursor | Mapping[str, object] | None = None,
    ) -> api.SinceResult:
        """Read accepted changes at current head, a pinned context, or the cursor's snapshot.

        Next: pass ``result.next_cursor`` back as ``cursor`` while truncated;
        ``cx.get(ref)`` on a changed artifact.
        """

        result = self._client.since(
            self._instance_id,
            generation=generation,
            access_profile=self._access_profile.model_dump(),
            at=self._read_at(),
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )
        self._observe_read(_coordinate(result.coordinate), expected=self._read_at())
        return result

    def curation_list(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> api.CurationListResult:
        """Read one page of the curation queue: a pure read of what detection recorded.

        Detection runs on its own on accepted generations; ``result.detection`` says
        when it last ran and ``inactive_detectors`` which detectors cannot run here.
        A truncated page carries ``next_cursor``; pass it back as ``cursor``.

        Next: ``cx.curation_overrule(...)``, ``cx.curation_accept_fixed(...)`` or
        ``cx.curation_suppress(...)`` on an item.
        """

        return self._client.list_curation(
            self._instance_id,
            access_profile=self._access_profile.model_dump(),
            limit=limit,
            cursor=cursor,
        )

    def curation_observe(
        self, *, dry_run: bool | None = None, at: str | None = None
    ) -> api.CurationObserveResult:
        """Record this workspace's declared blocks for block-churn detection.

        Block churn is the one detector that needs the workspace, which the daemon
        never reads; detection picks the scan up the next time it runs.

        Next: ``cx.curation_list()`` after the next accepted generation.
        """

        access_profile = self._access_profile.model_dump()
        observation, _coordinate = observe_next_workspace_with_coverage(
            self._client,
            self._instance_id,
            self._workspace_root,
            observation=observe_next_workspace(self._workspace_root),
            access_profile=access_profile,
        )
        return self._client.observe_curation(
            self._instance_id, workspace_observation=observation, dry_run=dry_run, at=at
        )

    def audit(
        self,
        *,
        claim_type_identities: tuple[str, ...] = (),
        subject_kinds: tuple[str, ...] = (),
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: api.AuditCursor | Mapping[str, object] | None = None,
    ) -> api.AuditResult:
        """Rank visible Claim verification work without changing governed state.

        Next: ``cx.get(row's Claim ID, detail="evidence")``, then ``cx.attest(...)`` once
        examined.
        """

        result = self._client.audit(
            self._instance_id,
            evaluation_time=self._evaluation_time(),
            access_profile=self._access_profile.model_dump(),
            at=self._read_at(),
            claim_type_identities=claim_type_identities,
            subject_kinds=subject_kinds,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )
        self._observe_read(_coordinate(result.coordinate), expected=self._read_at())
        return result

    def curation_overrule(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> api.CurationActionResult:
        """Record, permanently, that a detector pattern does not apply here.

        ``dry_run`` checks the ruling and appends nothing; ``at`` pins the commit.

        Next: ``cx.curation_list()``; the pattern is never raised again.
        """

        return self._client.overrule_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        )

    def curation_accept_fixed(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str | None = None,
        accepted_changeset_digest: str | None = None,
        accepted_generation: int | None = None,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> api.CurationActionResult:
        """Link an item to the accepted change that fixed it.

        Name the change by ``accepted_proposal_id`` (``accepted_changeset_digest``
        pins it exactly) or by ``accepted_generation``; the daemon resolves the rest.

        Next: ``cx.curation_list()``; the item is resolved.
        """

        return self._client.accept_fixed_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            accepted_generation=accepted_generation,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        )

    def curation_suppress(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "lineage"],
        until_generation: int | None = None,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> api.CurationActionResult:
        """Hide an item (``item``) or its whole lineage (``lineage``) without resolving it.

        Detection keeps running; ``until_generation`` lets the suppression lapse.

        Next: ``cx.curation_unsuppress(...)`` to lift it.
        """

        return self._client.suppress_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        )

    def curation_unsuppress(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        suppression_event_id: str | None = None,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> api.CurationActionResult:
        """Lift a suppression on an item; name it when the item carries more than one.

        Next: ``cx.curation_list()``; what it hid is listed again.
        """

        return self._client.unsuppress_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            suppression_event_id=suppression_event_id,
            attribution_refs=attribution_refs,
            dry_run=dry_run,
            at=at,
        )

    def _assert_coordinate(self, coordinate: AcceptedCoordinate) -> None:
        if coordinate != self.coordinate:
            raise ValueError(
                "typed reference coordinate differs from the active orientation; refresh or "
                "use the reference in authoring so the daemon can report its successor"
            )

    def _evaluation_time(self) -> str:
        return cast(str, format_datetime(self._clock()))


class ProjectionBlocks:
    """Declared projection blocks: client-side stamps over accepted Claims in workspace files.

    Next: ``cx.block.sync()`` to check every block.
    """

    def __init__(self, cx: Cruxible) -> None:
        self._playbill = cx

    def repin(
        self,
        source: str | SourceRef,
        block_id: str,
        *,
        claims: Sequence[str | ClaimRef] | None = None,
        queries: Sequence[str | QueryRef | tuple[str | QueryRef, Mapping[str, CanonicalValue]]]
        | None = None,
        artifacts: Sequence[ArtifactIdentity] | None = None,
        currency_policy: ProjectionCurrencyPolicy | None = None,
        backing_digest: str | None = None,
        evaluation_time: datetime,
        body: str | bytes | None = None,
        compact: bool = True,
        render: bool = False,
        dry_run: bool = False,
    ) -> ProjectionBlockStamp:
        """Refresh backing pins and optionally replace this block's authored body.

        Compact markers are the default: digest references with local manifests. Subsequent
        repins preserve that format. ``render`` writes the body as a table or list from
        the block's one query backing instead of ``body``. ``dry_run`` returns the stamp
        it would write and writes nothing.

        Next: Cruxible.orient() to map state, Cruxible.query() for rows, or Cruxible.get().
        """
        source_id = _address(source, RefKind.SOURCE)
        if isinstance(source, SourceRef):
            self._playbill._assert_coordinate(source.coordinate)
        claim_refs: list[str] = []
        for claim in claims or ():
            if isinstance(claim, ClaimRef):
                self._playbill._assert_coordinate(claim.coordinate)
            claim_refs.append(_address(claim, RefKind.CLAIM))
        query_refs: list[tuple[str, Mapping[str, object]]] = []
        for entry in queries or ():
            if isinstance(entry, tuple):
                query, parameters = entry
            else:
                query, parameters = entry, {}
            if isinstance(query, QueryRef):
                self._playbill._assert_coordinate(query.coordinate)
            query_refs.append((_address(query, RefKind.QUERY), parameters))
        return repin_projection_block(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace_root,
            source_id=source_id,
            block_id=block_id,
            claims=claim_refs if claims is not None else None,
            queries=query_refs if queries is not None else None,
            artifacts=artifacts,
            currency_policy=currency_policy,
            backing_digest=backing_digest,
            evaluation_time=evaluation_time,
            coordinate=self._playbill.coordinate,
            body=body.encode("utf-8") if isinstance(body, str) else body,
            compact=compact,
            render=render,
            dry_run=dry_run,
        )

    def sync(self, *paths: str | Path, all: bool = False) -> api.BlockSyncResult:
        """Check each block against its backings; a pure check that writes nothing.

        Each block reads unchanged, stale, dirty or skipped; a require_current
        block's drift is a refusal.

        Next: ``cx.next(...)`` names each drifted block with its repair.
        """

        return sync_projection_blocks(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace_root,
            paths=paths,
            all_sources=all,
        )

    def detach(self, *paths: str | Path, dry_run: bool = False) -> api.BlockSyncResult:
        """Strip retired blocks' markers from these pages, keeping each body as prose.

        Only blocks whose every backing is retired, or that belong to another
        instance, are detached; live blocks are refused. Edits pages only.
        ``dry_run`` reports what would change and edits nothing.

        Next: ``cx.block.depublish(source, block)`` when the block is not coming back.
        """

        if not paths:
            raise ValueError("name at least one page to detach retired blocks from")
        return sync_projection_blocks(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace_root,
            check=dry_run,
            detach_paths=paths,
        )

    def depublish(
        self,
        source: str | SourceRef,
        block_id: str,
        *,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> api.BlockDepublishResult:
        """Release the registration that demands one page block.

        ``next`` reads that registration to call a removed marker a blocking
        row; releasing it edits no page and retires no backing. ``dry_run``
        previews; ``at`` pins a commit to the preview's coordinate.

        Next: ``cx.block.detach(page)`` strips the markers if they remain.
        """

        source_id = _address(source, RefKind.SOURCE)
        return self._playbill._client.depublish_block(
            self._playbill._instance_id, source_id, block_id, dry_run=dry_run, at=at
        )


@dataclass(frozen=True)
class MeasurementOutcome:
    """One declared measurement's standing after an evaluation or inspection.

    Next: ``procedure.readings(run=...)`` for retained readings.
    """

    measurement_name: str
    status: str
    reading_status: str
    verdict: str | None
    resolution_id: str | None
    reading_id: str | None
    detail: str | None
    raw: api.ProcedureMeasurementRow


@dataclass(frozen=True)
class MeasurementBatch:
    """The result of one measurement evaluation: rows plus the three coordinates.

    Next: ``batch[name]`` for one measurement's outcome.
    """

    run_id: str | None
    activation_coordinate: AcceptedCoordinate
    observation_coordinate: AcceptedCoordinate
    observation_time: datetime
    outcomes: tuple[MeasurementOutcome, ...]
    raw: api.ProcedureMeasureResult

    def __getitem__(self, measurement_name: str) -> MeasurementOutcome:
        for outcome in self.outcomes:
            if outcome.measurement_name == measurement_name:
                return outcome
        raise KeyError(measurement_name)


def _measurement_batch(raw: api.ProcedureMeasureResult) -> MeasurementBatch:
    return MeasurementBatch(
        run_id=raw.run_id,
        activation_coordinate=raw.activation_coordinate,
        observation_coordinate=raw.observation_coordinate,
        observation_time=raw.observation_time,
        outcomes=tuple(
            MeasurementOutcome(
                measurement_name=row.measurement_name,
                status=row.status,
                reading_status=row.reading_status,
                verdict=None if row.resolution is None else row.resolution.verdict,
                resolution_id=None if row.resolution is None else row.resolution.resolution_id,
                reading_id=None if row.reading is None else row.reading.reading_id,
                detail=row.detail,
                raw=row,
            )
            for row in raw.rows
        ),
        raw=raw,
    )


class Procedure:
    """A handle on one accepted Procedure by name, read lazily.

    Next: ``procedure.run(input=procedure.input(...))``, or ``cx.get(procedure.ref)`` for its
    readiness and track record.
    """

    def __init__(self, cx: Cruxible, name: str, coordinate: AcceptedCoordinate | None) -> None:
        self._playbill = cx
        self._name = name
        self._coordinate = coordinate
        self._artifact: ProcedureArtifactAny | None = None

    @property
    def definition(self) -> ProcedureArtifactAny:
        """Exact accepted definition used for typed inputs and nested bindings.

        Next: ``procedure.input(...)`` to build a typed input.
        """
        if self._artifact is None:
            reading = self.readiness()
            if reading.artifact is None:
                raise ValueError("Daemon did not return the accepted Procedure definition")
            if (
                procedure_artifact_digest(reading.artifact).tagged
                != reading.procedure_artifact_digest
            ):
                raise ValueError("Procedure read does not reproduce its accepted digest")
            self._artifact = reading.artifact
            self._coordinate = _coordinate(reading.coordinate)
        return self._artifact.model_copy(deep=True)

    @property
    def input(self) -> RecordConstructor:
        """The typed constructor for this Procedure's input record.

        Next: ``procedure.run(input=procedure.input(...))``.
        """

        return procedure_record_constructor(self.definition, "input")

    @property
    def ref(self) -> ProcedureRef:
        """This Procedure as a typed ref. Next: ``cx.get(procedure.ref)``."""

        coordinate = self._coordinate or _coordinate(self.readiness().coordinate)
        return ProcedureRef(self._name, coordinate)

    def readiness(self) -> api.ProcedureReadiness:
        """Whether this Procedure can run now, and which slots still need binding.

        Next: ``procedure.bind(bindings=...)`` for open slots, else ``procedure.run(...)``.
        """

        requested = self._playbill._read_at(self._coordinate)
        result = self._playbill._client.procedure_readiness(
            self._playbill._instance_id,
            self._name,
            evaluation_time=self._playbill._evaluation_time(),
            at=requested,
        )
        self._playbill._observe_read(_coordinate(result.coordinate), expected=requested)
        return result

    def bind(
        self, *, bindings: Mapping[str | ProcedureSlotRef, TypedRef]
    ) -> api.ProcedureBindResult:
        # Binding is a current-state write with the existing daemon admission
        # contract, not a snapshot read. Preserve its observed-reference guard.
        """Bind this Procedure's open slots to accepted things.

        Next: ``procedure.readiness()``, then ``procedure.run(...)``.
        """

        coordinate = self._coordinate or self._playbill.coordinate
        self._playbill._assert_coordinate(coordinate)
        rows: list[dict[str, object]] = []
        for key, value in bindings.items():
            slot = key if isinstance(key, str) else _address(key, RefKind.SLOT)
            if isinstance(key, ProcedureSlotRef) and key.coordinate != coordinate:
                raise ValueError("procedure binding references must match its observed coordinate")
            if isinstance(value, ProcedureSlotRef):
                raise ReferenceKindError("a slot cannot be bound to another slot")
            if value.coordinate != coordinate:
                raise ValueError("procedure binding references must match its observed coordinate")
            target_kind = _REFERENCE_KINDS.get(value.kind)
            if target_kind is None:
                raise ReferenceKindError(f"cannot bind {value.kind.value} to a procedure slot")
            rows.append(
                {
                    "slot_name": slot,
                    "target": {"kind": target_kind, "name": value.address},
                }
            )
        rows.sort(key=lambda item: str(item["slot_name"]).encode("utf-8"))
        result = self._playbill._client.bind_procedure(
            self._playbill._instance_id, self._name, bindings=rows
        )
        return result

    def run(
        self,
        *,
        input: Record | None = None,
        at: AcceptedCoordinate | None = None,
        resolution_contract: ResolutionContractReference | None = None,
        trigger_event: TriggerEventReference | None = None,
    ) -> ProcedureRun:
        """Run this Procedure on a typed input and return the run.

        Next: ``run.succeeded`` and ``run.result``, or
        ``cx.get(f"ProcedureRun:{run.run_id}")``.
        """

        if at is not None and self._coordinate is not None and at != self._coordinate:
            raise ValueError("run coordinate differs from the pinned Procedure")
        if at is not None:
            self._coordinate = at
        if input is not None and not isinstance(input, Record):
            raise TypeError("Procedure.run requires a record made by procedure.input")
        constructor = self.input
        normalized = constructor() if input is None else constructor.from_wire(input)
        result = self._playbill._client.run_procedure(
            self._playbill._instance_id,
            self._name,
            evaluation_time=self._playbill._evaluation_time(),
            at=self._playbill._read_at(at or self._coordinate),
            input=normalize_canonical(normalized),
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
        )
        return ProcedureRun(
            self._playbill, result, output=procedure_record_constructor(self.definition, "output")
        )

    def measure(
        self,
        *,
        run: ProcedureRun | str | None = None,
        measurements: Sequence[str] = (),
        at: AcceptedCoordinate | None = None,
    ) -> MeasurementBatch:
        """Evaluate this Procedure's due measurements, crediting ``run`` if given.

        Pending measurements are reported, not evaluated; a standing resolution
        is returned rather than re-derived; calling again with the same run
        replays the same reading.

        Next: ``batch[name]`` for one measurement.
        """

        if at is not None and self._coordinate is not None and at != self._coordinate:
            raise ValueError("measurement coordinate differs from the pinned Procedure")
        requested = self._playbill._read_at(at or self._coordinate)
        run_id = run.run_id if isinstance(run, ProcedureRun) else run
        if isinstance(run, ProcedureRun) and run_id is None:
            raise ValueError(
                "a run without a run_id was refused at admission and cannot be measured"
            )
        result = self._playbill._client.measure_procedure(
            self._playbill._instance_id,
            self._name,
            request=api.ProcedureMeasureRequest(
                run_id=run_id,
                measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
                evaluation_time=datetime.fromisoformat(self._playbill._evaluation_time()),
                at=None if requested is None else _coordinate(requested),
            ),
        )
        self._playbill._observe_read(result.observation_coordinate, expected=requested)
        return _measurement_batch(result)

    def readings(
        self,
        *,
        run: ProcedureRun | str | None = None,
        measurements: Sequence[str] = (),
        limit: int = 50,
        cursor: str | None = None,
        at: AcceptedCoordinate | None = None,
    ) -> api.ProcedureReadingsResult:
        """Inspect measurement standing and retained readings. Never writes.

        A page's ``cursor`` continues that page's selection: the observation
        instant and coordinate the first page was answered at travel inside
        it, so passing the cursor back with the same ``run``/``measurements``
        pages the same selection even though this call stamps a fresh clock.

        Next: ``procedure.measure(run=...)`` to credit a run.
        """

        if at is not None and self._coordinate is not None and at != self._coordinate:
            raise ValueError("readings coordinate differs from the pinned Procedure")
        requested = self._playbill._read_at(at or self._coordinate)
        run_id = run.run_id if isinstance(run, ProcedureRun) else run
        result = self._playbill._client.list_procedure_readings(
            self._playbill._instance_id,
            self._name,
            request=api.ProcedureReadingsRequest(
                run_id=run_id,
                measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
                evaluation_time=datetime.fromisoformat(self._playbill._evaluation_time()),
                at=None if requested is None else _coordinate(requested),
                limit=limit,
                cursor=cursor,
            ),
        )
        self._playbill._observe_read(result.observation_coordinate, expected=requested)
        return result


class ProcedureRun:
    """One Procedure run as the daemon reported it.

    Next: ``run.result`` once ``run.succeeded``; ``cx.get(f"ProcedureRun:{run.run_id}")`` for
    its card.
    """

    def __init__(
        self,
        cx: Cruxible,
        raw: api.ProcedureRunState,
        *,
        output: RecordConstructor | None = None,
    ) -> None:
        self._playbill = cx
        self._raw = raw
        self._output = output

    @property
    def run_id(self) -> str | None:
        """The run's ID; None when admission refused it.

        Next: ``cx.get(f"ProcedureRun:{run.run_id}")``.
        """

        return self._raw.run_id

    @property
    def status(self) -> str:
        """The run's status. Next: ``run.refresh()`` while ``running``."""

        return self._raw.status

    @property
    def succeeded(self) -> bool:
        """Whether the run succeeded. Next: ``run.result``."""

        return self._raw.status == "succeeded"

    @property
    def result(self) -> Record:
        """The run's output record, typed by the Procedure's output contract.

        Raises unless the run succeeded. Next: write what it found with ``cx.set(...)``.
        """

        if not self.succeeded:
            raise ValueError(f"Procedure has no successful output: {self.status}")
        if self._output is None:
            owner = Procedure(
                self._playbill, str(self._raw.procedure_identity["name"]), self.coordinate
            )
            self._output = procedure_record_constructor(owner.definition, "output")
        return self._output.from_wire(self._raw.result)

    @property
    def outcome(self) -> api.ProcedureRunState:
        """The run's full served state. Next: ``run.terminal_egress`` for what its terminals
        did.
        """

        return self._raw.model_copy(deep=True)

    @property
    def terminal_egress(self) -> tuple[ProcedureTerminalEgress, ...]:
        """What each terminal did, with the authority it needed and the run held.

        A settle terminal reports `settle_outcome`: `settled` with the
        `accepted_git_oid`, or `proposed` with its `proposal_id` and
        `fallback_reason`.

        Next: ``cx.proposal(egress.proposal_id).review()`` for a proposed settle.
        """
        return tuple(item.model_copy(deep=True) for item in self._raw.terminal_egress)

    @property
    def receipt(self) -> str | None:
        """The run receipt's digest. Next: ``cx.get(f"ProcedureRun:{run.run_id}",
        detail="proof")``.
        """

        return self._raw.receipt_digest

    @property
    def coordinate(self) -> AcceptedCoordinate:
        """The coordinate the run was bound to.

        Next: ``cx.at(run.coordinate)`` to read what it read.
        """

        return _coordinate(self._raw.coordinate)

    @property
    def children(self) -> tuple[ProcedureRun, ...]:
        """Read the retained child runs through the same authorized run service.

        Next: ``child.result`` on each.
        """
        return tuple(
            ProcedureRun(
                self._playbill,
                self._playbill._client.get_procedure_run(self._playbill._instance_id, link.run_id),
            )
            for link in self._raw.children
        )

    @property
    def track_record(self) -> tuple[GetProcedureTrackRecord, ...]:
        """This run's Procedure's accepted track record: one entry per promotion.

        Read from ``cx.get("Procedure:<name>")`` in this connection's context
        (its live head, or the coordinate it is pinned to), so promotions
        accepted after this run count too. Empty until a promotion of the
        Procedure's run exhaust is accepted. Next: ``cx.get(...)`` with
        ``detail="proof"`` for the Procedure's full accepted definition.
        """

        name = str(self._raw.procedure_identity["name"])
        card = self._playbill._get(f"Procedure:{name}", "summary", None, None).card
        if not isinstance(card, GetProcedureCard):
            raise ValueError(f"get did not answer Procedure:{name} with a Procedure card")
        return card.track_record

    def refresh(self) -> ProcedureRun:
        """Read this run's state again. Next: ``run.status``."""

        if self.run_id is None:
            return self
        self._raw = self._playbill._client.get_procedure_run(
            self._playbill._instance_id, self.run_id
        )
        return self

    def measure(self, *, measurements: Sequence[str] = ()) -> MeasurementBatch:
        """Credit this run's exact grain with every due measurement's standing answer.

        Next: ``batch[name]`` for one measurement.
        """

        if self.run_id is None:
            raise ValueError(
                "a run without a run_id was refused at admission and cannot be measured"
            )
        name = str(self._raw.procedure_identity.get("name", ""))
        # Observation follows the owning live/pinned context, independently of
        # the run's admission coordinate. The daemon verifies the run revision.
        return Procedure(self._playbill, name, None).measure(run=self, measurements=measurements)


__all__ = [
    "ChangeSetDraft",
    "ClaimDraft",
    "ClaimTypeDraft",
    "ClaimTypeProposal",
    "Intent",
    "KnowledgeCard",
    "MeasurementBatch",
    "MeasurementOutcome",
    "NextPage",
    "Cruxible",
    "Prediction",
    "PredictionSettlement",
    "Procedure",
    "ProcedureDraft",
    "ProcedureRun",
    "Proposal",
    "QueryDraft",
    "SDK_CONTRACT_SNAPSHOT_DIGEST",
    "SubjectDraft",
    "carry",
    "re_author",
    "rescind",
    "retire",
]
